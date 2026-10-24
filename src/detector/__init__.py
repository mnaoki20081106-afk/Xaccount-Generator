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
    X-Utility（shadowban.ts）と同じ判定を、登録済みの観測者（別の垢）で行う。
      - `from:ユーザー名` の Top 検索に本人のポストが出ない → search_ban
      - 検索候補(typeahead)に本人が出ない                    → search_ban
    観測者が居ない／検索が失敗した時は unknown（クリーン扱いしない）。
    以前使っていた外部サイト(hisubscriptions 等)とゲスト検索は、形式変更・廃止で
    誤判定の原因だったため廃止した。
    """

    def __init__(self, browser: BrowserManager, observers=None, client_factory=None):
        self.browser = browser
        self.observers = observers
        self._client_factory = client_factory

    async def check(self, username: str, has_tweets: bool = True) -> BanStatus:
        handle = username.lstrip("@")
        if self.observers is None or self._client_factory is None:
            return BanStatus(source="none", unknown=True)
        observer = await self.observers.pick()
        if observer is None:
            logger.warning("shadowban check: 観測者が未登録 → unknown")
            return BanStatus(source="none", unknown=True)
        try:
            async with self._client_factory(observer, observer.proxy_url) as obs:
                state = await obs.verify_state()
                if state != "ok":
                    await self.observers.report(observer, state)
                    return BanStatus(source="observer", unknown=True)
                user = await obs.user_by_screen_name(handle)
                if user["state"] == "suspended":
                    return BanStatus(full_ban=True, source="observer")
                if user["state"] != "ok" or not user["tweet_count"]:
                    return BanStatus(source="observer", unknown=True)  # 鍵/不在/投稿0は判定不能
                real = user["screen_name"]
                top = await obs.search_timeline(f"from:{real}", "Top")
                in_top = any(t["screen_name"].lower() == real.lower() for t in top)
                sugg = await obs.typeahead_users(f"@{real} {user['name']}")
                in_sugg = any(u.lower() == real.lower() for u in sugg)
                logger.info(f"[@{handle}] shadowban (observer): top={in_top} suggestion={in_sugg}")
                return BanStatus(search_ban=not (in_top and in_sugg), source="observer")
        except Exception as e:
            logger.debug(f"[@{handle}] observer search error: {e}")
            return BanStatus(source="observer", unknown=True)


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
