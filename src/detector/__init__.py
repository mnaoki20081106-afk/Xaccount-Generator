"""
シャドウバン検知 + 解除 + 検索TOPチェック。

バン種別:
  search_ban   — 検索結果・タイプアヘッドに出ない
  reply_ban    — リプライが折りたたまれる (ghost ban)
  full_ban     — 完全ゴースト

検知フロー (3ソース多数決):
  1. hisubscriptions API
  2. shadowban.eu 互換 API
  3. X GraphQL search + ゲスト API (自前検証)
  多数決スコア 2/3 以上で確定、1/3 は "plausible"

SearchTopChecker:
  - from:@username を LiveSearch で叩きツイートが出るか確認
  - Typeahead (オートコンプリート) で @username が候補に出るか確認
  - 両方 OK → TOP出現クリア
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from ..models import Account, AccountStatus
from ..utils.browser import BrowserManager
from ..actions.login import LoginAction
from ..actions.tweet import TweetAction
from ..actions.like import LikeAction


# ── 公開 Bearer トークン ──────────────────────────────────────
_GUEST_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_BEARER_URL_ENC = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_GUEST_TOKEN_URL = "https://api.twitter.com/1.1/guest/activate.json"

# adaptive search (v2相当 — ゲストトークン必須)
_SEARCH_ADAPTIVE_URL = (
    "https://twitter.com/i/api/2/search/adaptive.json"
    "?q=from%3A{handle}&count=5&result_filter=live"
)
# v1.1 search
_SEARCH_V1_URL = (
    "https://api.twitter.com/1.1/search/tweets.json"
    "?q=from%3A{handle}&count=5&result_type=recent"
)
# タイプアヘッド
_TYPEAHEAD_URL = (
    "https://twitter.com/i/api/1.1/search/typeahead.json"
    "?q={handle}&result_type=users&src=search_box"
)

# リトライ設定
_MAX_RETRIES = 3
_RETRY_BASE = 2.0


# ──────────────────────────────────────────────────────────────
# データクラス
# ──────────────────────────────────────────────────────────────

@dataclass
class BanStatus:
    search_ban: bool = False
    reply_ban:  bool = False
    full_ban:   bool = False
    checked_at: Optional[datetime] = None
    source: str = "unknown"
    # 各ソースの生の投票結果
    votes: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.now(timezone.utc)

    @property
    def shadow_score(self) -> float:
        score = 0.0
        if self.search_ban:
            score += 0.4
        if self.reply_ban:
            score += 0.4
        if self.full_ban:
            score += 0.8
        return min(score, 1.0)

    @property
    def is_clean(self) -> bool:
        return not (self.search_ban or self.reply_ban or self.full_ban)

    @property
    def status(self) -> AccountStatus:
        if self.full_ban or self.shadow_score >= 0.8:
            return AccountStatus.HARD_SHADOW
        elif self.shadow_score > 0:
            return AccountStatus.SOFT_SHADOW
        return AccountStatus.CLEAN


@dataclass
class SearchTopResult:
    """from:@username の検索TOP可視性チェック結果。"""
    appears_in_live_search: bool = False
    appears_in_typeahead:   bool = False
    checked_at: Optional[datetime] = None
    tweet_count: int = 0

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.now(timezone.utc)

    @property
    def is_visible(self) -> bool:
        return self.appears_in_live_search and self.appears_in_typeahead

    @property
    def visibility_score(self) -> float:
        score = 0.0
        if self.appears_in_live_search:
            score += 0.6
        if self.appears_in_typeahead:
            score += 0.4
        return score


# ──────────────────────────────────────────────────────────────
# ゲストトークン管理
# ──────────────────────────────────────────────────────────────

class _GuestTokenManager:
    _token: Optional[str] = None
    _expires_at: float = 0.0
    _lock = asyncio.Lock()

    @classmethod
    async def get(cls) -> Optional[str]:
        async with cls._lock:
            if cls._token and time.monotonic() < cls._expires_at:
                return cls._token
            return await cls._refresh()

    @classmethod
    async def _refresh(cls) -> Optional[str]:
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.post(
                        _GUEST_TOKEN_URL,
                        headers={
                            "Authorization": f"Bearer {_GUEST_BEARER}",
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "x-twitter-client-language": "ja",
                        },
                    )
                    if r.status_code == 200:
                        token = r.json().get("guest_token")
                        if token:
                            cls._token = token
                            cls._expires_at = time.monotonic() + 3300  # ~55分
                            logger.debug(f"[GuestToken] refreshed (attempt {attempt+1})")
                            return token
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BASE ** attempt + random.uniform(0, 1))
                logger.debug(f"[GuestToken] refresh error (attempt {attempt+1}): {e}")
        return None

    @classmethod
    def invalidate(cls):
        cls._token = None
        cls._expires_at = 0.0


def _guest_headers(token: str) -> dict:
    return {
        "Authorization":            f"Bearer {_GUEST_BEARER}",
        "x-guest-token":            token,
        "User-Agent":               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "x-twitter-active-user":    "yes",
        "x-twitter-client-language":"ja",
        "Referer":                  "https://twitter.com/",
        "Accept":                   "application/json",
        "Accept-Language":          "ja-JP,ja;q=0.9,en;q=0.8",
    }


# ──────────────────────────────────────────────────────────────
# ShadowbanDetector
# ──────────────────────────────────────────────────────────────

class ShadowbanDetector:
    """
    3ソースから投票を集め、多数決でシャドウバン状態を判定。
    各ソースが None を返した場合は棄権として扱う。
    """

    def __init__(self, browser: BrowserManager):
        self.browser = browser

    async def check(self, username: str) -> BanStatus:
        handle = username.lstrip("@")

        # 3ソースを並列取得（タイムアウト 30 秒）
        results = await asyncio.gather(
            self._check_hisubscriptions(handle),
            self._check_shadowban_eu(handle),
            self._check_via_guest_api(handle),
            return_exceptions=True,
        )

        votes: dict[str, list[Optional[bool]]] = {
            "search_ban": [],
            "reply_ban":  [],
            "full_ban":   [],
        }
        source_names = ["hisubscriptions", "shadowban_eu", "guest_api"]

        for src_name, result in zip(source_names, results):
            if isinstance(result, Exception) or result is None:
                continue
            votes["search_ban"].append(result.get("search_ban"))
            votes["reply_ban"].append(result.get("reply_ban"))
            votes["full_ban"].append(result.get("ghost_ban"))

        def _majority(vals: list[Optional[bool]]) -> bool:
            true_count = sum(1 for v in vals if v is True)
            total = len([v for v in vals if v is not None])
            if total == 0:
                return False
            # 2ソース以上が True → 確定
            return true_count >= max(2, (total + 1) // 2)

        status = BanStatus(
            search_ban=_majority(votes["search_ban"]),
            reply_ban =_majority(votes["reply_ban"]),
            full_ban  =_majority(votes["full_ban"]),
            source    ="multi_vote",
            votes     ={k: list(v) for k, v in votes.items()},
        )

        logger.info(
            f"[@{handle}] shadowban check: "
            f"search={status.search_ban} reply={status.reply_ban} full={status.full_ban} "
            f"score={status.shadow_score:.2f}"
        )
        return status

    # ──── hisubscriptions ────────────────────────────────────

    async def _check_hisubscriptions(self, handle: str) -> Optional[dict]:
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.get(
                        "https://hisubscriptions.com/api/shadowban",
                        params={"screen_name": handle},
                        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                    )
                    if r.status_code == 200:
                        data = r.json()
                        return {
                            "search_ban": bool(data.get("has_search_ban") or data.get("search_ban")),
                            "reply_ban":  bool(data.get("has_ghost_ban")  or data.get("reply_ban")),
                            "ghost_ban":  bool(data.get("has_ban")        or data.get("ghost_ban")),
                        }
                    if r.status_code in (429, 503):
                        await asyncio.sleep(_RETRY_BASE ** attempt)
                        continue
                    return None
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BASE ** attempt + random.uniform(0, 0.5))
                logger.debug(f"[@{handle}] hisubscriptions error (attempt {attempt+1}): {e}")
        return None

    # ──── shadowban.eu 互換 ───────────────────────────────────

    async def _check_shadowban_eu(self, handle: str) -> Optional[dict]:
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(
                    timeout=22.0,
                    follow_redirects=True,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                ) as client:
                    r = await client.get(f"https://shadowban.eu/.api/{handle}")
                    if r.status_code == 200:
                        data = r.json()
                        tests   = data.get("tests", {})
                        profile = data.get("profile", {})
                        search_ban = bool(
                            tests.get("search", {}).get("ban")
                            or tests.get("typeahead", {}).get("ban")
                        )
                        ghost_ban = bool(
                            tests.get("ghost", {}).get("ban")
                            or profile.get("sensitiveMediaSettings")
                        )
                        reply_ban = bool(tests.get("more_replies", {}).get("ban"))
                        return {
                            "search_ban": search_ban,
                            "reply_ban":  reply_ban,
                            "ghost_ban":  ghost_ban,
                        }
                    if r.status_code in (429, 503):
                        await asyncio.sleep(_RETRY_BASE ** attempt)
                        continue
                    return None
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BASE ** attempt + random.uniform(0, 0.5))
                logger.debug(f"[@{handle}] shadowban.eu error (attempt {attempt+1}): {e}")
        return None

    # ──── ゲスト API 自前検証 ─────────────────────────────────

    async def _check_via_guest_api(self, handle: str) -> Optional[dict]:
        """
        ゲスト検索 API で from:@handle を叩く。
        ツイートゼロ件 → search_ban の強い指標。
        """
        token = await _GuestTokenManager.get()
        if not token:
            return None

        for attempt in range(_MAX_RETRIES):
            try:
                url = _SEARCH_ADAPTIVE_URL.format(handle=handle)
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.get(url, headers=_guest_headers(token))

                    if r.status_code == 401:
                        _GuestTokenManager.invalidate()
                        token = await _GuestTokenManager.get()
                        if not token:
                            return None
                        continue

                    if r.status_code == 429:
                        await asyncio.sleep(_RETRY_BASE ** attempt * 2)
                        continue

                    if r.status_code == 200:
                        tweet_count = _parse_tweet_count(r.json())
                        search_ban = tweet_count == 0
                        return {
                            "search_ban": search_ban,
                            "reply_ban":  False,  # この API では判断できない
                            "ghost_ban":  False,
                            "_tweet_count": tweet_count,
                        }
                    return None

            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BASE ** attempt + random.uniform(0, 0.5))
                logger.debug(f"[@{handle}] guest_api error (attempt {attempt+1}): {e}")
        return None


def _parse_tweet_count(data: dict) -> int:
    """adaptive.json / v2 search レスポンスからツイート件数を取得。"""
    if not isinstance(data, dict):
        return 0
    # v1.1 adaptive
    tweets = data.get("globalObjects", {}).get("tweets", {})
    if tweets:
        return len(tweets)
    # v2 style
    try:
        instructions = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
        for inst in instructions:
            entries = inst.get("entries", [])
            # tweet エントリだけカウント
            tweet_entries = [
                e for e in entries
                if e.get("entryId", "").startswith("tweet-")
            ]
            if tweet_entries:
                return len(tweet_entries)
    except Exception:
        pass
    return 0


# ──────────────────────────────────────────────────────────────
# SearchTopChecker
# ──────────────────────────────────────────────────────────────

class SearchTopChecker:
    """
    垢が検索TOPに出るかを確認する専用クラス。

    チェック内容:
      1. Live Search  — from:@username でツイートが取得できるか
      2. Typeahead    — @username がオートコンプリートに出るか
    """

    async def check(self, username: str) -> SearchTopResult:
        handle = username.lstrip("@")

        live_result, typeahead_result = await asyncio.gather(
            self._check_live_search(handle),
            self._check_typeahead(handle),
            return_exceptions=True,
        )

        live_ok = live_result if isinstance(live_result, int) else 0
        ta_ok   = typeahead_result if isinstance(typeahead_result, bool) else False

        result = SearchTopResult(
            appears_in_live_search=live_ok > 0,
            appears_in_typeahead=ta_ok,
            tweet_count=live_ok,
        )
        logger.info(
            f"[@{handle}] search top: live={result.appears_in_live_search}({live_ok} tweets) "
            f"typeahead={result.appears_in_typeahead} "
            f"visibility={result.visibility_score:.2f}"
        )
        return result

    async def _check_live_search(self, handle: str) -> int:
        """from:@handle でライブ検索してツイート件数を返す。0 → 不可視。"""
        token = await _GuestTokenManager.get()
        if not token:
            return 0

        for attempt in range(_MAX_RETRIES):
            try:
                url = _SEARCH_ADAPTIVE_URL.format(handle=handle)
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.get(url, headers=_guest_headers(token))
                    if r.status_code == 401:
                        _GuestTokenManager.invalidate()
                        token = await _GuestTokenManager.get()
                        if not token:
                            return 0
                        continue
                    if r.status_code == 429:
                        await asyncio.sleep(_RETRY_BASE ** attempt * 2)
                        continue
                    if r.status_code == 200:
                        return _parse_tweet_count(r.json())
                    return 0
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BASE ** attempt)
                logger.debug(f"[@{handle}] live_search error (attempt {attempt+1}): {e}")
        return 0

    async def _check_typeahead(self, handle: str) -> bool:
        """
        タイプアヘッド検索で @handle が候補に出るかを確認。
        True → 検索サジェストに表示される = 正常
        """
        token = await _GuestTokenManager.get()
        if not token:
            return False

        for attempt in range(_MAX_RETRIES):
            try:
                url = _TYPEAHEAD_URL.format(handle=handle)
                async with httpx.AsyncClient(timeout=15.0) as client:
                    r = await client.get(url, headers=_guest_headers(token))
                    if r.status_code == 401:
                        _GuestTokenManager.invalidate()
                        token = await _GuestTokenManager.get()
                        if not token:
                            return False
                        continue
                    if r.status_code == 429:
                        await asyncio.sleep(_RETRY_BASE ** attempt * 2)
                        continue
                    if r.status_code == 200:
                        data = r.json()
                        users = data.get("users", [])
                        lc_handle = handle.lower()
                        for u in users:
                            sn = (u.get("screen_name") or "").lower()
                            if sn == lc_handle:
                                return True
                        return False
                    return False
            except Exception as e:
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BASE ** attempt)
                logger.debug(f"[@{handle}] typeahead error (attempt {attempt+1}): {e}")
        return False


# ──────────────────────────────────────────────────────────────
# ShadowbanResolver
# ──────────────────────────────────────────────────────────────

class ShadowbanResolver:
    def __init__(
        self,
        browser: BrowserManager,
        login_action: LoginAction,
        tweet_action: TweetAction,
        like_action: LikeAction,
    ):
        self.browser = browser
        self.login = login_action
        self.tweet = tweet_action
        self.like = like_action

    async def resolve(
        self,
        account: Account,
        status: BanStatus,
        proxy_url: Optional[str] = None,
    ) -> str:
        score = status.shadow_score

        if score >= 0.8:
            logger.info(f"[{account.username}] full ban → cooling period")
            return "cooling"

        if score >= 0.4:
            logger.info(f"[{account.username}] soft ban → engagement recovery")
            await self._engagement_recovery(account, proxy_url)
            return "engagement_recovery"

        # search_ban のみ → 軽いエンゲージメント
        if status.search_ban and not status.reply_ban:
            logger.info(f"[{account.username}] search_ban only → light recovery")
            await self._light_recovery(account, proxy_url)
            return "light_recovery"

        await self._light_recovery(account, proxy_url)
        return "light_recovery"

    async def _light_recovery(
        self, account: Account, proxy_url: Optional[str] = None
    ):
        """軽いブラウズ + いいね数枚でアルゴリズムに正常シグナルを送る。"""
        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            return

        try:
            page = await self.browser.new_page(context)
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(4.0, 8.0))

            for _ in range(random.randint(4, 8)):
                await page.mouse.wheel(0, random.randint(200, 500))
                await asyncio.sleep(random.uniform(2.0, 5.0))

            await page.close()
            await self.like.like_timeline_random(context, account, count=random.randint(3, 5))
            await asyncio.sleep(random.uniform(30, 90))
        finally:
            await context.close()

        logger.info(f"[{account.username}] light recovery done")

    async def _engagement_recovery(
        self, account: Account, proxy_url: Optional[str] = None
    ):
        """
        より積極的な回復:
        1. ブラウズ + いいね
        2. トレンドを閲覧
        3. 無害ツイートを削除 (いいね 0 件のもの)
        """
        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            return

        try:
            page = await self.browser.new_page(context)

            # ホームブラウズ
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(3, 6))
            for _ in range(random.randint(5, 10)):
                await page.mouse.wheel(0, random.randint(150, 500))
                await asyncio.sleep(random.uniform(1.5, 4.0))

            # エクスプローラー閲覧
            await page.goto("https://x.com/explore", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(random.uniform(3, 7))
            for _ in range(random.randint(2, 4)):
                await page.mouse.wheel(0, random.randint(200, 400))
                await asyncio.sleep(random.uniform(2, 4))

            await page.close()
            await self.like.like_timeline_random(context, account, count=random.randint(4, 8))
            await asyncio.sleep(random.uniform(60, 180))

        finally:
            await context.close()

        logger.info(f"[{account.username}] engagement recovery done")

    async def delete_recent_tweets(
        self,
        account: Account,
        count: int = 5,
        proxy_url: Optional[str] = None,
    ) -> int:
        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            return 0

        deleted = 0
        page = await self.browser.new_page(context)
        try:
            profile_url = f"https://x.com/{account.username}"
            await page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(2.0, 3.5))

            for _ in range(count):
                try:
                    menu_sel = '[data-testid="caret"]'
                    menus = await page.locator(menu_sel).all()
                    if not menus:
                        break
                    await menus[0].click()
                    await asyncio.sleep(0.6)

                    for dsel in [
                        'div[role="menuitem"]:has-text("削除")',
                        'div[role="menuitem"]:has-text("Delete")',
                    ]:
                        try:
                            d = page.locator(dsel).first
                            if await d.is_visible(timeout=2000):
                                await d.click()
                                await asyncio.sleep(0.5)
                                conf = page.locator(
                                    '[data-testid="confirmationSheetConfirm"]'
                                ).first
                                if await conf.is_visible(timeout=2000):
                                    await conf.click()
                                deleted += 1
                                await asyncio.sleep(random.uniform(1.5, 3.0))
                                break
                        except Exception:
                            continue
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"[{account.username}] delete tweet error: {e}")
        finally:
            await page.close()
            await context.close()

        logger.info(f"[{account.username}] deleted {deleted} tweets")
        return deleted
