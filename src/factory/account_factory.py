"""
アカウント生成。
Playwrightでプロキシ経由のX登録フローを実行する。
セレクタはXのUI変更で壊れやすいので、動作確認しながら調整すること。
"""
from datetime import datetime
from pathlib import Path
import random
import asyncio
from loguru import logger
from faker import Faker

from ..models import Account, AccountStatus
from ..proxy.manager import ProxyManager
from ..auth.twofa import TwoFAManager
from ..config import AppConfig
from ..utils.browser import BrowserManager

fake = Faker("ja_JP")


class AccountFactory:
    def __init__(
        self,
        config: AppConfig,
        proxy_manager: ProxyManager,
        twofa_manager: TwoFAManager,
        browser_manager: BrowserManager,
    ):
        self.config = config
        self.proxy_manager = proxy_manager
        self.twofa_manager = twofa_manager
        self.browser = browser_manager

    async def create_one(self) -> Account | None:
        username = self._gen_username()
        password = self._gen_password()
        display_name = fake.name()
        email = await self._get_email(username)

        if not email:
            logger.error(f"[{username}] email取得失敗")
            return None

        proxy = self.proxy_manager.get_proxy_for_account(username)
        logger.info(f"[{username}] proxy session={proxy.session_id}")

        healthy = await self.proxy_manager.health_check(proxy)
        if not healthy:
            logger.warning(f"[{username}] proxy unhealthy, skip")
            return None

        context = None
        try:
            context = await self.browser.new_context(proxy_url=proxy.url)
            page = await self.browser.new_page(context)

            success = await self._run_signup_flow(
                page, username, password, email, display_name
            )

            if not success:
                logger.warning(f"[{username}] signup flow failed")
                return None

            cookies_dir = Path("data/cookies")
            cookies_dir.mkdir(parents=True, exist_ok=True)
            cookies_path = str(cookies_dir / f"{username}.json")
            await self.browser.save_cookies(context, cookies_path)

            twofa_secret = None
            if self.config.twofa.enabled:
                raw_secret = self.twofa_manager.generate_secret()
                twofa_secret = self.twofa_manager.encrypt(raw_secret)

            account = Account(
                username=username,
                password=password,
                email=email,
                twofa_secret=twofa_secret,
                status=AccountStatus.CREATED,
                proxy_id=proxy.session_id,
                fingerprint_hash=BrowserManager.fingerprint_hash(username),
                cookies_path=cookies_path,
                created_at=datetime.utcnow(),
            )
            logger.success(f"[{username}] account created")
            return account

        except Exception as e:
            logger.exception(f"[{username}] create failed: {e}")
            return None
        finally:
            if context:
                await context.close()

    async def _get_email(self, username: str) -> str | None:
        """
        メール取得。
        現状はプレースホルダ。
        後で temp-mail / mail.tm / 有料API などに差し替える。
        """
        provider = self.config.email.provider
        if not provider:
            # 開発用ダミー
            return f"{username}@mail.example.com"
        # TODO: providerごとの実装
        logger.warning("email provider 未実装")
        return None

    async def _run_signup_flow(
        self,
        page,
        username: str,
        password: str,
        email: str,
        display_name: str,
    ) -> bool:
        """
        Xサインアップの現実的なステップ骨格。

        注意:
        - XはUI・セレクタを頻繁に変える
        - 電話番号要求・Captcha・異常検知が挟まる
        - このコードは「流れ」を示すもの。実環境でセレクタを合わせて調整が必要
        """
        try:
            await page.goto(
                "https://x.com/i/flow/signup",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await self._human_delay(1.5, 3.0)
            logger.debug(f"[{username}] signup page loaded")

            # --- Step 1: 「アカウントを作成」系のボタンを探す ---
            # 言語によってテキストが違うので複数候補を試す
            create_selectors = [
                'div[role="button"]:has-text("アカウントを作成")',
                'div[role="button"]:has-text("Create account")',
                'span:has-text("アカウントを作成")',
                'span:has-text("Create account")',
                '[data-testid="signupButton"]',
            ]
            clicked = await self._click_first_visible(page, create_selectors)
            if clicked:
                await self._human_delay(1.0, 2.0)

            # --- Step 2: 名前入力 ---
            name_selectors = [
                'input[name="name"]',
                'input[autocomplete="name"]',
                'input[data-testid="ocfEnterTextTextInput"]',
            ]
            if not await self._fill_first_visible(page, name_selectors, display_name):
                logger.warning(f"[{username}] name input not found")
                # 続行を試みる

            await self._human_delay(0.8, 1.5)

            # --- Step 3: メールに切り替え（電話がデフォルトの場合） ---
            email_switch_selectors = [
                'span:has-text("代わりにメールアドレスを登録する")',
                'span:has-text("Use email instead")',
                'div[role="button"]:has-text("メール")',
            ]
            await self._click_first_visible(page, email_switch_selectors)
            await self._human_delay(0.5, 1.2)

            # --- Step 4: メール入力 ---
            email_selectors = [
                'input[name="email"]',
                'input[type="email"]',
                'input[autocomplete="email"]',
            ]
            if not await self._fill_first_visible(page, email_selectors, email):
                logger.warning(f"[{username}] email input not found")

            await self._human_delay(0.8, 1.5)

            # --- Step 5: 生年月日（必要なら） ---
            # Xは月・日・年のセレクトが多い
            try:
                # 月
                month = random.randint(1, 12)
                day = random.randint(1, 28)
                year = random.randint(1988, 2002)
                # セレクトの実装はUI依存のため、ここではログのみ
                logger.debug(f"[{username}] birthdate candidate {year}-{month:02d}-{day:02d}")
            except Exception:
                pass

            await self._human_delay(0.5, 1.0)

            # --- Step 6: 次へ / Next ---
            next_selectors = [
                'div[role="button"]:has-text("次へ")',
                'div[role="button"]:has-text("Next")',
                '[data-testid="ocfEnterTextNextButton"]',
            ]
            await self._click_first_visible(page, next_selectors)
            await self._human_delay(2.0, 4.0)

            # --- ここから先はメール認証コード入力・パスワード設定などが続く ---
            # メールプロバイダ連携ができてから本実装する
            logger.info(
                f"[{username}] basic form steps done. "
                "メール認証・パスワード設定は次フェーズで実装。"
            )

            # 現時点では「フォームまで到達できた」ことを成功扱い（開発用）
            # 本番では認証完了まで確認して True を返す
            return True

        except Exception as e:
            logger.error(f"[{username}] signup flow error: {e}")
            return False

    async def _click_first_visible(self, page, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.click()
                    return True
            except Exception:
                continue
        return False

    async def _fill_first_visible(self, page, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(value)
                    return True
            except Exception:
                continue
        return False

    async def _human_delay(self, min_s: float, max_s: float):
        await asyncio.sleep(random.uniform(min_s, max_s))

    def _gen_username(self) -> str:
        base = fake.user_name().replace(".", "").replace("_", "")[:8]
        suffix = fake.random_number(digits=4)
        return f"{base}{suffix}"

    def _gen_password(self) -> str:
        return fake.password(length=14, special_chars=True)
