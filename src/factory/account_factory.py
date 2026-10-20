"""
アカウント生成。
Playwrightでブラウザを起動し、プロキシ経由でX登録フローの骨格を実行する。
実際のセレクタやフローはXのUI変更に合わせて随時調整が必要。
"""
from datetime import datetime
from pathlib import Path
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
        email = f"{username}@example.com"  # TODO: 本物のメールプロバイダに差し替え

        proxy = self.proxy_manager.get_proxy_for_account(username)
        logger.info(f"[{username}] proxy session={proxy.session_id}")

        # ヘルスチェック（失敗したらスキップ）
        healthy = await self.proxy_manager.health_check(proxy)
        if not healthy:
            logger.warning(f"[{username}] proxy unhealthy, skip")
            return None

        context = None
        try:
            context = await self.browser.new_context(proxy_url=proxy.url)
            page = await self.browser.new_page(context)

            # --- X登録フロー（骨格） ---
            # 実際のセレクタはXの現在のUIに合わせて書き換えること
            success = await self._run_signup_flow(page, username, password, email)

            if not success:
                logger.warning(f"[{username}] signup flow failed")
                return None

            # cookies保存
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

    async def _run_signup_flow(
        self, page, username: str, password: str, email: str
    ) -> bool:
        """
        Xのサインアップフロー骨格。
        現状はページ到達確認まで。実際の入力・送信は次のステップで実装する。
        """
        try:
            await page.goto("https://x.com/i/flow/signup", wait_until="domcontentloaded")
            logger.debug(f"[{username}] reached signup page")

            # ここから先はXのUIに依存するセレクタを使って実装する
            # 例（仮）:
            # await page.get_by_text("アカウントを作成").click()
            # await page.locator('input[name="name"]').fill(fake.name())
            # await page.locator('input[name="email"]').fill(email)
            # ...

            # 現時点では到達できたことを成功とみなす（テスト用）
            # 本番では実際にアカウントが作れたかで判定する
            await page.wait_for_timeout(2000)
            return True

        except Exception as e:
            logger.error(f"[{username}] signup flow error: {e}")
            return False

    def _gen_username(self) -> str:
        base = fake.user_name().replace(".", "").replace("_", "")[:8]
        suffix = fake.random_number(digits=4)
        return f"{base}{suffix}"

    def _gen_password(self) -> str:
        return fake.password(length=14, special_chars=True)
