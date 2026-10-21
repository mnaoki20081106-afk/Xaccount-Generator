"""
ツイート投稿アクション。
"""
import asyncio
import random
from typing import Optional
from loguru import logger

from ..models import Account
from ..utils.browser import BrowserManager


class TweetAction:
    def __init__(self, browser: BrowserManager):
        self.browser = browser

    async def post(self, context, text: str, account: Account) -> bool:
        page = await self.browser.new_page(context)
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # ツイートボックスをクリック
            compose_selectors = [
                '[data-testid="tweetTextarea_0"]',
                'div[aria-label="ツイートを投稿"]',
                'div[aria-label="Post text"]',
                'div[role="textbox"][data-testid="tweetTextarea_0"]',
            ]
            clicked = False
            for sel in compose_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.click()
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                logger.warning(f"[{account.username}] compose box not found")
                return False

            await asyncio.sleep(random.uniform(0.5, 1.0))

            # 文字を人間らしく入力
            await self._human_type(page, text)
            await asyncio.sleep(random.uniform(0.8, 2.0))

            # 投稿ボタン
            submit_selectors = [
                '[data-testid="tweetButtonInline"]',
                '[data-testid="tweetButton"]',
                'div[role="button"]:has-text("ポスト")',
                'div[role="button"]:has-text("Tweet")',
                'div[role="button"]:has-text("Post")',
            ]
            for sel in submit_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2000):
                        await loc.click()
                        await asyncio.sleep(random.uniform(1.5, 3.0))
                        logger.info(f"[{account.username}] tweeted: {text[:40]}...")
                        return True
                except Exception:
                    continue

            logger.warning(f"[{account.username}] post button not found")
            return False

        except Exception as e:
            logger.error(f"[{account.username}] tweet error: {e}")
            return False
        finally:
            await page.close()

    async def _human_type(self, page, text: str):
        """キーストロークに人間らしいばらつきを入れる。"""
        for char in text:
            await page.keyboard.type(char)
            await asyncio.sleep(random.uniform(0.03, 0.15))
