"""
アカウントウォームアップスケジューラ。
新垢は3週間かけて段階的に人間らしい行動パターンを積み上げる。
"""
import asyncio
import random
from datetime import datetime, timedelta
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
    WarmupPhase.OBSERVE: {"tweets": 0, "likes": 5,  "follows": 3,  "retweets": 0},
    WarmupPhase.ENGAGE:  {"tweets": 3, "likes": 10, "follows": 8,  "retweets": 2},
    WarmupPhase.ACTIVE:  {"tweets": 8, "likes": 20, "follows": 15, "retweets": 5},
}

# 垢齢（日）からフェーズを決める
def get_phase(created_at: datetime) -> WarmupPhase:
    age_days = (datetime.utcnow() - created_at).days
    if age_days < 7:
        return WarmupPhase.OBSERVE
    elif age_days < 14:
        return WarmupPhase.ENGAGE
    else:
        return WarmupPhase.ACTIVE


# 日本時間でアクティブな時間帯（UTC+9換算でUTC 0:00-14:00）
ACTIVE_HOURS_UTC = list(range(0, 14))  # 09:00-23:00 JST


def is_active_hour() -> bool:
    return datetime.utcnow().hour in ACTIVE_HOURS_UTC


class DailyActionCounter:
    """アカウントごとの当日アクション数をメモリで管理。"""

    def __init__(self):
        self._counters: dict[int, dict] = {}
        self._dates: dict[int, str] = {}

    def _reset_if_new_day(self, account_id: int):
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self._dates.get(account_id) != today:
            self._counters[account_id] = {"tweets": 0, "likes": 0, "follows": 0, "retweets": 0}
            self._dates[account_id] = today

    def increment(self, account_id: int, action: str):
        self._reset_if_new_day(account_id)
        self._counters[account_id][action] = self._counters[account_id].get(action, 0) + 1

    def get(self, account_id: int, action: str) -> int:
        self._reset_if_new_day(account_id)
        return self._counters[account_id].get(action, 0)

    def can_do(self, account_id: int, action: str, phase: WarmupPhase) -> bool:
        limit = PHASE_LIMITS[phase].get(action, 0)
        return self.get(account_id, action) < limit


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
    ):
        self.config = config
        self.browser = browser
        self.login = login_action
        self.tweet = tweet_action
        self.follow = follow_action
        self.like = like_action
        self.follow_targets = follow_targets or []
        self.tweet_templates = tweet_templates  # None の場合は random_tweet() を使う
        self._counter = DailyActionCounter()

    async def run_daily_actions(
        self, account: Account, proxy_url: Optional[str] = None
    ) -> dict:
        """
        1垢分の1日分アクションを実行して結果dictを返す。
        """
        if not is_active_hour():
            logger.debug(f"[{account.username}] outside active hours, skip")
            return {"skipped": True, "reason": "inactive_hour"}

        phase = get_phase(account.created_at)
        limits = PHASE_LIMITS[phase]
        results = {"phase": phase.value, "tweets": 0, "likes": 0, "follows": 0}

        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            logger.warning(f"[{account.username}] login failed in warmup")
            return {"error": "login_failed"}

        try:
            # いいね
            target_likes = random.randint(
                max(1, limits["likes"] // 2), limits["likes"]
            )
            if self._counter.can_do(account.id, "likes", phase) and target_likes > 0:
                liked = await self.like.like_timeline_random(context, account, count=target_likes)
                self._counter.increment(account.id, "likes")
                results["likes"] = liked
                await self._rest(10, 30)

            # ツイート
            if (
                limits["tweets"] > 0
                and self._counter.can_do(account.id, "tweets", phase)
                and random.random() < 0.6
            ):
                text = (
                    random.choice(self.tweet_templates)
                    if self.tweet_templates
                    else random_tweet()
                )
                ok = await self.tweet.post(context, text, account)
                if ok:
                    self._counter.increment(account.id, "tweets")
                    results["tweets"] += 1
                await self._rest(15, 45)

            # フォロー
            if (
                limits["follows"] > 0
                and self._counter.can_do(account.id, "follows", phase)
                and self.follow_targets
                and random.random() < 0.4
            ):
                target = random.choice(self.follow_targets)
                ok = await self.follow.follow(context, target, account)
                if ok:
                    self._counter.increment(account.id, "follows")
                    results["follows"] += 1
                await self._rest(10, 25)

        finally:
            await context.close()

        logger.info(f"[{account.username}] warmup done: {results}")
        return results

    async def _rest(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))
