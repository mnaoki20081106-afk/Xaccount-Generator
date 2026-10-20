"""
アカウント生成の骨格。
実際のX登録フローはここを拡張する。
"""
from datetime import datetime
from loguru import logger
from faker import Faker

from ..models import Account, AccountStatus
from ..proxy.manager import ProxyManager
from ..auth.twofa import TwoFAManager
from ..config import AppConfig

fake = Faker("ja_JP")


class AccountFactory:
    def __init__(
        self,
        config: AppConfig,
        proxy_manager: ProxyManager,
        twofa_manager: TwoFAManager,
    ):
        self.config = config
        self.proxy_manager = proxy_manager
        self.twofa_manager = twofa_manager

    async def create_one(self) -> Account | None:
        """
        1アカウント生成を試みる。
        現時点では骨格のみ。実際のPlaywrightフローは次のステップで実装。
        """
        username = self._gen_username()
        password = self._gen_password()
        email = f"{username}@example.com"  # 後で本物のメールプロバイダに差し替え

        proxy = self.proxy_manager.get_proxy_for_account(username)
        logger.info(f"Creating account {username} via proxy session {proxy.session_id}")

        # TODO: PlaywrightでX登録フローを実行
        # 成功したら cookies 保存、必要なら2FA有効化

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
            created_at=datetime.utcnow(),
        )
        logger.success(f"Account skeleton created: {username}")
        return account

    def _gen_username(self) -> str:
        base = fake.user_name().replace(".", "")[:10]
        suffix = fake.random_number(digits=3)
        return f"{base}{suffix}"

    def _gen_password(self) -> str:
        return fake.password(length=14, special_chars=True)
