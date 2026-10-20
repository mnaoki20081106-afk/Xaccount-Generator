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
from ..utils.profile import setup_profile
from ..email_provider.mailtm import MailTmClient

fake = Faker("ja_JP")

# X サインアップで出る FunCaptcha の public key
X_FUNCAPTCHA_KEY = "2F4F0B28-BC94-4271-8AD7-A51662E3C91C"


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
        self._captcha_client = None
        self._sms_client = None
        self._init_clients()

    def _init_clients(self):
        if self.config.captcha.provider == "capsolver" and self.config.captcha.api_key:
            from ..utils.captcha import CapSolverClient
            self._captcha_client = CapSolverClient(self.config.captcha.api_key)

        if self.config.factory.use_phone:
            from ..sms.manager import SmsManager
            self._sms_client = SmsManager(self.config.sms)

    async def create_one(self) -> Account | None:
        username = self._gen_username()
        password = self._gen_password()
        display_name = fake.name()
        email, mail_client = await self._get_email(username)

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
        phone_number = None
        activation_id = None

        # SMS番号を事前取得（use_phone=True の場合）
        sms_client_used = None
        if self.config.factory.use_phone and self._sms_client and self._sms_client.enabled:
            activation_id, phone_number, sms_client_used = await self._sms_client.get_number()
            if not phone_number:
                logger.warning(f"[{username}] SMS番号取得失敗、emailのみで続行")
                phone_number = None

        try:
            context = await self.browser.new_context(proxy_url=proxy.url)
            # サインアップは画像・フォント等を遮断して帯域最小化 (~1MB/垢)
            page = await self.browser.new_page(context, block_resources=True)

            success = await self._run_signup_flow(
                page,
                username,
                password,
                email,
                display_name,
                mail_client=mail_client,
                phone_number=phone_number,
                activation_id=activation_id,
                proxy_url=proxy.url,
            )

            if not success:
                logger.warning(f"[{username}] signup flow failed")
                if activation_id and sms_client_used:
                    await sms_client_used._cancel(activation_id)
                return None

            cookies_dir = Path("data/cookies")
            cookies_dir.mkdir(parents=True, exist_ok=True)
            cookies_path = str(cookies_dir / f"{username}.json")
            await self.browser.save_cookies(context, cookies_path)

            # auth_token と ct0 を cookies から抽出
            auth_token, ct0 = await self._extract_auth_tokens(context)

            # プロフィール完成（アイコン・bio・場所）
            if self.config.factory.setup_profile:
                # プロフィール設定は画像アップロードがあるので遮断なし
                profile_page = await self.browser.new_page(context, block_resources=False)
                await setup_profile(profile_page, username, display_name)
                await profile_page.close()

            twofa_secret = None
            if self.config.twofa.enabled:
                raw_secret = self.twofa_manager.generate_secret()
                twofa_secret = self.twofa_manager.encrypt(raw_secret)

            account = Account(
                username=username,
                password=password,
                email=email,
                phone=phone_number,
                twofa_secret=twofa_secret,
                auth_token=auth_token,
                ct0=ct0,
                status=AccountStatus.CREATED,
                proxy_id=proxy.session_id,
                fingerprint_hash=BrowserManager.fingerprint_hash(username),
                cookies_path=cookies_path,
                created_at=datetime.utcnow(),
            )
            logger.success(f"[{username}] account created phone={'yes' if phone_number else 'no'} auth_token={'yes' if auth_token else 'no'}")
            return account

        except Exception as e:
            logger.exception(f"[{username}] create failed: {e}")
            return None
        finally:
            if context:
                await context.close()
            if mail_client:
                await mail_client.close()

    async def _extract_auth_tokens(self, context) -> tuple[str | None, str | None]:
        """クッキーから auth_token と ct0 を取り出す。"""
        try:
            cookies = await context.cookies()
            auth_token = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
            ct0 = next((c["value"] for c in cookies if c["name"] == "ct0"), None)
            return auth_token, ct0
        except Exception:
            return None, None

    async def _get_email(self, username: str) -> tuple[str | None, MailTmClient | None]:
        provider = self.config.email.provider
        if not provider or provider == "mailtm":
            client = MailTmClient()
            ok = await client.create_account()
            if ok and client.address:
                return client.address, client
            await client.close()
            return None, None
        logger.warning(f"email provider '{provider}' 未実装")
        return None, None

    async def _run_signup_flow(
        self,
        page,
        username: str,
        password: str,
        email: str,
        display_name: str,
        mail_client=None,
        phone_number: str | None = None,
        activation_id: str | None = None,
        proxy_url: str | None = None,
    ) -> bool:
        try:
            await page.goto(
                "https://x.com/i/flow/signup",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await self._human_delay(1.5, 3.0)
            logger.debug(f"[{username}] signup page loaded")

            # --- Step 1: 「アカウントを作成」ボタン ---
            create_selectors = [
                'div[role="button"]:has-text("アカウントを作成")',
                'div[role="button"]:has-text("Create account")',
                '[data-testid="signupButton"]',
                'a[href="/i/flow/signup"]',
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
            await self._fill_first_visible(page, name_selectors, display_name)
            await self._human_delay(0.8, 1.5)

            # --- Step 3: 電話かメールかを選ぶ ---
            if phone_number:
                # 電話番号モード: デフォルトが電話の場合はそのまま、メールの場合は切り替え
                phone_switch = [
                    'span:has-text("代わりに電話番号を登録する")',
                    'span:has-text("Use phone instead")',
                ]
                await self._click_first_visible(page, phone_switch)
                await self._human_delay(0.5, 1.0)

                phone_selectors = [
                    'input[name="phone_number"]',
                    'input[type="tel"]',
                    'input[autocomplete="tel"]',
                ]
                formatted_phone = f"+{phone_number}"
                if not await self._fill_first_visible(page, phone_selectors, formatted_phone):
                    logger.warning(f"[{username}] phone input not found, fallback to email")
                    phone_number = None
                else:
                    logger.debug(f"[{username}] phone entered: +{phone_number}")

            if not phone_number:
                # メールモード
                email_switch_selectors = [
                    'span:has-text("代わりにメールアドレスを登録する")',
                    'span:has-text("Use email instead")',
                ]
                await self._click_first_visible(page, email_switch_selectors)
                await self._human_delay(0.5, 1.2)

                email_selectors = [
                    'input[name="email"]',
                    'input[type="email"]',
                    'input[autocomplete="email"]',
                ]
                if not await self._fill_first_visible(page, email_selectors, email):
                    logger.warning(f"[{username}] email input not found")

            await self._human_delay(0.8, 1.5)

            # --- Step 4: 生年月日 ---
            await self._fill_birthdate(page, username)
            await self._human_delay(0.5, 1.0)

            # --- Step 5: 次へ ---
            next_selectors = [
                'div[role="button"]:has-text("次へ")',
                'div[role="button"]:has-text("Next")',
                '[data-testid="ocfEnterTextNextButton"]',
            ]
            await self._click_first_visible(page, next_selectors)
            await self._human_delay(2.0, 4.0)

            # --- Step 6: Captcha が出たら解く ---
            await self._handle_captcha(page, username, proxy_url)

            # --- Step 7: メール OR SMS 認証コード ---
            code_selectors = [
                'input[data-testid="ocfEnterTextTextInput"]',
                'input[name="verification_code"]',
                'input[name="verfication_code"]',
                'input[inputmode="numeric"]',
                'input[placeholder*="コード"]',
                'input[placeholder*="code"]',
            ]
            for sel in code_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=8000):
                        code = None
                        if phone_number and activation_id and sms_client_used:
                            code = await sms_client_used.wait_for_sms(activation_id, timeout_s=120)
                        elif mail_client:
                            code = await mail_client.wait_for_code(
                                subject_keyword="Twitter", timeout=120
                            )
                        if code:
                            await loc.fill(code)
                            await self._human_delay(0.8, 1.5)
                            await self._click_first_visible(page, [
                                '[data-testid="ocfEnterTextNextButton"]',
                                'div[role="button"]:has-text("次へ")',
                                'div[role="button"]:has-text("Next")',
                            ])
                            await self._human_delay(2.0, 4.0)
                            logger.debug(f"[{username}] verification code entered: {code}")
                        else:
                            logger.warning(f"[{username}] 認証コードタイムアウト")
                            return False
                        break
                except Exception:
                    continue

            # --- Step 8: パスワード設定 ---
            pass_selectors = [
                'input[name="password"]',
                'input[type="password"]',
                'input[autocomplete="new-password"]',
            ]
            for sel in pass_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=5000):
                        await loc.fill(password)
                        await self._human_delay(0.8, 1.5)
                        await self._click_first_visible(page, next_selectors)
                        await self._human_delay(2.0, 4.0)
                        break
                except Exception:
                    continue

            # --- Step 9: ユーザー名設定 ---
            uname_selectors = [
                'input[name="username"]',
                'input[data-testid="ocfEnterTextTextInput"]',
            ]
            for sel in uname_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.is_visible(timeout=5000):
                        await loc.clear()
                        await loc.fill(username)
                        await self._human_delay(0.5, 1.0)
                        await self._click_first_visible(page, next_selectors)
                        await self._human_delay(2.0, 3.5)
                        break
                except Exception:
                    continue

            # --- 完了確認 ---
            await self._human_delay(2.0, 4.0)
            current_url = page.url
            if "home" in current_url:
                logger.success(f"[{username}] signup completed → {current_url}")
                return True

            content = await page.content()
            if username in content or display_name in content:
                logger.info(f"[{username}] signup likely succeeded (url={current_url})")
                return True

            logger.warning(f"[{username}] signup uncertain, url={current_url}")
            return False

        except Exception as e:
            logger.error(f"[{username}] signup flow error: {e}")
            return False

    async def _fill_birthdate(self, page, username: str):
        """生年月日のセレクトボックスを埋める。"""
        month = random.randint(1, 12)
        day = random.randint(1, 28)
        year = random.randint(1988, 2002)

        month_names_en = [
            "January","February","March","April","May","June",
            "July","August","September","October","November","December",
        ]
        month_names_jp = [f"{i}月" for i in range(1, 13)]

        # セレクト方式
        select_map = [
            ("select#SELECTOR_1", month_names_en[month - 1]),  # 月
            ("select#SELECTOR_2", str(day)),                    # 日
            ("select#SELECTOR_3", str(year)),                   # 年
        ]
        for sel, val in select_map:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=3000):
                    await loc.select_option(label=val)
                    await self._human_delay(0.3, 0.7)
            except Exception:
                pass

        # data-testid方式 (バリエーション)
        testid_map = [
            ('[data-testid="birthdateMonth"]', str(month)),
            ('[data-testid="birthdateDay"]', str(day)),
            ('[data-testid="birthdateYear"]', str(year)),
        ]
        for sel, val in testid_map:
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=2000):
                    await loc.select_option(value=val)
                    await self._human_delay(0.3, 0.7)
            except Exception:
                pass

        logger.debug(f"[{username}] birthdate {year}-{month:02d}-{day:02d}")

    async def _handle_captcha(self, page, username: str, proxy_url: str | None):
        """FunCaptcha が出ていたら CapSolver で解く。"""
        if not self._captcha_client:
            return
        try:
            # FunCaptcha iframe が存在するか確認
            fc_frame = page.frame_locator('iframe[src*="arkoselabs"]').first
            if await fc_frame.locator("body").count() > 0:
                logger.info(f"[{username}] FunCaptcha 検出、CapSolver で解決中...")
                token = await self._captcha_client.solve_funcaptcha(
                    website_url="https://x.com",
                    website_public_key=X_FUNCAPTCHA_KEY,
                    proxy=proxy_url,
                )
                if token:
                    # トークンをページに注入
                    await page.evaluate(
                        f"window.ArkoseEnforcement && window.ArkoseEnforcement.run('{token}')"
                    )
                    await self._human_delay(1.0, 2.0)
                    logger.debug(f"[{username}] Captcha 解決完了")
                else:
                    logger.warning(f"[{username}] Captcha 解決失敗")
        except Exception:
            pass

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
