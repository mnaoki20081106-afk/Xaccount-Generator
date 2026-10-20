"""
Outlook / Hotmail アカウント生成モジュール。

注意:
- Microsoftの登録はCaptcha・電話認証・リスクベース判定が非常に厳しい
- 成功率はプロキシ品質・指紋・レートに大きく依存する
- このモジュールは「自動化の骨格」を提供する。実環境での調整が必須
"""
import asyncio
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from loguru import logger
from faker import Faker

from ..utils.browser import BrowserManager
from ..proxy.manager import ProxyManager
from ..config import AppConfig

fake = Faker("ja_JP")


class OutlookGenerator:
    def __init__(
        self,
        config: AppConfig,
        proxy_manager: ProxyManager,
        browser_manager: BrowserManager,
    ):
        self.config = config
        self.proxy_manager = proxy_manager
        self.browser = browser_manager

    async def create_one(self) -> Optional[dict]:
        """
        Outlookアカウントを1つ作成して結果を返す。
        成功時: {email, password, cookies_path, status}
        失敗時: None
        """
        local_part = self._gen_local_part()
        password = self._gen_password()
        # まずは outlook.com を試す（hotmail.com も可）
        desired_email = f"{local_part}@outlook.com"

        proxy = self.proxy_manager.get_proxy_for_account(f"outlook_{local_part}")
        logger.info(f"[Outlook] trying {desired_email} via session={proxy.session_id}")

        healthy = await self.proxy_manager.health_check(proxy)
        if not healthy:
            logger.warning("[Outlook] proxy unhealthy")
            return None

        context = None
        try:
            context = await self.browser.new_context(proxy_url=proxy.url)
            page = await self.browser.new_page(context)

            success, final_email = await self._run_signup_flow(
                page, local_part, password
            )

            if not success or not final_email:
                logger.warning(f"[Outlook] signup failed for {local_part}")
                return None

            cookies_dir = Path("data/cookies/outlook")
            cookies_dir.mkdir(parents=True, exist_ok=True)
            cookies_path = str(cookies_dir / f"{local_part}.json")
            await self.browser.save_cookies(context, cookies_path)

            result = {
                "email": final_email,
                "password": password,
                "cookies_path": cookies_path,
                "proxy_id": proxy.session_id,
                "created_at": datetime.utcnow().isoformat(),
                "status": "created",
            }
            logger.success(f"[Outlook] created {final_email}")
            return result

        except Exception as e:
            logger.exception(f"[Outlook] error: {e}")
            return None
        finally:
            if context:
                await context.close()

    async def _run_signup_flow(
        self, page, local_part: str, password: str
    ) -> tuple[bool, Optional[str]]:
        """
        Microsoftアカウント登録フローの骨格。
        セレクタは変更されやすいので実環境で要調整。
        """
        try:
            # 公式のサインアップ入口
            await page.goto(
                "https://signup.live.com/",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await self._delay(2.0, 4.0)
            logger.debug("[Outlook] signup page loaded")

            # メール入力欄
            email_selectors = [
                'input[type="email"]',
                'input[name="MemberName"]',
                '#MemberName',
                'input[aria-label*="mail"]',
            ]
            if not await self._fill_first(page, email_selectors, local_part):
                # ドメイン選択が別の場合がある
                logger.debug("[Outlook] trying alternative email fill")

            await self._delay(0.8, 1.5)

            # 次へ
            next_selectors = [
                'input[type="submit"]',
                'button[type="submit"]',
                '#iSignupAction',
                'input[value*="次"]',
                'input[value*="Next"]',
            ]
            await self._click_first(page, next_selectors)
            await self._delay(2.0, 3.5)

            # パスワード入力
            pass_selectors = [
                'input[type="password"]',
                'input[name="Password"]',
                '#PasswordInput',
            ]
            if not await self._fill_first(page, pass_selectors, password):
                logger.warning("[Outlook] password field not found")
                return False, None

            await self._delay(0.8, 1.5)
            await self._click_first(page, next_selectors)
            await self._delay(2.0, 4.0)

            # 名前
            first_name = fake.first_name()
            last_name = fake.last_name()
            await self._fill_first(
                page,
                ['input[name="FirstName"]', '#FirstName'],
                first_name,
            )
            await self._delay(0.4, 0.9)
            await self._fill_first(
                page,
                ['input[name="LastName"]', '#LastName'],
                last_name,
            )
            await self._delay(0.6, 1.2)
            await self._click_first(page, next_selectors)
            await self._delay(2.0, 3.5)

            # 生年月日（セレクトが多い）
            # ここでは存在するセレクトを埋める試みだけする
            try:
                await self._try_fill_birthdate(page)
            except Exception:
                pass

            await self._click_first(page, next_selectors)
            await self._delay(3.0, 5.0)

            # ここから先に Captcha / 電話認証 / 追加確認 が入りやすい
            # 成功判定の簡易チェック
            content = await page.content()
            current_url = page.url

            if "account.microsoft" in current_url or "privacynotice" in current_url.lower():
                final_email = f"{local_part}@outlook.com"
                logger.info(f"[Outlook] possible success → {final_email}")
                return True, final_email

            # 電話番号要求やCaptchaが出ているかログに残す
            if any(k in content.lower() for k in ["phone", "電話", "captcha", "ロボット"]):
                logger.warning("[Outlook] phone or captcha challenge detected")
                return False, None

            logger.warning("[Outlook] could not confirm success")
            return False, None

        except Exception as e:
            logger.error(f"[Outlook] flow error: {e}")
            return False, None

    async def _try_fill_birthdate(self, page):
        """生年月日セレクトを埋める簡易処理。"""
        # 年
        year = str(random.randint(1988, 2002))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 28))

        for sel, val in [
            ('select[name="BirthYear"]', year),
            ('select[name="BirthMonth"]', month),
            ('select[name="BirthDay"]', day),
            ('#BirthYear', year),
            ('#BirthMonth', month),
            ('#BirthDay', day),
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1000):
                    await loc.select_option(val)
            except Exception:
                continue

    async def _fill_first(self, page, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2500):
                    await loc.fill(value)
                    return True
            except Exception:
                continue
        return False

    async def _click_first(self, page, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2500):
                    await loc.click()
                    return True
            except Exception:
                continue
        return False

    async def _delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    def _gen_local_part(self) -> str:
        base = fake.user_name().replace(".", "").replace("_", "")[:8].lower()
        suffix = fake.random_number(digits=3)
        return f"{base}{suffix}"

    def _gen_password(self) -> str:
        # Microsoftのパスワード要件を満たす強めのもの
        return fake.password(length=16, special_chars=True, digits=True, upper_case=True)
