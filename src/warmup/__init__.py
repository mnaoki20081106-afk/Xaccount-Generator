"""
アカウントウォームアップスケジューラ。

auth_token + ct0 が DB にある垢は API 経由で実行（Chromium 不要）。
トークンがない場合のみブラウザフォールバック。

API モード: メモリ ~20MB、プロキシ通信 ~50KB/アクション
ブラウザモード: メモリ ~300MB、プロキシ通信 ~2-5MB/アクション
"""
import asyncio
import hashlib
import math
import random
from datetime import date, datetime, timezone
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
    OBSERVE = "observe"    # week1: 閲覧中心
    ENGAGE = "engage"      # week2: いいね・フォロー
    ACTIVE = "active"      # week3+: 投稿・全行動


# フェーズの代表値（アンカー）。日々の上限は age に応じてこの間を滑らかに補間する。
PHASE_LIMITS = {
    WarmupPhase.OBSERVE: {"tweets": 0, "likes": 5,  "follows": 3,  "retweets": 0, "browse": 3},
    WarmupPhase.ENGAGE:  {"tweets": 3, "likes": 10, "follows": 8,  "retweets": 2, "browse": 2},
    WarmupPhase.ACTIVE:  {"tweets": 8, "likes": 20, "follows": 15, "retweets": 5, "browse": 1},
}

# 1日のセッション回数（人は1日に1回だけ、まとめて操作しない）
SESSIONS_PER_DAY = {
    WarmupPhase.OBSERVE: (1, 2),
    WarmupPhase.ENGAGE: (2, 3),
    WarmupPhase.ACTIVE: (2, 4),
}
REST_DAY_EVERY = 8          # 初週を過ぎたら、約8日に1日は何もしない日を作る
BACKOFF_HOURS = 24          # 自動化判定/上限に当たったら、この時間は止める


def get_phase(created_at: datetime) -> WarmupPhase:
    age_days = _age_days(created_at)
    if age_days < 7:
        return WarmupPhase.OBSERVE
    elif age_days < 14:
        return WarmupPhase.ENGAGE
    else:
        return WarmupPhase.ACTIVE


def _age_days(created_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    return (now - ca).total_seconds() / 86400


def _h(*parts) -> int:
    """プロセス間で安定な決定的ハッシュ（hash() は起動ごとに変わる）。"""
    return int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big")


def day_limits(account_id: int, created_at: datetime, today: Optional[date] = None) -> dict:
    """
    その日の上限。フェーズ境界でいきなり倍になる階段状だと不自然なので、
    0→7日→14日のアンカー間を線形補間し、垢ごと・日ごとに ±25% ほどゆらす。
    """
    age = _age_days(created_at)
    o, e, a = (PHASE_LIMITS[p] for p in (WarmupPhase.OBSERVE, WarmupPhase.ENGAGE, WarmupPhase.ACTIVE))
    if age < 7:
        lo, hi, t = o, e, age / 7
    elif age < 14:
        lo, hi, t = e, a, (age - 7) / 7
    else:
        lo, hi, t = a, a, 0.0
    today = today or datetime.now(timezone.utc).date()
    out = {}
    for k in lo:
        base = lo[k] + (hi[k] - lo[k]) * t
        jitter = 0.75 + (_h("lim", account_id, today.isoformat(), k) % 51) / 100  # 0.75–1.25
        out[k] = int(round(base * jitter)) if base >= 1 else 0
    # ツイートは初週は0のまま（フォロワー0で連投すると目立つ）
    if age < 5:
        out["tweets"] = 0
        out["retweets"] = 0
    return out


def is_rest_day(account_id: int, created_at: datetime, today: Optional[date] = None) -> bool:
    if _age_days(created_at) < 7:
        return False
    today = today or datetime.now(timezone.utc).date()
    return _h("rest", account_id, today.isoformat()) % REST_DAY_EVERY == 0


def session_hours(account_id: int, phase: WarmupPhase, today: Optional[date] = None) -> list[int]:
    """その日のセッション開始時刻（UTC時）。日本時間の活動時間内で垢ごとに散らす。"""
    today = today or datetime.now(timezone.utc).date()
    lo, hi = SESSIONS_PER_DAY[phase]
    n = lo + _h("n", account_id, today.isoformat()) % (hi - lo + 1)
    hours = sorted({_h("hr", account_id, today.isoformat(), i) % len(ACTIVE_HOURS_UTC) for i in range(n)})
    return [ACTIVE_HOURS_UTC[i] for i in hours]


ACTIVE_HOURS_UTC = list(range(0, 14))  # 09:00-23:00 JST


def is_active_hour() -> bool:
    return datetime.now(timezone.utc).hour in ACTIVE_HOURS_UTC


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

    async def _remaining(self, account_id: int, action: str, limits: dict) -> int:
        limit = limits.get(action, 0)
        if limit <= 0:
            return 0
        return max(0, limit - await self._count_today(account_id, action))

    @staticmethod
    def _share(remaining: int, sessions_left: int) -> int:
        """今日の残り枠を残りセッションで割り、±1ゆらす（最終セッションは残り全部まで）。"""
        if remaining <= 0:
            return 0
        if sessions_left <= 1:
            return remaining
        base = math.ceil(remaining / sessions_left)
        return max(0, min(remaining, base + random.choice((-1, 0, 0, 1))))

    async def _fresh_tweet(self, account_id: int) -> str:
        """直近の自分のツイートと被らない文面を選ぶ（重複ツイートは目立つ・弾かれる）。"""
        recent: set[str] = set()
        if self.db:
            try:
                recent = {d[:30] for d in await self.db.recent_action_details(account_id, "tweets", 40)}
            except Exception:
                pass
        text = ""
        for _ in range(8):
            text = random.choice(self.tweet_templates) if self.tweet_templates else random_tweet()
            if text[:30] not in recent:
                break
        return text

    # ────────────────────────────────────────────────────────
    # メインエントリ
    # ────────────────────────────────────────────────────────

    async def _due(self, account: Account):
        """今セッションを動かしてよいか。戻り値: (スキップ理由 or None, phase, 当日の計画時刻, 実施済み数)。"""
        phase = get_phase(account.created_at)
        if not is_active_hour():
            return "inactive_hour", phase, [], 0
        if self.db and await self.db.has_recent_action(account.id, "backoff", BACKOFF_HOURS):
            return "backoff", phase, [], 0
        if is_rest_day(account.id, account.created_at):
            return "rest_day", phase, [], 0
        plan = session_hours(account.id, phase)
        due = [h for h in plan if h <= datetime.now(timezone.utc).hour]
        done = await self._count_today(account.id, "session")
        if done >= len(due):
            return "no_session_due", phase, plan, done
        return None, phase, plan, done

    async def due_reason(self, account: Account) -> Optional[str]:
        """外部（オーケストレーター）が、通信を伴う事前確認（IP確認など）の前に聞くための入口。"""
        return (await self._due(account))[0]

    async def run_daily_actions(
        self, account: Account, proxy_url: Optional[str] = None
    ) -> dict:
        """
        1回の呼び出し = 最大1セッション。1日の予定セッション（垢ごとに時刻をずらす）のうち、
        開始時刻を過ぎていて未実施のものがある時だけ動く。オーケストレーターは1時間ごとなど
        頻繁に呼んでよい。戻り値に "flag"（suspended/locked）が入ったら呼び出し側で状態を更新する。
        """
        reason, phase, plan, done = await self._due(account)
        if reason:
            logger.debug(f"[{account.username}] warmup skip: {reason}")
            return {"skipped": True, "reason": reason}
        sessions_left = len(plan) - done

        limits = day_limits(account.id, account.created_at)
        quota = {
            k: self._share(await self._remaining(account.id, k, limits), sessions_left)
            for k in ("likes", "follows", "tweets", "retweets")
        }
        quota["browse"] = 1 if limits.get("browse", 0) > 0 else 0

        # auth_token があれば API モード（Chromium 不要）
        if account.auth_token and account.ct0 and HAS_CURL_CFFI:
            result = await self._run_api(account, phase, proxy_url, quota)
        else:
            # トークンなし → ブラウザログインして取得してから実行
            result = await self._run_browser(account, phase, proxy_url, quota)
        if "error" not in result and "flag" not in result:
            await self._log(account.id, "session", f"{phase.value}:{sum(quota.values())}")
        return result

    async def _after(self, account: Account, client, results: dict) -> bool:
        """クライアントが止めるべきサインを出していたら記録して True。"""
        flags = getattr(client, "flags", set())
        if "suspended" in flags or "locked" in flags:
            results["flag"] = "suspended" if "suspended" in flags else "locked"
            await self._log(account.id, "flag", results["flag"])
            logger.error(f"[@{account.username}] {results['flag']} detected during warmup")
            return True
        if flags & {"automated", "limited"}:
            await self._log(account.id, "backoff", ",".join(sorted(flags)))
            results["backoff"] = sorted(flags)
            logger.warning(f"[@{account.username}] warmup backoff {sorted(flags)} — {BACKOFF_HOURS}h pause")
            return True
        return False

    # ────────────────────────────────────────────────────────
    # API モード（軽量・高速）
    # ────────────────────────────────────────────────────────

    async def _run_api(
        self, account: Account, phase: WarmupPhase, proxy_url: Optional[str], quota: dict
    ) -> dict:
        results = {"mode": "api", "phase": phase.value, "tweets": 0, "likes": 0,
                   "follows": 0, "retweets": 0, "browse": 0}

        # 1 セッションを全アクションで共有（TLS ハンドシェイク 1 回）
        async with XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url) as client:
            state = await client.verify_state()
            if state in ("suspended", "locked"):
                results["flag"] = state
                await self._log(account.id, "flag", state)
                return results
            if state == "unauthorized":
                logger.warning(f"[{account.username}] API token invalid, falling back to browser")
                return await self._run_browser(account, phase, proxy_url, quota)
            if state != "ok":
                return {"error": "verify_failed"}

            # まずタイムラインを眺める（毎回。いきなり操作から入らない）
            ids: list[str] = []
            if quota.get("browse"):
                ids = await client.home_timeline(count=random.randint(15, 30))
                if ids:
                    await self._log(account.id, "browse", f"{phase.value}_api")
                    results["browse"] = 1
                await asyncio.sleep(random.uniform(20, 70))

            # いいね
            if quota["likes"] > 0:
                liked = await client.like_random_from_timeline(count=quota["likes"])
                if liked > 0:
                    await self._log(account.id, "likes", str(liked))
                results["likes"] = liked
                await asyncio.sleep(random.uniform(10, 30))
                if await self._after(account, client, results):
                    return results

            # リツイート
            if quota["retweets"] > 0:
                pool = ids or await client.home_timeline(count=20)
                random.shuffle(pool)
                for tid in pool[:quota["retweets"]]:
                    if await client.retweet(tid):
                        await self._log(account.id, "retweets", tid)
                        results["retweets"] += 1
                    await asyncio.sleep(random.uniform(8, 25))
                    if client.flags:
                        break
                if await self._after(account, client, results):
                    return results

            # フォロー
            if quota["follows"] > 0:
                if random.random() < 0.5 or not self.follow_targets:
                    n = max(1, min(3, quota["follows"]))
                    followed = await client.follow_suggestions(count=n)
                    if followed > 0:
                        await self._log(account.id, "follows", f"suggestions_api:{followed}")
                        results["follows"] += followed
                else:
                    target = random.choice(self.follow_targets)
                    if await client.follow(target):
                        await self._log(account.id, "follows", target)
                        results["follows"] += 1
                await asyncio.sleep(random.uniform(10, 25))
                if await self._after(account, client, results):
                    return results

            # ツイート（セッション内の枠を全部使うのではなく、確率で間引く）
            if quota["tweets"] > 0 and random.random() < 0.7:
                text = await self._fresh_tweet(account.id)
                if await client.tweet(text):
                    await self._log(account.id, "tweets", text[:40])
                    results["tweets"] += 1
                await asyncio.sleep(random.uniform(15, 45))
                await self._after(account, client, results)

        logger.info(f"[{account.username}] warmup(api) done phase={phase.value}: {results}")
        return results

    # ────────────────────────────────────────────────────────
    # ブラウザフォールバック（トークンなし垢 or API 失敗時）
    # ────────────────────────────────────────────────────────

    async def _run_browser(
        self, account: Account, phase: WarmupPhase, proxy_url: Optional[str], quota: dict
    ) -> dict:
        results = {"mode": "browser", "phase": phase.value, "tweets": 0, "likes": 0,
                   "follows": 0, "retweets": 0, "browse": 0}

        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            logger.warning(f"[{account.username}] browser login failed")
            return {"error": "login_failed"}

        try:
            if quota.get("browse"):
                ok = await self.like.browse_timeline(
                    context, account, scroll_count=random.randint(4, 8)
                )
                if ok:
                    await self._log(account.id, "browse", f"{phase.value}_browser")
                    results["browse"] += 1
                await asyncio.sleep(random.uniform(30, 90))

            if quota["likes"] > 0:
                liked = await self.like.like_timeline_random(context, account, count=quota["likes"])
                if liked > 0:
                    await self._log(account.id, "likes", str(liked))
                results["likes"] = liked
                await asyncio.sleep(random.uniform(10, 30))

            if quota["follows"] > 0:
                if random.random() < 0.5 or not self.follow_targets:
                    n = max(1, min(3, quota["follows"]))
                    followed = await self.follow.follow_suggestions(context, account, count=n)
                    if followed > 0:
                        await self._log(account.id, "follows", f"suggestions_browser:{followed}")
                        results["follows"] += followed
                else:
                    target = random.choice(self.follow_targets)
                    if await self.follow.follow(context, target, account):
                        await self._log(account.id, "follows", target)
                        results["follows"] += 1
                await asyncio.sleep(random.uniform(10, 25))

            if quota["tweets"] > 0 and random.random() < 0.7:
                text = await self._fresh_tweet(account.id)
                if await self.tweet.post(context, text, account):
                    await self._log(account.id, "tweets", text[:40])
                    results["tweets"] += 1
                await asyncio.sleep(random.uniform(15, 45))

        finally:
            await context.close()

        logger.info(f"[{account.username}] warmup(browser) done phase={phase.value}: {results}")
        return results
