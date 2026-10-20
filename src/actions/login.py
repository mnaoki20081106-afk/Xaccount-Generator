"""
Xへのログイン。クッキーがあればそれを使い、なければID/PW+2FAで再ログイン。
"""
import asyncio
import random
from typing import Optional
from pathlib import Path
from loguru import logger

from ..models import Account
from ..utils.browser import BrowserManager
from ..auth.twofa import TwoFAManager


class LoginAction:
    def __init__(self, browser: BrowserManager, twofa: TwoFAManager, enc_key: str):
        self.browser = browser
        self.twofa = twofa
        self.enc_key = enc_key

    async def login(self, account: Account, proxy_url: Optional[str] = None):
        """
        ログイン済みコンテキストを返す。
        呼び出し元で context.close() すること。
        """
        context = await self.browser.new_context(proxy_url=proxy_url)

        if account.cookies_path and Path(account.cookies_path).exists():
            await self.browser.load_cookies(context, account.cookies_path)
            page = await self.browser.new_page(context)
            if await self._is_logged_in(page):
                logger.debug(f"[{account.username}] cookie login OK")
                await page.close()
                return context
            await page.close()
            logger.debug(f"[{account.username}] cookies expired, re-login")

        page = await self.browser.new_page(context)
        ok = await self._do_login(page, account)
        await page.close()

        if not ok:
            await context.close()
            return None

        if account.cookies_path:
            await self.browser.save_cookies(context, account.cookies_path)

        return context

    async def _is_logged_in(self, page) -> bool:
        try:
            await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            url = page.url
            return "home" in url and "login" not in url
        except Exception:
            return False

    async def _do_login(self, page, account: Account) -> bool:
        try:
            await page.goto(
                "https://x.com/i/flow/login",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await asyncio.sleep(random.uniform(1.5, 3.0))

            # ユーザー名 / メール
            id_selectors = [
                'input[name="text"]',
                'input[autocomplete="username"]',
                'input[data-testid="ocfEnterTextTextInput"]',
            ]
            entered = False
            for sel in id_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.fill(account.username)
                        entered = True
                        break
                except Exception:
                    continue

            if not entered:
                logger.warning(f"[{account.username}] username field not found")
                return False

            await asyncio.sleep(random.uniform(0.8, 1.5))

            next_sel = [
                'div[role="button"]:has-text("次へ")',
                'div[role="button"]:has-text("Next")',
                '[data-testid="LoginForm_Login_Button"]',
            ]
            for sel in next_sel:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2000):
                        await loc.click()
                        break
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(1.5, 2.5))

            # パスワード
            pass_selectors = [
                'input[name="password"]',
                'input[type="password"]',
            ]
            for sel in pass_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        await loc.fill(account.password)
                        break
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(0.5, 1.2))

            login_sel = [
                'div[role="button"]:has-text("ログイン")',
                'div[role="button"]:has-text("Log in")',
                '[data-testid="LoginForm_Login_Button"]',
            ]
            for sel in login_sel:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=2000):
                        await loc.click()
                        break
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(2.0, 4.0))

            # 2FA チャレンジ
            twofa_sel = [
                'input[name="text"][data-testid="ocfEnterTextTextInput"]',
                'input[inputmode="numeric"]',
            ]
            for sel in twofa_sel:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=3000):
                        if account.twofa_secret:
                            raw = self.twofa.decrypt(account.twofa_secret)
                            code = self.twofa.get_totp_code(raw)
                            await loc.fill(code)
                            await asyncio.sleep(0.5)
                            for conf in ['div[role="button"]:has-text("次へ")', 'div[role="button"]:has-text("Next")', '[data-testid="ocfEnterTextNextButton"]']:
                                try:
                                    cl = page.locator(conf).first
                                    if await cl.is_visible(timeout=1500):
                                        await cl.click()
                                        break
                                except Exception:
                                    continue
                        break
                except Exception:
                    continue

            await asyncio.sleep(random.uniform(2.0, 3.5))

            url = page.url
            if "home" in url or "twitter.com" in url:
                logger.info(f"[{account.username}] login success")
                return True

            logger.warning(f"[{account.username}] login failed, url={url}")
            return False

        except Exception as e:
            logger.error(f"[{account.username}] login error: {e}")
            return False
