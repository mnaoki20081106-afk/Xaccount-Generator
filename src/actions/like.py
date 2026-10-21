"""
いいね / リツイートアクション。
タイムライン上の投稿や特定URLに対して実行する。
"""
import asyncio
import random
from loguru import logger

from ..models import Account
from ..utils.browser import BrowserManager


class LikeAction:
    def __init__(self, browser: BrowserManager):
        self.browser = browser

    async def like_by_url(self, context, tweet_url: str, account: Account) -> bool:
        page = await self.browser.new_page(context)
        try:
            await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            like_selectors = [
                '[data-testid="like"]',
                'div[aria-label*="いいね"]',
                'div[aria-label*="Like"]',
            ]
            for sel in like_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.click()
                        await asyncio.sleep(random.uniform(0.8, 1.5))
                        logger.info(f"[{account.username}] liked {tweet_url}")
                        return True
                except Exception:
                    continue

            logger.warning(f"[{account.username}] like button not found")
            return False

        except Exception as e:
            logger.error(f"[{account.username}] like error: {e}")
            return False
        finally:
            await page.close()

    async def browse_timeline(
        self, context, account: Account, scroll_count: int = 5
    ) -> bool:
        """閲覧のみ（いいねなし）— OBSERVE フェーズ用。人間らしいスクロール行動。"""
        page = await self.browser.new_page(context)
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(2.0, 5.0))
            for _ in range(scroll_count):
                # 読んでいる風にゆっくりスクロール
                await page.mouse.wheel(0, random.randint(200, 600))
                await asyncio.sleep(random.uniform(3.0, 9.0))
            logger.info(f"[{account.username}] browsed timeline ({scroll_count} scrolls)")
            return True
        except Exception as e:
            logger.error(f"[{account.username}] browse_timeline error: {e}")
            return False
        finally:
            await page.close()

    async def like_timeline_random(
        self, context, account: Account, count: int = 3
    ) -> int:
        """
        ホームタイムラインからランダムにcount件いいねする。
        成功した件数を返す。
        """
        page = await self.browser.new_page(context)
        liked = 0
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(2.0, 4.0))

            for _ in range(count):
                try:
                    # まだいいねしていないハートボタンをランダムに選ぶ
                    buttons = await page.locator('[data-testid="like"]').all()
                    if not buttons:
                        break
                    btn = random.choice(buttons[:min(len(buttons), 8)])
                    if await btn.is_visible():
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.5, 1.5))
                        await btn.click()
                        liked += 1
                        await asyncio.sleep(random.uniform(2.0, 5.0))
                except Exception:
                    continue

                # スクロールして新しい投稿を読み込む
                await page.mouse.wheel(0, random.randint(300, 700))
                await asyncio.sleep(random.uniform(1.0, 2.5))

        except Exception as e:
            logger.error(f"[{account.username}] timeline like error: {e}")
        finally:
            await page.close()

        logger.info(f"[{account.username}] liked {liked}/{count} on timeline")
        return liked

    async def retweet_by_url(self, context, tweet_url: str, account: Account) -> bool:
        page = await self.browser.new_page(context)
        try:
            await page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            rt_selectors = [
                '[data-testid="retweet"]',
                'div[aria-label*="リポスト"]',
                'div[aria-label*="Repost"]',
            ]
            for sel in rt_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.click()
                        await asyncio.sleep(random.uniform(0.5, 1.0))
                        confirm = [
                            '[data-testid="retweetConfirm"]',
                            'div[role="menuitem"]:has-text("リポスト")',
                            'div[role="menuitem"]:has-text("Repost")',
                        ]
                        for csel in confirm:
                            try:
                                cl = page.locator(csel).first
                                if await cl.is_visible(timeout=2000):
                                    await cl.click()
                                    logger.info(f"[{account.username}] retweeted {tweet_url}")
                                    return True
                            except Exception:
                                continue
                except Exception:
                    continue

            return False

        except Exception as e:
            logger.error(f"[{account.username}] retweet error: {e}")
            return False
        finally:
            await page.close()
