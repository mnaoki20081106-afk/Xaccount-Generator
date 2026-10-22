"""
アカウントウォームアップスケジューラ。

auth_token + ct0 が DB にある垢は API 経由で実行（Chromium 不要）。
トークンがない場合のみブラウザフォールバック。

API モード: メモリ ~20MB、プロキシ通信 ~50KB/アクション
ブラウザモード: メモリ ~300MB、プロキシ通信 ~2-5MB/アクション

フェーズ体系（垢齢ベース）:
  SEED    (0〜3日):   タイムライン閲覧のみ — 新垢シグナルを最小化
  OBSERVE (3〜7日):   閲覧 + 検索ブラウズ — 人間らしい興味関心を形成
  ENGAGE  (7〜14日):  いいね + フォロー + プロフィール訪問
  ACTIVE  (14〜21日): ツイート + リツイート + フル操作
  MATURE  (21日〜):   全操作フル解放 + 引用ツイート
"""
import asyncio
import random
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from loguru import logger

from ..models import Account, AccountStatus
from ..actions.login import LoginAction
from ..actions.tweet import TweetAction
from ..actions.follow import FollowAction
from ..actions.like import LikeAction
from ..utils.browser import BrowserManager
from ..config import WarmupConfig
from .content import (
    random_tweet, random_quote_comment,
    random_search_query, random_profile_target,
)

try:
    from ..actions.x_api import XApiClient, HAS_CURL_CFFI
except ImportError:
    XApiClient = None
    HAS_CURL_CFFI = False


# ──────────────────────────────────────────────────────────────
# フェーズ定義
# ──────────────────────────────────────────────────────────────

class WarmupPhase(str, Enum):
    SEED    = "seed"     # day 0-3:  閲覧のみ
    OBSERVE = "observe"  # day 3-7:  閲覧 + 検索
    ENGAGE  = "engage"   # day 7-14: いいね + フォロー
    ACTIVE  = "active"   # day 14-21: ツイート解禁
    MATURE  = "mature"   # day 21+:  全機能フル


# action → 1日の上限値
PHASE_LIMITS: dict[WarmupPhase, dict[str, int]] = {
    WarmupPhase.SEED:    {"tweets": 0, "likes": 0,  "follows": 0,  "retweets": 0, "quotes": 0, "search": 2, "browse": 4, "profile_visit": 2},
    WarmupPhase.OBSERVE: {"tweets": 0, "likes": 3,  "follows": 2,  "retweets": 0, "quotes": 0, "search": 4, "browse": 3, "profile_visit": 4},
    WarmupPhase.ENGAGE:  {"tweets": 2, "likes": 10, "follows": 6,  "retweets": 1, "quotes": 0, "search": 3, "browse": 2, "profile_visit": 3},
    WarmupPhase.ACTIVE:  {"tweets": 5, "likes": 18, "follows": 12, "retweets": 3, "quotes": 1, "search": 2, "browse": 1, "profile_visit": 2},
    WarmupPhase.MATURE:  {"tweets": 8, "likes": 25, "follows": 15, "retweets": 5, "quotes": 3, "search": 2, "browse": 1, "profile_visit": 2},
}

# アクション間の自然な待機レンジ（秒）
_ACTION_PAUSE = {
    "browse":       (20, 60),
    "search":       (15, 45),
    "profile_visit":(5,  20),
    "likes":        (4,  12),
    "follows":      (8,  25),
    "retweets":     (10, 30),
    "tweets":       (30, 90),
    "quotes":       (25, 70),
}

# JST 09:00〜23:00 = UTC 00:00〜14:00
ACTIVE_HOURS_UTC = set(range(0, 14))


def get_phase(created_at: datetime) -> WarmupPhase:
    now = datetime.now(timezone.utc)
    ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    age_days = (now - ca).days
    if age_days < 3:
        return WarmupPhase.SEED
    elif age_days < 7:
        return WarmupPhase.OBSERVE
    elif age_days < 14:
        return WarmupPhase.ENGAGE
    elif age_days < 21:
        return WarmupPhase.ACTIVE
    else:
        return WarmupPhase.MATURE


def is_active_hour() -> bool:
    return datetime.now(timezone.utc).hour in ACTIVE_HOURS_UTC


async def _jitter_sleep(action: str) -> None:
    lo, hi = _ACTION_PAUSE.get(action, (5, 20))
    # ±20% のゆらぎを足す（より人間的）
    base = random.uniform(lo, hi)
    jitter = base * random.uniform(-0.2, 0.3)
    await asyncio.sleep(max(2.0, base + jitter))


# ──────────────────────────────────────────────────────────────
# スケジューラ
# ──────────────────────────────────────────────────────────────

class WarmupScheduler:
    def __init__(
        self,
        config: WarmupConfig,
        browser: BrowserManager,
        login_action: LoginAction,
        tweet_action: TweetAction,
        follow_action: FollowAction,
        like_action: LikeAction,
        follow_targets: Optional[list[str]] = None,
        tweet_templates: Optional[list[str]] = None,
        db=None,
    ):
        self.config = config
        self.browser = browser
        self.login = login_action
        self.tweet = tweet_action
        self.follow = follow_action
        self.like = like_action
        self.follow_targets = follow_targets or []
        self.tweet_templates = tweet_templates
        self.db = db

    # ──── DB ヘルパー ────────────────────────────────────────

    async def _count_today(self, account_id: int, action: str) -> int:
        if self.db:
            try:
                return await self.db.get_action_count_today(account_id, action)
            except Exception:
                pass
        return 0

    async def _log(self, account_id: int, action: str, detail: str = "") -> None:
        if self.db:
            try:
                await self.db.log_action(account_id, action, detail)
            except Exception:
                pass

    async def _can_do(self, account_id: int, action: str, phase: WarmupPhase) -> bool:
        limit = PHASE_LIMITS[phase].get(action, 0)
        if limit <= 0:
            return False
        done = await self._count_today(account_id, action)
        return done < limit

    def _pick_tweet(self) -> str:
        if self.tweet_templates:
            return random.choice(self.tweet_templates)
        return random_tweet()

    # ──── メインエントリ ─────────────────────────────────────

    async def run_daily_actions(
        self, account: Account, proxy_url: Optional[str] = None
    ) -> dict:
        if not is_active_hour():
            logger.debug(f"[{account.username}] outside active hours, skip")
            return {"skipped": True, "reason": "inactive_hour"}

        phase = get_phase(account.created_at)
        logger.info(f"[{account.username}] warmup phase={phase.value}")

        if account.auth_token and account.ct0 and HAS_CURL_CFFI:
            return await self._run_api(account, phase, proxy_url)
        else:
            return await self._run_browser(account, phase, proxy_url)

    # ──── API モード ─────────────────────────────────────────

    async def _run_api(
        self, account: Account, phase: WarmupPhase, proxy_url: Optional[str]
    ) -> dict:
        limits = PHASE_LIMITS[phase]
        results: dict = {
            "mode": "api", "phase": phase.value,
            "tweets": 0, "likes": 0, "follows": 0,
            "retweets": 0, "quotes": 0, "search": 0, "browse": 0,
        }

        async with XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url) as client:
            if not await client.verify():
                logger.warning(f"[{account.username}] API token invalid, falling back to browser")
                return await self._run_browser(account, phase, proxy_url)

            # ── セッション開幕: ブラウズ ──────────────────────
            if await self._can_do(account.id, "browse", phase):
                ok = await client.browse(count=random.randint(8, 20))
                if ok:
                    await self._log(account.id, "browse", "session_open_api")
                    results["browse"] += 1
                await _jitter_sleep("browse")

            # ── 検索ブラウズ ───────────────────────────────────
            search_n = random.randint(1, max(1, limits["search"]))
            for _ in range(search_n):
                if not await self._can_do(account.id, "search", phase):
                    break
                q = random_search_query()
                ok = await client.search_browse(q)
                if ok:
                    await self._log(account.id, "search", q)
                    results["search"] += 1
                await _jitter_sleep("search")

            # ── いいね ─────────────────────────────────────────
            target_likes = random.randint(
                max(1, limits["likes"] // 3), limits["likes"]
            )
            if target_likes > 0 and await self._can_do(account.id, "likes", phase):
                liked = await client.like_random_from_timeline(count=target_likes)
                if liked > 0:
                    await self._log(account.id, "likes", str(liked))
                results["likes"] = liked
                await _jitter_sleep("likes")

            # ── フォロー ───────────────────────────────────────
            if limits["follows"] > 0 and await self._can_do(account.id, "follows", phase):
                n = random.randint(1, min(3, limits["follows"]))
                if random.random() < 0.6:
                    followed = await client.follow_suggestions(count=n)
                elif self.follow_targets:
                    target = random.choice(self.follow_targets)
                    followed = 1 if await client.follow(target) else 0
                else:
                    followed = 0
                if followed > 0:
                    await self._log(account.id, "follows", str(followed))
                    results["follows"] += followed
                await _jitter_sleep("follows")

            # ── リツイート ─────────────────────────────────────
            if limits["retweets"] > 0 and await self._can_do(account.id, "retweets", phase):
                if random.random() < 0.55:
                    rt_id = await client.get_retweetable_id()
                    if rt_id and await client.retweet(rt_id):
                        await self._log(account.id, "retweets", rt_id)
                        results["retweets"] += 1
                await _jitter_sleep("retweets")

            # ── ツイート ───────────────────────────────────────
            if limits["tweets"] > 0 and await self._can_do(account.id, "tweets", phase):
                if random.random() < 0.65:
                    text = self._pick_tweet()
                    if await client.tweet(text):
                        await self._log(account.id, "tweets", text[:40])
                        results["tweets"] += 1
                await _jitter_sleep("tweets")

            # ── 引用ツイート (MATURE のみ) ─────────────────────
            if limits["quotes"] > 0 and await self._can_do(account.id, "quotes", phase):
                if random.random() < 0.45:
                    qt_id = await client.get_retweetable_id()
                    if qt_id:
                        comment = random_quote_comment()
                        if await client.quote_tweet(qt_id, comment):
                            await self._log(account.id, "quotes", qt_id[:20])
                            results["quotes"] += 1
                await _jitter_sleep("quotes")

        logger.info(
            f"[{account.username}] warmup(api) done phase={phase.value}: "
            f"tw={results['tweets']} lk={results['likes']} "
            f"fl={results['follows']} rt={results['retweets']} qt={results['quotes']}"
        )
        return results

    # ──── ブラウザフォールバック ──────────────────────────────

    async def _run_browser(
        self, account: Account, phase: WarmupPhase, proxy_url: Optional[str]
    ) -> dict:
        limits = PHASE_LIMITS[phase]
        results: dict = {
            "mode": "browser", "phase": phase.value,
            "tweets": 0, "likes": 0, "follows": 0,
            "retweets": 0, "search": 0, "browse": 0,
        }

        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            logger.warning(f"[{account.username}] browser login failed")
            return {"error": "login_failed"}

        try:
            page = await self.browser.new_page(context)

            # ── セッション開幕: ブラウズ ──────────────────────
            if await self._can_do(account.id, "browse", phase):
                await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
                scroll_n = random.randint(4, 10)
                for _ in range(scroll_n):
                    await page.mouse.wheel(0, random.randint(200, 600))
                    await asyncio.sleep(random.uniform(1.5, 4.0))
                await self._log(account.id, "browse", "session_open_browser")
                results["browse"] += 1
                await _jitter_sleep("browse")

            # ── 検索ブラウズ ───────────────────────────────────
            for _ in range(random.randint(1, max(1, limits["search"]))):
                if not await self._can_do(account.id, "search", phase):
                    break
                q = random_search_query()
                try:
                    await page.goto(
                        f"https://x.com/search?q={q}&src=typed_query&f=live",
                        wait_until="domcontentloaded", timeout=25000,
                    )
                    await asyncio.sleep(random.uniform(3, 8))
                    for _ in range(random.randint(2, 4)):
                        await page.mouse.wheel(0, random.randint(200, 400))
                        await asyncio.sleep(random.uniform(1, 3))
                    await self._log(account.id, "search", q)
                    results["search"] += 1
                except Exception as e:
                    logger.debug(f"[{account.username}] search browse error: {e}")
                await _jitter_sleep("search")

            # ── プロフィール訪問 ───────────────────────────────
            if limits["profile_visit"] > 0 and await self._can_do(account.id, "profile_visit", phase):
                target = random_profile_target()
                try:
                    await page.goto(f"https://x.com/{target}", wait_until="domcontentloaded", timeout=25000)
                    await asyncio.sleep(random.uniform(3, 7))
                    for _ in range(random.randint(1, 3)):
                        await page.mouse.wheel(0, random.randint(150, 400))
                        await asyncio.sleep(random.uniform(1, 3))
                    await self._log(account.id, "profile_visit", target)
                except Exception as e:
                    logger.debug(f"[{account.username}] profile visit error: {e}")
                await _jitter_sleep("profile_visit")

            await page.close()

            # ── いいね ─────────────────────────────────────────
            target_likes = random.randint(
                max(1, limits["likes"] // 3), limits["likes"]
            )
            if target_likes > 0 and await self._can_do(account.id, "likes", phase):
                liked = await self.like.like_timeline_random(context, account, count=target_likes)
                if liked > 0:
                    await self._log(account.id, "likes", str(liked))
                results["likes"] = liked
                await _jitter_sleep("likes")

            # ── フォロー ───────────────────────────────────────
            if limits["follows"] > 0 and await self._can_do(account.id, "follows", phase):
                n = random.randint(1, min(3, limits["follows"]))
                if random.random() < 0.6:
                    followed = await self.follow.follow_suggestions(context, account, count=n)
                    if followed > 0:
                        await self._log(account.id, "follows", f"suggestions:{followed}")
                        results["follows"] += followed
                elif self.follow_targets:
                    target = random.choice(self.follow_targets)
                    if await self.follow.follow(context, target, account):
                        await self._log(account.id, "follows", target)
                        results["follows"] += 1
                await _jitter_sleep("follows")

            # ── ツイート ───────────────────────────────────────
            if limits["tweets"] > 0 and await self._can_do(account.id, "tweets", phase):
                if random.random() < 0.65:
                    text = self._pick_tweet()
                    ok = await self.tweet.post(context, text, account)
                    if ok:
                        await self._log(account.id, "tweets", text[:40])
                        results["tweets"] += 1
                await _jitter_sleep("tweets")

        finally:
            await context.close()

        logger.info(
            f"[{account.username}] warmup(browser) done phase={phase.value}: "
            f"tw={results['tweets']} lk={results['likes']} "
            f"fl={results['follows']} s={results['search']}"
        )
        return results
