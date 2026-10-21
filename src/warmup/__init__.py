"""
アカウントウォームアップスケジューラ。
新垢は3週間かけて段階的に人間らしい行動パターンを積み上げる。
フェーズ: OBSERVE (閲覧のみ) → ENGAGE (いいね・フォロー) → ACTIVE (投稿・全行動)
カウンターは DB に永続化するため再起動で消えない。
"""
import asyncio
import random
from datetime import datetime
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


class WarmupPhase(str, Enum):
    OBSERVE = "observe"    # week1: 閲覧・いいねのみ
    ENGAGE = "engage"      # week2: リプ・少量ツイート
    ACTIVE = "active"      # week3+: 通常運用


# フェーズ別の1日上限
PHASE_LIMITS = {
    WarmupPhase.OBSERVE: {"tweets": 0, "likes": 5,  "follows": 3,  "retweets": 0, "browse": 3},
    WarmupPhase.ENGAGE:  {"tweets": 3, "likes": 10, "follows": 8,  "retweets": 2, "browse": 2},
    WarmupPhase.ACTIVE:  {"tweets": 8, "likes": 20, "follows": 15, "retweets": 5, "browse": 1},
}


def get_phase(created_at: datetime) -> WarmupPhase:
    age_days = (datetime.utcnow() - created_at).days
    if age_days < 7:
        return WarmupPhase.OBSERVE
    elif age_days < 14:
        return WarmupPhase.ENGAGE
    else:
        return WarmupPhase.ACTIVE


# 日本時間でアクティブな時間帯 (UTC+9 換算で UTC 0:00-14:00)
ACTIVE_HOURS_UTC = list(range(0, 14))  # 09:00-23:00 JST


def is_active_hour() -> bool:
    return datetime.utcnow().hour in ACTIVE_HOURS_UTC


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
        self.db = db  # Database instance — used for persistent action counters

    async def _count_today(self, account_id: int, action: str) -> int:
        if self.db:
            try:
                return await self.db.get_action_count_today(account_id, action)
            except Exception:
                pass
        return 0

    async def _log_action(self, account_id: int, action: str, detail: str = ""):
        if self.db:
            try:
                await self.db.log_action(account_id, action, detail)
            except Exception:
                pass

    async def _can_do(self, account_id: int, action: str, phase: WarmupPhase) -> bool:
        limit = PHASE_LIMITS[phase].get(action, 0)
        if limit <= 0:
            return False
        count = await self._count_today(account_id, action)
        return count < limit

    async def run_daily_actions(
        self, account: Account, proxy_url: Optional[str] = None
    ) -> dict:
        """1垢分の1日分アクションを実行して結果 dict を返す。"""
        if not is_active_hour():
            logger.debug(f"[{account.username}] outside active hours, skip")
            return {"skipped": True, "reason": "inactive_hour"}

        phase = get_phase(account.created_at)
        limits = PHASE_LIMITS[phase]
        results = {"phase": phase.value, "tweets": 0, "likes": 0, "follows": 0, "browse": 0}

        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            logger.warning(f"[{account.username}] login failed in warmup")
            return {"error": "login_failed"}

        try:
            # ── OBSERVE フェーズ: 閲覧のみ ──────────────────────────────
            if phase == WarmupPhase.OBSERVE:
                if await self._can_do(account.id, "browse", phase):
                    ok = await self.like.browse_timeline(
                        context, account,
                        scroll_count=random.randint(4, 8),
                    )
                    if ok:
                        await self._log_action(account.id, "browse", "observe_phase")
                        results["browse"] += 1
                    await self._rest(30, 90)

            # ── いいね ───────────────────────────────────────────────────
            target_likes = random.randint(max(1, limits["likes"] // 2), limits["likes"])
            if target_likes > 0 and await self._can_do(account.id, "likes", phase):
                liked = await self.like.like_timeline_random(context, account, count=target_likes)
                if liked > 0:
                    await self._log_action(account.id, "likes", str(liked))
                results["likes"] = liked
                await self._rest(10, 30)

            # ── フォロー: おすすめから（targets があればそちらも）─────────
            if limits["follows"] > 0 and await self._can_do(account.id, "follows", phase):
                if random.random() < 0.5:
                    # X のサジェストからフォロー（ターゲット一覧不要）
                    n = random.randint(1, min(3, limits["follows"]))
                    followed = await self.follow.follow_suggestions(context, account, count=n)
                    if followed > 0:
                        await self._log_action(account.id, "follows", f"suggestions:{followed}")
                        results["follows"] += followed
                    await self._rest(10, 25)
                elif self.follow_targets:
                    target = random.choice(self.follow_targets)
                    ok = await self.follow.follow(context, target, account)
                    if ok:
                        await self._log_action(account.id, "follows", target)
                        results["follows"] += 1
                    await self._rest(10, 25)

            # ── ツイート ─────────────────────────────────────────────────
            if limits["tweets"] > 0 and await self._can_do(account.id, "tweets", phase):
                if random.random() < 0.6:
                    text = (
                        random.choice(self.tweet_templates)
                        if self.tweet_templates
                        else random_tweet()
                    )
                    ok = await self.tweet.post(context, text, account)
                    if ok:
                        await self._log_action(account.id, "tweets", text[:40])
                        results["tweets"] += 1
                    await self._rest(15, 45)

        finally:
            await context.close()

        logger.info(f"[{account.username}] warmup done phase={phase.value}: {results}")
        return results

    async def _rest(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))
