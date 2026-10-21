"""
シャドウバン検知 + 解除。

3種類のバンを区別する:
- search_ban:  検索結果に出ない（from:@username が一致しない）
- reply_ban:   リプライが折りたたまれる / ghost ban
- full_ban:    完全にゴースト状態

検知フロー:
  1. hisubscriptions API（メイン）
  2. API失敗 → shadowban.eu 互換 API（バックアップ）
  3. ゲスト検索 API で from:@username を実際に叩く（最終確認）
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

from ..models import Account, AccountStatus
from ..utils.browser import BrowserManager
from ..actions.login import LoginAction
from ..actions.tweet import TweetAction
from ..actions.like import LikeAction

# X ゲスト認証 — 公開 Bearer トークン（変わることがある）
_GUEST_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_GUEST_TOKEN_URL = "https://api.twitter.com/1.1/guest/activate.json"
_SEARCH_URL = (
    "https://twitter.com/i/api/2/search/adaptive.json"
    "?q=from%3A{handle}&count=5&result_filter=live"
)

# Shadowban チェック API — 複数エンドポイントでフォールバック
_SB_APIS = [
    "https://hisubscriptions.com/api/shadowban",
    "https://shadowban.eu/.api/{handle}",
]


@dataclass
class BanStatus:
    search_ban: bool = False
    reply_ban: bool = False
    full_ban: bool = False
    checked_at: Optional[datetime] = None
    source: str = "unknown"

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


class ShadowbanDetector:
    def __init__(self, browser: BrowserManager):
        self.browser = browser
        self._guest_token_cache: tuple[str, float] | None = None

    async def check(self, username: str) -> BanStatus:
        handle = username.lstrip("@")

        # 1. hisubscriptions.com
        result = await self._check_hisubscriptions(handle)
        if result:
            status = BanStatus(
                search_ban=result.get("search_ban", False),
                reply_ban=result.get("reply_ban", False),
                full_ban=result.get("ghost_ban", False),
                source="hisubscriptions",
            )
            logger.info(
                f"[@{handle}] shadowban (hisubscriptions): "
                f"search={status.search_ban} reply={status.reply_ban} full={status.full_ban}"
            )
            # search_ban だけゲスト検索で二重確認
            if status.search_ban or (not result and True):
                guest_result = await self._verify_search_ban_via_guest_api(handle)
                if guest_result is not None:
                    if status.search_ban != guest_result:
                        logger.info(
                            f"[@{handle}] search_ban override by guest API: "
                            f"{status.search_ban} → {guest_result}"
                        )
                        status.search_ban = guest_result
            return status

        # 2. shadowban.eu 互換 API フォールバック
        result2 = await self._check_shadowban_eu(handle)
        if result2:
            status = BanStatus(
                search_ban=result2.get("search_ban", False),
                reply_ban=result2.get("reply_ban", False),
                full_ban=result2.get("ghost_ban", False),
                source="shadowban_eu",
            )
            logger.info(
                f"[@{handle}] shadowban (eu): "
                f"search={status.search_ban} reply={status.reply_ban} full={status.full_ban}"
            )
            return status

        # 3. ゲスト API のみ
        search_ban = await self._verify_search_ban_via_guest_api(handle)
        status = BanStatus(
            search_ban=bool(search_ban),
            source="guest_api",
        )
        logger.info(f"[@{handle}] shadowban (guest_api only): search={status.search_ban}")
        return status

    # ──────────────────────────────────────────
    # hisubscriptions
    # ──────────────────────────────────────────

    async def _check_hisubscriptions(self, handle: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    "https://hisubscriptions.com/api/shadowban",
                    params={"screen_name": handle},
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "search_ban": bool(data.get("has_search_ban") or data.get("search_ban")),
                        "reply_ban": bool(data.get("has_ghost_ban") or data.get("reply_ban")),
                        "ghost_ban": bool(data.get("has_ban") or data.get("ghost_ban")),
                    }
        except Exception as e:
            logger.debug(f"[@{handle}] hisubscriptions error: {e}")
        return None

    # ──────────────────────────────────────────
    # shadowban.eu 互換
    # ──────────────────────────────────────────

    async def _check_shadowban_eu(self, handle: str) -> Optional[dict]:
        """
        shadowban.eu の非公式 JSON エンドポイント。
        レスポンス形式が変わりやすいので防御的に読む。
        """
        try:
            url = f"https://shadowban.eu/.api/{handle}"
            async with httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            ) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    profile = data.get("profile", {})
                    tests = data.get("tests", {})
                    # shadowban.eu の tests フィールド構造
                    search_ban = (
                        tests.get("search", {}).get("ban")
                        or tests.get("typeahead", {}).get("ban")
                    )
                    ghost_ban = (
                        tests.get("ghost", {}).get("ban")
                        or profile.get("sensitiveMediaSettings")
                    )
                    return {
                        "search_ban": bool(search_ban),
                        "reply_ban": bool(
                            tests.get("more_replies", {}).get("ban")
                        ),
                        "ghost_ban": bool(ghost_ban),
                    }
        except Exception as e:
            logger.debug(f"[@{handle}] shadowban.eu error: {e}")
        return None

    # ──────────────────────────────────────────
    # ゲスト検索 API による from:@username 確認
    # ──────────────────────────────────────────

    async def _get_guest_token(self) -> Optional[str]:
        import time
        if self._guest_token_cache:
            token, ts = self._guest_token_cache
            if time.time() - ts < 3600:
                return token

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    _GUEST_TOKEN_URL,
                    headers={
                        "Authorization": f"Bearer {_GUEST_BEARER}",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                if r.status_code == 200:
                    token = r.json().get("guest_token")
                    if token:
                        import time as _t
                        self._guest_token_cache = (token, _t.time())
                        return token
        except Exception as e:
            logger.debug(f"guest token error: {e}")
        return None

    async def _verify_search_ban_via_guest_api(self, handle: str) -> Optional[bool]:
        """
        from:@handle を X の検索 API で叩く。
        ツイートが 0 件 → search_ban の可能性が高い（完全には断言できない）。
        None を返すと「不明」として扱う。
        """
        token = await self._get_guest_token()
        if not token:
            return None
        try:
            url = _SEARCH_URL.format(handle=handle)
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    url,
                    headers={
                        "Authorization": f"Bearer {_GUEST_BEARER}",
                        "x-guest-token": token,
                        "User-Agent": "Mozilla/5.0",
                        "x-twitter-active-user": "yes",
                        "x-twitter-client-language": "ja",
                        "Referer": "https://twitter.com/",
                        "Accept": "application/json",
                    },
                )
                if r.status_code == 200:
                    data = r.json()
                    tweets = (
                        data.get("globalObjects", {}).get("tweets", {})
                        or data.get("data", {}).get("search_by_raw_query", {})
                        .get("search_timeline", {})
                        .get("timeline", {})
                        .get("instructions", [{}])[0]
                        .get("entries", [])
                    )
                    if isinstance(tweets, dict):
                        return len(tweets) == 0
                    elif isinstance(tweets, list):
                        return len(tweets) == 0
                    return None
        except Exception as e:
            logger.debug(f"[@{handle}] guest search error: {e}")
        return None


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
            logger.info(f"[{account.username}] soft ban → light recovery")
            await self._light_recovery(account, proxy_url)
            return "light_actions"

        return "light_actions"

    async def _light_recovery(
        self, account: Account, proxy_url: Optional[str] = None
    ):
        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            return

        try:
            page = await self.browser.new_page(context)
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(3.0, 6.0))

            for _ in range(random.randint(3, 6)):
                await page.mouse.wheel(0, random.randint(200, 500))
                await asyncio.sleep(random.uniform(2.0, 5.0))

            await page.close()
            await self.like.like_timeline_random(context, account, count=random.randint(2, 3))
            await asyncio.sleep(random.uniform(30, 90))
        finally:
            await context.close()

        logger.info(f"[{account.username}] light recovery done")

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
                    await asyncio.sleep(0.5)

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
