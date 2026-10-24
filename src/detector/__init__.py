"""
シャドウバン検知 + 解除。

3種類のバンを区別する:
- search_ban:  検索結果に出ない（from:@username が一致しない）
- reply_ban:   リプライが折りたたまれる / ghost ban
- full_ban:    完全にゴースト状態

検知フロー:
  1. 別垢（観測者）から People / from: 検索を実際に叩く（主）
  2. 外部のシャドウバン確認サイト（補助）
  3. どちらも得られなければ unknown（クリーン扱いしない）
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

@dataclass
@dataclass
class BanStatus:
    search_ban: bool = False
    reply_ban: bool = False
    full_ban: bool = False
    checked_at: Optional[datetime] = None
    source: str = "unknown"
    # 判定材料が1つも得られなかった。この時は「クリーン」と見なしてはいけない。
    unknown: bool = False

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
        return not self.unknown and not (self.search_ban or self.reply_ban or self.full_ban)

    @property
    def status(self) -> AccountStatus:
        if self.full_ban or self.shadow_score >= 0.8:
            return AccountStatus.HARD_SHADOW
        elif self.shadow_score > 0:
            return AccountStatus.SOFT_SHADOW
        return AccountStatus.CLEAN


class ShadowbanDetector:
    """
    判定の主役は「別の垢（観測者）から実際に検索する」方式。
    - ユーザー検索（People）でハンドルが出ない → search_ban
    - from:ハンドル の最新検索が0件（ツイート済みなのに）→ search_ban
    外部のシャドウバン確認サイトはレスポンス形式が不安定なので補助扱い。
    どちらも得られない場合は unknown（=クリーン扱いにしない）。
    """

    def __init__(self, browser: BrowserManager, client_factory=None):
        self.browser = browser
        self._client_factory = client_factory

    async def check(
        self,
        username: str,
        observer=None,
        observer_proxy_url: Optional[str] = None,
        has_tweets: bool = True,
    ) -> BanStatus:
        handle = username.lstrip("@")

        # 1. 観測者の検索（主）
        if observer is not None and self._client_factory is not None:
            res = await self._check_via_observer(handle, observer, observer_proxy_url, has_tweets)
            if res is not None:
                logger.info(f"[@{handle}] shadowban (observer): search={res.search_ban}")
                return res

        # 2. 外部サイト（補助）
        for fn, src in ((self._check_hisubscriptions, "hisubscriptions"),
                        (self._check_shadowban_eu, "shadowban_eu")):
            result = await fn(handle)
            if result:
                status = BanStatus(
                    search_ban=result.get("search_ban", False),
                    reply_ban=result.get("reply_ban", False),
                    full_ban=result.get("ghost_ban", False),
                    source=src,
                )
                logger.info(
                    f"[@{handle}] shadowban ({src}): search={status.search_ban} "
                    f"reply={status.reply_ban} full={status.full_ban}"
                )
                return status

        logger.warning(f"[@{handle}] shadowban 判定材料なし → unknown")
        return BanStatus(source="none", unknown=True)

    async def _check_via_observer(
        self, handle: str, observer, proxy_url: Optional[str], has_tweets: bool
    ) -> Optional[BanStatus]:
        try:
            async with self._client_factory(observer, proxy_url) as obs:
                people = await obs.search_timeline(handle, "People")
                in_people = any(u["screen_name"].lower() == handle.lower() for u in people[:5])
                if not people:
                    return None  # 検索自体が空 = 観測者側の不調かもしれない。判定しない
                search_ban = not in_people
                if has_tweets and not search_ban:
                    latest = await obs.search_timeline(f"from:{handle}", "Latest")
                    search_ban = len(latest) == 0
                return BanStatus(search_ban=search_ban, source="observer")
        except Exception as e:
            logger.debug(f"[@{handle}] observer search error: {e}")
            return None

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
