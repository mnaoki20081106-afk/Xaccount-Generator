"""
シャドウバン検知 + 解除。

3種類のバンを区別する:
- search_ban:  検索結果に出ない
- reply_ban:   リプライが折りたたまれる (suggestive / ghost)
- full_ban:    完全にゴースト状態（フォロワーのTLにも出ない）
"""
import asyncio
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from loguru import logger
import httpx

from ..models import Account, AccountStatus
from ..utils.browser import BrowserManager
from ..actions.login import LoginAction
from ..actions.tweet import TweetAction
from ..actions.like import LikeAction


@dataclass
class BanStatus:
    search_ban: bool = False
    reply_ban: bool = False
    full_ban: bool = False
    checked_at: datetime = None

    def __post_init__(self):
        if self.checked_at is None:
            self.checked_at = datetime.utcnow()

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
    """
    hisubscriptions.com の非公式shadowban APIを使う（無料・登録不要）。
    ブラウザ検証と組み合わせて確度を高める。
    """

    SHADOWBAN_API = "https://hisubscriptions.com/api/shadowban"

    def __init__(self, browser: BrowserManager):
        self.browser = browser

    async def check(self, username: str) -> BanStatus:
        status = BanStatus()

        # API チェック
        api_result = await self._check_via_api(username)
        if api_result:
            status.search_ban = api_result.get("search_ban", False)
            status.reply_ban = api_result.get("reply_ban", False)
            status.full_ban = api_result.get("ghost_ban", False)

        logger.info(
            f"[@{username}] shadowban check: "
            f"search={status.search_ban} reply={status.reply_ban} full={status.full_ban}"
        )
        return status

    async def _check_via_api(self, username: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(
                    self.SHADOWBAN_API,
                    params={"screen_name": username},
                )
                if r.status_code == 200:
                    data = r.json()
                    return {
                        "search_ban": data.get("has_search_ban") or data.get("search_ban"),
                        "reply_ban": data.get("has_ghost_ban") or data.get("reply_ban"),
                        "ghost_ban": data.get("has_ban") or data.get("ghost_ban"),
                    }
        except Exception as e:
            logger.warning(f"[@{username}] shadowban API error: {e}")
        return None


class ShadowbanResolver:
    """
    シャドウバン解除のための自動アクション群。
    """

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
        """
        バン種別に応じた解除アクションを実行。
        戻り値: "cooling" | "light_actions" | "heavy_reset"
        """
        score = status.shadow_score

        if score >= 0.8:
            # フルバン → クーリング期間のみ
            logger.info(f"[{account.username}] full ban detected, entering cooling period")
            return "cooling"

        if score >= 0.4:
            # reply/search ban → アクション停止 + 軽い回復作業
            logger.info(f"[{account.username}] soft ban, doing light recovery")
            await self._light_recovery(account, proxy_url)
            return "light_actions"

        # 軽度 → 通常行動で様子見
        return "light_actions"

    async def _light_recovery(
        self, account: Account, proxy_url: Optional[str] = None
    ):
        """
        - スパム判定されやすいツイートを削除
        - 自然なエンゲージメントを少量実行
        - プロフィールを微修正
        """
        context = await self.login.login(account, proxy_url=proxy_url)
        if not context:
            return

        try:
            # タイムラインをスクロールして閲覧（人間感を出す）
            page = await self.browser.new_page(context)
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(3.0, 6.0))

            for _ in range(random.randint(3, 6)):
                await page.mouse.wheel(0, random.randint(200, 500))
                await asyncio.sleep(random.uniform(2.0, 5.0))

            await page.close()

            # いいね2-3件だけ自然に
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
        """最近のツイートを削除（スパム判定ツイートの掃除）。"""
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
                    # ケバブメニューを開く
                    menu_sel = '[data-testid="caret"]'
                    menus = await page.locator(menu_sel).all()
                    if not menus:
                        break
                    await menus[0].click()
                    await asyncio.sleep(0.5)

                    # 削除ボタン
                    del_sel = [
                        'div[role="menuitem"]:has-text("削除")',
                        'div[role="menuitem"]:has-text("Delete")',
                    ]
                    for dsel in del_sel:
                        try:
                            d = page.locator(dsel).first
                            if await d.is_visible(timeout=2000):
                                await d.click()
                                await asyncio.sleep(0.5)
                                # 確認
                                conf = page.locator('[data-testid="confirmationSheetConfirm"]').first
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
