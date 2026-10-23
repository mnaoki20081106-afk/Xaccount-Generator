"""
アカウントウォームアップスケジューラ。

auth_token + ct0 が DB にある垢は API 経由で実行（Chromium 不要）。
トークンがない場合のみブラウザフォールバック。

API モード: メモリ ~20MB、プロキシ通信 ~50KB/アクション
ブラウザモード: メモリ ~300MB、プロキシ通信 ~2-5MB/アクション
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
from .content import random_tweet

try:
    from ..actions.x_api import XApiClient, HAS_CURL_CFFI
except ImportError:
    XApiClient = None
    HAS_CURL_CFFI = False


class WarmupPhase(str, Enum):
    OBSERVE = "observe"    # week1: 閲覧のみ
    ENGAGE = "engage"      # week2: いいね・フォロー
    ACTIVE = "active"      # week3+: 投稿・全行動


PHASE_LIMITS = {
    WarmupPhase.OBSERVE: {"tweets": 0, "likes": 5,  "follows": 3,  "retweets": 0, "browse": 3},
    WarmupPhase.ENGAGE:  {"tweets": 3, "likes": 10, "follows": 8,  "retweets": 2, "browse": 2},
    WarmupPhase.ACTIVE:  {"tweets": 8, "likes": 20, "follows": 15, "retweets": 5, "browse": 1},
}


def get_phase(created_at: datetime) -> WarmupPhase:
    now = datetime.now(timezone.utc)
    ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    age_days = (now - ca).days
    if age_days < 7:
        return WarmupPhase.OBSERVE
    elif age_days < 14:
        return WarmupPhase.ENGAGE
    else:
        return WarmupPhase.ACTIVE


def is_active_hour(active_hours_utc: list[int]) -> bool:
    return datetime.now(timezone.utc).hour in active_hours_utc


def should_run_graduation(account: Account, config: WarmupConfig) -> bool:
    """
    卒業テスト対象かどうかを返す。
    条件: WARMING / CLEAN 状態で ACTIVE フェーズに入ってから
          graduation_min_active_days 日以上経過している。
    """
    if account.status not in (AccountStatus.WARMING, AccountStatus.CLEAN):
        return False
    if get_phase(account.created_at) != WarmupPhase.ACTIVE:
        return False
    now = datetime.now(timezone.utc)
    ca = account.created_at if account.created_at.tzinfo else account.created_at.replace(tzinfo=timezone.utc)
    # ACTIVE フェーズ開始日 = 作成から14日後
    active_start = ca.replace() if False else ca  # type annotation help
    days_in_active = (now - ca).days - 14
    return days_in_active >= config.graduation_min_active_days


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

    async def _count_today(self, account_id: int, action: str) -> int:
        if self.db:
            try:
                return await self.db.get_action_count_today(account_id, action)
            except Exception:
                pass
        return 0

    async def _log(self, account_id: int, action: str, detail: str = ""):
        if self.db:
            try:
                await self.db.log_action(account_id, action, detail)
            except Exception:
                pass

    async def _can_do(self, account_id: int, action: str, phase: WarmupPhase) -> bool:
        limit = PHASE_LIMITS[phase].get(action, 0)
        if limit <= 0:
            return False
        return await self._count_today(account_id, action) < limit

    # ────────────────────────────────────────────────────────
    # メインエントリ
    # ────────────────────────────────────────────────────────

    async def run_daily_actions(
        self, account: Account, proxy_url: Optional[str] = None
    ) -> dict:
        if not is_active_hour(self.config.active_hours_utc):
            logger.debug(f"[{account.username}] outside active hours, skip")
            return {"skipped": True, "reason": "inactive_hour"}

        phase = get_phase(account.created_at)
        graduation_ready = should_run_graduation(account, self.config)

        if account.auth_token and account.ct0 and HAS_CURL_CFFI:
            result = await self._run_api(account, phase, proxy_url)
        else:
            result = await self._run_browser(account, phase, proxy_url)

        result["graduation_ready"] = graduation_ready
        return result

    # ────────────────────────────────────────────────────────
    # API モード（軽量・高速）
    # ────────────────────────────────────────────────────────

    async def _run_api(
        self, account: Account, phase: WarmupPhase, proxy_url: Optional[str]
    ) -> dict:
        limits = PHASE_LIMITS[phase]
        results = {"mode": "api", "phase": phase.value, "tweets": 0, "likes": 0, "follows": 0, "browse": 0}

        async with XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url) as client:
            if not await client.verify():
                logger.warning(f"[{account.username}] API token invalid, falling back to browser")
                return await self._run_browser(account, phase, proxy_url)

            if phase == WarmupPhase.OBSERVE:
                if await self._can_do(account.id, "browse", phase):
                    ok = await client.browse(count=random.randint(10, 20))
                    if ok:
                        await self._log(account.id, "browse", "observe_api")
                        results["browse"] += 1
                    await asyncio.sleep(random.uniform(30, 90))

            target_likes = random.randint(max(1, limits["likes"] // 2), limits["likes"])
            if target_likes > 0 and await self._can_do(account.id, "likes", phase):
                liked = await client.like_random_from_timeline(count=target_likes)
                if liked > 0:
                    await self._log(account.id, "likes", str(liked))
                results["likes"] = liked
                await asyncio.sleep(random.uniform(10, 30))

            if limits["follows"] > 0 and await self._can_do(account.id, "follows", phase):
                if random.random() < 0.5:
                    n = random.randint(1, min(3, limits["follows"]))
                    followed = await client.follow_suggestions(count=n)
                    if followed > 0:
                        await self._log(account.id, "follows", f"suggestions_api:{followed}")
                        results["follows"] += followed
                elif self.follow_targets:
                    target = random.choice(self.follow_targets)
                    if await client.follow(target):
                        await self._log(account.id, "follows", target)
                        results["follows"] += 1
                await asyncio.sleep(random.uniform(10, 25))

            if limits["tweets"] > 0 and await self._can_do(account.id, "tweets", phase):
                if random.random() < 0.6:
                    text = (
                        random.choice(self.tweet_templates)
                        if self.tweet_templates
                        else random_tweet()
                    )
                    if await client.tweet(text):
                        await self._log(account.id, "tweets", text[:40])
                        results["tweets"] += 1
                    await asyncio.sleep(random.uniform(15, 45))

        logger.info(f"[{account.username}] warmup(api) done phase={phase.value}: {results}")
        return results

    # ────────────────────────────────────────────────────────
    # ブラウザフォールバック（トークンなし垢 or API 失敗時）
    # ────────────────────────────────────────────────────────

    async def _run_browser(
        self, account: Account, phase: WarmupPhase, proxy_url: Optional[str]
    ) -> dict:
        limits = PHASE_LIMITS[phase]
        results = {"mode": "browser", "phase": phase.value, "tweets": 0, "likes": 0, "follows": 0, "browse": 0}

        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            logger.warning(f"[{account.username}] browser login failed")
            return {"error": "login_failed"}

        try:
            if phase == WarmupPhase.OBSERVE:
                if await self._can_do(account.id, "browse", phase):
                    ok = await self.like.browse_timeline(
                        context, account, scroll_count=random.randint(4, 8)
                    )
                    if ok:
                        await self._log(account.id, "browse", "observe_browser")
                        results["browse"] += 1
                    await asyncio.sleep(random.uniform(30, 90))

            target_likes = random.randint(max(1, limits["likes"] // 2), limits["likes"])
            if target_likes > 0 and await self._can_do(account.id, "likes", phase):
                liked = await self.like.like_timeline_random(context, account, count=target_likes)
                if liked > 0:
                    await self._log(account.id, "likes", str(liked))
                results["likes"] = liked
                await asyncio.sleep(random.uniform(10, 30))

            if limits["follows"] > 0 and await self._can_do(account.id, "follows", phase):
                if random.random() < 0.5:
                    n = random.randint(1, min(3, limits["follows"]))
                    followed = await self.follow.follow_suggestions(context, account, count=n)
                    if followed > 0:
                        await self._log(account.id, "follows", f"suggestions_browser:{followed}")
                        results["follows"] += followed
                elif self.follow_targets:
                    target = random.choice(self.follow_targets)
                    ok = await self.follow.follow(context, target, account)
                    if ok:
                        await self._log(account.id, "follows", target)
                        results["follows"] += 1
                await asyncio.sleep(random.uniform(10, 25))

            if limits["tweets"] > 0 and await self._can_do(account.id, "tweets", phase):
                if random.random() < 0.6:
                    text = (
                        random.choice(self.tweet_templates)
                        if self.tweet_templates
                        else random_tweet()
                    )
                    ok = await self.tweet.post(context, text, account)
                    if ok:
                        await self._log(account.id, "tweets", text[:40])
                        results["tweets"] += 1
                    await asyncio.sleep(random.uniform(15, 45))

        finally:
            await context.close()

        logger.info(f"[{account.username}] warmup(browser) done phase={phase.value}: {results}")
        return results
