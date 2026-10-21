"""
フォロー / アンフォローアクション。
"""
import asyncio
import random
from loguru import logger

from ..models import Account
from ..utils.browser import BrowserManager


class FollowAction:
    def __init__(self, browser: BrowserManager):
        self.browser = browser

    async def follow(self, context, target_username: str, account: Account) -> bool:
        page = await self.browser.new_page(context)
        try:
            url = f"https://x.com/{target_username.lstrip('@')}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            follow_selectors = [
                f'[data-testid="{target_username.lstrip("@")}-follow"]',
                'div[role="button"]:has-text("フォロー"):not(:has-text("フォロー中"))',
                'div[role="button"]:has-text("Follow"):not(:has-text("Following"))',
            ]
            for sel in follow_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.click()
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        logger.info(f"[{account.username}] followed @{target_username}")
                        return True
                except Exception:
                    continue

            logger.warning(f"[{account.username}] follow button not found for @{target_username}")
            return False

        except Exception as e:
            logger.error(f"[{account.username}] follow error: {e}")
            return False
        finally:
            await page.close()

    async def follow_suggestions(
        self, context, account: Account, count: int = 2
    ) -> int:
        """X の「おすすめ」ページからフォロー。ターゲット一覧不要。"""
        page = await self.browser.new_page(context)
        followed = 0
        try:
            await page.goto(
                "https://x.com/i/connect_people",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await asyncio.sleep(random.uniform(2.0, 4.0))

            for sel in [
                'div[role="button"]:has-text("フォロー"):not(:has-text("フォロー中"))',
                'div[role="button"]:has-text("Follow"):not(:has-text("Following"))',
            ]:
                try:
                    btns = await page.locator(sel).all()
                    for btn in btns[:count - followed]:
                        if await btn.is_visible():
                            await btn.scroll_into_view_if_needed()
                            await asyncio.sleep(random.uniform(1.0, 2.5))
                            await btn.click()
                            followed += 1
                            await asyncio.sleep(random.uniform(2.0, 5.0))
                            if followed >= count:
                                break
                except Exception:
                    continue
                if followed >= count:
                    break

            logger.info(f"[{account.username}] followed {followed} suggestions")
        except Exception as e:
            logger.error(f"[{account.username}] follow_suggestions error: {e}")
        finally:
            await page.close()
        return followed

    async def unfollow(self, context, target_username: str, account: Account) -> bool:
        page = await self.browser.new_page(context)
        try:
            url = f"https://x.com/{target_username.lstrip('@')}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            unfollow_selectors = [
                f'[data-testid="{target_username.lstrip("@")}-unfollow"]',
                'div[role="button"]:has-text("フォロー中")',
                'div[role="button"]:has-text("Following")',
            ]
            for sel in unfollow_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.click()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        # 確認ダイアログ
                        confirm_sel = [
                            'div[role="button"]:has-text("フォローをやめる")',
                            'div[role="button"]:has-text("Unfollow")',
                            '[data-testid="confirmationSheetConfirm"]',
                        ]
                        for csel in confirm_sel:
                            try:
                                cl = page.locator(csel).first
                                if await cl.is_visible(timeout=2000):
                                    await cl.click()
                                    break
                            except Exception:
                                continue
                        logger.info(f"[{account.username}] unfollowed @{target_username}")
                        return True
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.error(f"[{account.username}] unfollow error: {e}")
            return False
        finally:
            await page.close()
