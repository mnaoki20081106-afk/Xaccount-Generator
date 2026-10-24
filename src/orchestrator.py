"""
100垢を並列で管理するオーケストレーター。
各アカウントのライフサイクル（生成→ウォームアップ→運用→バン解除）を制御する。
"""
import asyncio
import random
import time
from datetime import datetime, timezone
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
from .detector import ShadowbanDetector, ShadowbanResolver
from .graduation import GraduationExaminer
from .observers import ObserverPool

_ALL = 100000  # list_accounts の既定 limit=100 だと101垢目以降が処理されないため


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
    def __init__(self, config: AppConfig, proxy_manager: ProxyManager | None = None):
        self.config = config
        self.db = Database(config.db.path)
        # APIサーバーと同じインスタンスを共有できる（別インスタンスだと
        # proxy_sessions.json を互いに上書きしてしまうため）。
        self.proxy_manager = proxy_manager or ProxyManager(config.proxy)
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
        self.observers = ObserverPool(self.db, self.proxy_manager, config.twofa.encryption_key)
        self.detector = ShadowbanDetector(self.browser, self.observers, client_factory=self._api_client)
        self.resolver = ShadowbanResolver(
            self.browser, self.login_action, self.tweet_action, self.like_action
        )
        self.warmup = WarmupScheduler(
            config.warmup,
            self.browser,
            self.login_action,
            self.tweet_action,
            self.follow_action,
            self.like_action,
            db=self.db,
        )
        self.graduation = GraduationExaminer(self.db, self.proxy_manager, config.warmup, self.observers)

    @staticmethod
    def _api_client(account: Account, proxy_url: str | None):
        from .actions.x_api import XApiClient
        return XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url)

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
        accounts = await self.db.list_accounts(limit=_ALL)
        active = [
            a for a in accounts
            if a.status in (AccountStatus.CREATED, AccountStatus.WARMING, AccountStatus.CLEAN)
        ]
        logger.info(f"Warmup cycle: {len(active)} accounts")

        sem = asyncio.Semaphore(concurrency)

        async def _warmup_one(account: Account):
            async with sem:
                if await self.warmup.due_reason(account):
                    return  # 今は動かさない日/時間帯。IP確認の通信も無駄なので先に弾く
                # 生成時と同じセッション(proxy_id)で、実際に同じ出口IPか確認してから動かす。
                # 想定外にIPが変わっていたら記録し、その日の行動は見送る（卒業判定にも反映される）。
                ipc = await self.proxy_manager.verify_account_ip(account)
                if not ipc["ok"]:
                    logger.warning(f"[@{account.username}] IP check {ipc['kind']} — warmup skipped ({ipc})")
                    await self.db.log_action(account.id, "ip_check", f"{ipc['kind']}:{ipc.get('ip')}")
                    return
                proxy = self.proxy_manager.proxy_for_account(account)
                result = await self.warmup.run_daily_actions(account, proxy_url=proxy.url)
                if result.get("flag") == "suspended":
                    await self.db.update_status(account.id, AccountStatus.SUSPENDED)
                elif result.get("flag") == "locked":
                    await self.db.update_status(account.id, AccountStatus.LOCKED)
                elif "error" not in result and not result.get("skipped"):
                    await self.db.update_last_action(account.id)
                    if account.status == AccountStatus.CREATED:
                        await self.db.update_status(account.id, AccountStatus.WARMING)
                if result.get("skipped"):
                    return
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
        """
        # SOLD/凍結/ロックは対象外。以前は全垢を検査して status を上書きしていたため、
        # 売却済み(SOLD)がCLEANに戻って再販売され、作成直後の垢も即CLEAN(販売可)になっていた。
        skip = {AccountStatus.SOLD, AccountStatus.SUSPENDED, AccountStatus.LOCKED}
        accounts = [a for a in await self.db.list_accounts(limit=_ALL) if a.status not in skip]
        sem = asyncio.Semaphore(concurrency)

        async def _check_one(account: Account):
            async with sem:
                ban_status = await self.detector.check(account.username)
                if ban_status.unknown:
                    return  # 判定できなかった垢は状態を動かさない
                await self.db.update_shadow_score(account.id, ban_status.shadow_score)
                new_status = ban_status.status
                # ウォームアップ中の垢は、検査で「問題なし」でもCLEAN(販売可)に昇格させない。
                # CLEANへの昇格は経過日数を満たしてから。
                if new_status == AccountStatus.CLEAN and account.status in (
                    AccountStatus.CREATED, AccountStatus.WARMING
                ):
                    age = (datetime.now(timezone.utc) - (
                        account.created_at if account.created_at.tzinfo
                        else account.created_at.replace(tzinfo=timezone.utc))).days
                    if age < self.config.warmup.min_days_before_sale:
                        new_status = account.status

                if new_status != account.status:
                    await self.db.update_status(account.id, new_status)
                    logger.info(f"[@{account.username}] status {account.status} → {new_status}")

                if not ban_status.is_clean:
                    proxy = self.proxy_manager.proxy_for_account(account)
                    await self.resolver.resolve(account, ban_status, proxy_url=proxy.url)

                await asyncio.sleep(random.uniform(2.0, 5.0))

        tasks = [_check_one(a) for a in accounts]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ──────────────────────────────────────────
    # 卒業テスト
    # ──────────────────────────────────────────

    async def run_graduation_cycle(self, concurrency: int = 2) -> dict:
        """CLEAN（検査で問題なし）かつ経過日数を満たした垢に卒業テストを回す。"""
        accounts = await self.db.list_accounts(status=AccountStatus.CLEAN, limit=_ALL)
        sem = asyncio.Semaphore(concurrency)
        tally: dict[str, int] = {}

        async def _one(account: Account):
            async with sem:
                try:
                    report = await self.graduation.examine(account)
                except Exception as e:
                    logger.exception(f"[@{account.username}] graduation error: {e}")
                    return
                tally[report.stage] = tally.get(report.stage, 0) + 1
                await asyncio.sleep(random.uniform(5, 20))

        await asyncio.gather(*[_one(a) for a in accounts], return_exceptions=True)
        logger.info(f"Graduation cycle: {tally}")
        return tally

    # ──────────────────────────────────────────
    # フル自動ループ（デーモン用）
    # ──────────────────────────────────────────

    async def run_forever(
        self,
        target_count: int = 100,
        generation_concurrency: int = 2,
        warmup_concurrency: int = 5,
        check_interval_hours: float = 6.0,
        warmup_interval_hours: float = 1.0,
    ):
        """
        永続ループ（warmup_interval_hours ごとに回る）:
        - 毎回:               不足垢の補充 / ウォームアップ（垢ごとの予定セッションが来ていれば実行）
        - check_interval ごと: シャドウバン確認 / 卒業テスト
        ウォームアップを1時間ごとに回すのは、1日の行動を垢ごとにずらした複数セッションに
        分けているため（6時間に1回だと予定時刻に合わせられない）。
        """
        logger.info(
            f"run_forever: target={target_count}, warmup every {warmup_interval_hours}h, "
            f"checks every {check_interval_hours}h"
        )
        last_check = 0.0

        while True:
            try:
                await self.generate_accounts(target_count, concurrency=generation_concurrency)
                await self.run_warmup_cycle(concurrency=warmup_concurrency)

                if time.monotonic() - last_check >= check_interval_hours * 3600 or last_check == 0.0:
                    await self.run_shadowban_check()
                    await self.run_graduation_cycle()
                    last_check = time.monotonic()

                wait = warmup_interval_hours * 3600
                jitter = random.uniform(-300, 300)
                logger.info(f"Cycle done, sleeping {(wait + jitter) / 3600:.2f}h")
                await asyncio.sleep(max(60.0, wait + jitter))

            except asyncio.CancelledError:
                logger.info("Orchestrator loop cancelled")
                break
            except Exception as e:
                logger.exception(f"Orchestrator loop error: {e}")
                await asyncio.sleep(60)
