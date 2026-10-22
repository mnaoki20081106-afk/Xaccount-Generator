"""
100垢を並列で管理するオーケストレーター。
各アカウントのライフサイクル（生成→ウォームアップ→運用→バン解除）を制御する。
"""
import asyncio
import random
import time
from datetime import datetime, timezone as _tz
from typing import Optional
from loguru import logger

from .config import AppConfig, load_config
from .db import Database
from .models import Account, AccountStatus
from .proxy.manager import ProxyManager
from .auth.twofa import TwoFAManager
from .utils.browser import BrowserManager
from .factory.account_factory import AccountFactory
from .email_provider.mailtm import MailTmClient
from .actions.login import LoginAction
from .actions.tweet import TweetAction
from .actions.follow import FollowAction
from .actions.like import LikeAction
from .warmup import WarmupScheduler
from .detector import ShadowbanDetector, ShadowbanResolver, SearchTopChecker


def _account_age_days(account: Account, now: datetime) -> int:
    """アカウントの垢齢（日数）を返す。timezone-aware に正規化して計算。"""
    ca = account.created_at
    if ca.tzinfo is None:
        ca = ca.replace(tzinfo=_tz.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz.utc)
    return max(0, (now - ca).days)


class _RateLimiter:
    """
    1時間あたり per_hour 回までという全体レートを concurrency に関係なく守る。
    sem 配下で各タスクが個別に sleep するだけだと、concurrency 個が同時に
    スリープ→同時に実行してしまい per_hour が無意味化するため、
    ロックで「次に実行してよい時刻」を直列に予約してからスリープする。
    """

    def __init__(self, per_hour: float):
        self._interval = 3600.0 / max(per_hour, 0.001)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at)
            self._next_at = start + self._interval
            delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)


class Orchestrator:
    def __init__(self, config: AppConfig):
        self.config = config
        self.db = Database(config.db.path)
        self.proxy_manager = ProxyManager(config.proxy)
        self.twofa_manager = TwoFAManager(config.twofa.encryption_key)
        self.browser = BrowserManager(
            headless=config.browser.headless,
            slow_mo=config.browser.slow_mo,
            timeout=config.browser.default_timeout,
            max_concurrent=config.browser.max_concurrent_browsers,
        )

        self.login_action = LoginAction(self.browser, self.twofa_manager, config.twofa.encryption_key)
        self.tweet_action = TweetAction(self.browser)
        self.follow_action = FollowAction(self.browser)
        self.like_action = LikeAction(self.browser)

        self.factory = AccountFactory(
            config,
            self.proxy_manager,
            self.twofa_manager,
            self.browser,
        )
        self.detector = ShadowbanDetector(self.browser)
        self.resolver = ShadowbanResolver(
            self.browser, self.login_action, self.tweet_action, self.like_action
        )
        self.search_top_checker = SearchTopChecker()
        self.warmup = WarmupScheduler(
            config.warmup,
            self.browser,
            self.login_action,
            self.tweet_action,
            self.follow_action,
            self.like_action,
            db=self.db,
        )

    async def start(self):
        await self.db.connect()
        # Browser uses lazy start — it starts on first new_context() call,
        # so warmup-only invocations don't pay the browser startup cost.
        logger.info("Orchestrator started")

    async def stop(self):
        await self.browser.stop()
        await self.db.close()
        logger.info("Orchestrator stopped")

    # ──────────────────────────────────────────
    # 生成
    # ──────────────────────────────────────────

    async def generate_accounts(self, target_count: int, concurrency: int = 3):
        """
        target_count 垢を生成する。
        concurrency: 同時に走らせるブラウザ数（プロキシ帯域に注意）
        """
        existing = await self.db.list_accounts()
        already = len(existing)
        need = max(0, target_count - already)

        if need == 0:
            logger.info(f"Already have {already} accounts, no generation needed")
            return

        logger.info(f"Generating {need} accounts (concurrency={concurrency})")
        sem = asyncio.Semaphore(concurrency)
        limiter = _RateLimiter(self.config.factory.max_per_hour)

        async def _create_one():
            # レート制限はまず全体で直列に予約 (concurrency に関係なく守られる)。
            # sem の取得はその後 — 複数タスクが同時に実行枠に入ることはあるが、
            # 「開始時刻」自体は max_per_hour に従って均等分散される。
            await limiter.wait()
            async with sem:
                account = await self.factory.create_one()
                if account:
                    aid = await self.db.add_account(account)
                    logger.success(f"Created account id={aid} @{account.username}")
                else:
                    logger.warning("Account creation failed, will retry next cycle")

        tasks = [_create_one() for _ in range(need)]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ──────────────────────────────────────────
    # 日次ウォームアップループ
    # ──────────────────────────────────────────

    async def run_warmup_cycle(self, concurrency: int = 5):
        """
        全アクティブ垢の1日分ウォームアップを並列実行。
        """
        accounts = await self.db.list_accounts()
        active = [
            a for a in accounts
            if a.status in (AccountStatus.CREATED, AccountStatus.WARMING, AccountStatus.CLEAN)
        ]
        logger.info(f"Warmup cycle: {len(active)} accounts")

        sem = asyncio.Semaphore(concurrency)

        async def _warmup_one(account: Account):
            async with sem:
                proxy = self.proxy_manager.get_warmup_proxy(account.username)
                result = await self.warmup.run_daily_actions(account, proxy_url=proxy.url)
                if "error" not in result:
                    await self.db.update_last_action(account.id)
                    if account.status == AccountStatus.CREATED:
                        await self.db.update_status(account.id, AccountStatus.WARMING)
                # ランダム間隔でスロットリング
                await asyncio.sleep(random.uniform(30, 90))

        tasks = [_warmup_one(a) for a in active]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ──────────────────────────────────────────
    # シャドウバン検査 + 解除ループ
    # ──────────────────────────────────────────

    async def run_shadowban_check(self, concurrency: int = 10):
        """
        全垢を検査し、バン状態を更新。解除アクションも自動実行。
        HARD_SHADOW の垢は 6 時間以上経過後のみ再チェック（連続叩き防止）。
        """
        accounts = await self.db.list_accounts()
        now = datetime.now(_tz.utc)
        sem = asyncio.Semaphore(concurrency)

        async def _check_one(account: Account):
            async with sem:
                # HARD_SHADOW は最終アクションから 6h 以上経過していないとスキップ
                if account.status == AccountStatus.HARD_SHADOW and account.last_action_at:
                    last = account.last_action_at
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=_tz.utc)
                    if (now - last).total_seconds() < 6 * 3600:
                        logger.debug(f"[@{account.username}] HARD_SHADOW cooling, skip check")
                        return

                ban_status = await self.detector.check(account.username)
                await self.db.update_shadow_score(account.id, ban_status.shadow_score)
                new_status = ban_status.status

                if new_status != account.status:
                    await self.db.update_status(account.id, new_status)
                    logger.info(f"[@{account.username}] status {account.status} → {new_status}")

                if not ban_status.is_clean:
                    proxy = self.proxy_manager.get_proxy_for_account(account.username)
                    await self.resolver.resolve(account, ban_status, proxy_url=proxy.url)

                # CLEAN に戻ったら検索TOP確認
                elif new_status == AccountStatus.CLEAN and account.status != AccountStatus.CLEAN:
                    await asyncio.sleep(random.uniform(3, 8))
                    top = await self.search_top_checker.check(account.username)
                    if not top.is_visible:
                        logger.warning(
                            f"[@{account.username}] ban score clean but search top not visible "
                            f"(live={top.appears_in_live_search} typeahead={top.appears_in_typeahead})"
                        )

                await asyncio.sleep(random.uniform(2.0, 6.0))

        tasks = [_check_one(a) for a in accounts]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run_search_top_check(self, concurrency: int = 8):
        """
        CLEAN / WARMING 垢を対象に検索TOP可視性を確認。
        visibility_score < 0.6 の場合は soft_shadow 相当の回復を実行。
        """
        accounts = await self.db.list_accounts()
        targets = [
            a for a in accounts
            if a.status in (AccountStatus.CLEAN, AccountStatus.WARMING)
        ]
        logger.info(f"Search top check: {len(targets)} accounts")
        sem = asyncio.Semaphore(concurrency)

        async def _check_one(account: Account):
            async with sem:
                result = await self.search_top_checker.check(account.username)
                if result.visibility_score < 0.6:
                    logger.warning(
                        f"[@{account.username}] low search visibility={result.visibility_score:.2f}, "
                        f"triggering light recovery"
                    )
                    from .detector import BanStatus
                    soft = BanStatus(search_ban=True, source="search_top_check")
                    proxy = self.proxy_manager.get_proxy_for_account(account.username)
                    await self.resolver.resolve(account, soft, proxy_url=proxy.url)
                await asyncio.sleep(random.uniform(1.5, 4.0))

        tasks = [_check_one(a) for a in targets]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ──────────────────────────────────────────
    # 卒業チェック（21日 → CLEAN = 販売可能）
    # ──────────────────────────────────────────

    async def run_graduation_check(self, concurrency: int = 5):
        """
        min_days_before_sale 日以上経過した WARMING 垢に卒業試験を実施。

        合格条件:
          1. shadowban スコア 0（3ソース全てクリーン）
          2. 検索TOP可視性スコア ≥ 0.6（live search OR typeahead 通過）

        合格 → AccountStatus.CLEAN（販売可能）
        不合格 → WARMING のまま + 回復アクション → 次サイクルで再試験
        """
        from .detector import BanStatus as _BS

        accounts = await self.db.list_accounts()
        now = datetime.now(_tz.utc)
        min_days = self.config.warmup.min_days_before_sale

        candidates = [
            a for a in accounts
            if a.status == AccountStatus.WARMING
            and _account_age_days(a, now) >= min_days
        ]

        if not candidates:
            logger.info("Graduation check: no candidates")
            return

        logger.info(
            f"Graduation check: {len(candidates)} candidates "
            f"(≥{min_days}d old, still WARMING)"
        )
        sem = asyncio.Semaphore(concurrency)

        async def _graduate(account: Account):
            async with sem:
                age = _account_age_days(account, now)
                proxy = self.proxy_manager.get_proxy_for_account(account.username)

                # ── ステップ1: shadowban チェック ────────────────
                ban = await self.detector.check(account.username)
                await self.db.update_shadow_score(account.id, ban.shadow_score)

                if not ban.is_clean:
                    logger.info(
                        f"[@{account.username}] graduation FAIL: "
                        f"shadowban score={ban.shadow_score:.2f} age={age}d — recovery triggered"
                    )
                    await self.db.update_status(account.id, ban.status)
                    await self.resolver.resolve(account, ban, proxy_url=proxy.url)
                    return

                # ── ステップ2: 検索TOP可視性チェック ─────────────
                await asyncio.sleep(random.uniform(3, 8))
                top = await self.search_top_checker.check(account.username)

                if top.visibility_score < 0.6:
                    logger.warning(
                        f"[@{account.username}] graduation FAIL: "
                        f"search visibility={top.visibility_score:.2f} age={age}d "
                        f"(live={top.appears_in_live_search} typeahead={top.appears_in_typeahead}) "
                        f"— light recovery triggered"
                    )
                    soft = _BS(search_ban=True, source="graduation_visibility_fail")
                    await self.resolver.resolve(account, soft, proxy_url=proxy.url)
                    return

                # ── 合格: CLEAN に昇格 ────────────────────────────
                await self.db.update_status(account.id, AccountStatus.CLEAN)
                await self.db.update_shadow_score(account.id, 0.0)
                logger.success(
                    f"[@{account.username}] ★ GRADUATED → CLEAN (sale ready) "
                    f"age={age}d "
                    f"visibility={top.visibility_score:.2f} "
                    f"live={top.appears_in_live_search} typeahead={top.appears_in_typeahead}"
                )
                await asyncio.sleep(random.uniform(2, 5))

        tasks = [_graduate(a) for a in candidates]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ──────────────────────────────────────────
    # フル自動ループ（デーモン用）
    # ──────────────────────────────────────────

    async def run_forever(
        self,
        target_count: int = 100,
        generation_concurrency: int = 2,
        warmup_concurrency: int = 5,
        check_interval_hours: float = 6.0,
    ):
        """
        永続ループ:
        1. 不足垢を補充
        2. ウォームアップ実行
        3. シャドウバン確認（3ソース多数決）
        4. 卒業チェック（21日経過 WARMING → CLEAN）
        5. 検索TOP可視性確認（CLEAN 垢 / 2サイクルに1回）
        6. 次のサイクルまで待機
        """
        logger.info(f"run_forever: target={target_count}, check_interval={check_interval_hours}h")
        cycle = 0

        while True:
            try:
                cycle += 1
                logger.info(f"=== Cycle #{cycle} start ===")

                # 1. 補充
                await self.generate_accounts(target_count, concurrency=generation_concurrency)

                # 2. ウォームアップ
                await self.run_warmup_cycle(concurrency=warmup_concurrency)

                # 3. シャドウバン確認
                await self.run_shadowban_check()

                # 4. 卒業チェック（毎サイクル）
                await self.run_graduation_check()

                # 5. 検索TOP確認（2サイクルに1回）
                if cycle % 2 == 0:
                    await self.run_search_top_check()

                # 6. 待機
                wait = check_interval_hours * 3600
                jitter = random.uniform(-300, 300)
                logger.info(f"=== Cycle #{cycle} done, sleeping {(wait + jitter) / 3600:.1f}h ===")
                await asyncio.sleep(wait + jitter)

            except asyncio.CancelledError:
                logger.info("Orchestrator loop cancelled")
                break
            except Exception as e:
                logger.exception(f"Orchestrator loop error: {e}")
                await asyncio.sleep(60)
