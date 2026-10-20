"""
SMS プロバイダーマネージャー。
5sim と sms-activate を両方持ちで、残高があって安い方を使う。
"""
from loguru import logger
from ..config import SmsConfig


class SmsManager:
    def __init__(self, config: SmsConfig):
        self.config = config
        self._clients: list = []
        self._init_clients()

    def _init_clients(self):
        if not self.config.api_key:
            return

        provider = self.config.provider.lower()

        if provider == "5sim" or provider == "fivesim":
            from .fivesim import FiveSimClient
            self._clients.append(
                FiveSimClient(self.config.api_key, self.config.preferred_countries)
            )
        elif provider == "smsactivate":
            from .smsactivate import SmsActivateClient
            self._clients.append(
                SmsActivateClient(self.config.api_key, self.config.preferred_countries)
            )
        elif provider == "auto":
            # 両方設定されていれば両方試す (api_key は primary, api_key2 は secondary)
            from .fivesim import FiveSimClient
            from .smsactivate import SmsActivateClient
            if self.config.api_key:
                self._clients.append(
                    FiveSimClient(self.config.api_key, self.config.preferred_countries)
                )
            if self.config.api_key2:
                self._clients.append(
                    SmsActivateClient(self.config.api_key2, self.config.preferred_countries)
                )

        if not self._clients:
            logger.warning(f"SMS provider '{provider}' 未対応 or APIキー未設定")

    async def get_number(self) -> tuple[str | None, str | None, object | None]:
        """
        番号を取得。どのプロバイダーを使ったかを返すためにクライアントも返す。
        返り値: (order_id, phone_number, client) or (None, None, None)
        """
        for client in self._clients:
            try:
                balance = await client.get_balance()
                if balance < 0.05:
                    logger.warning(f"{type(client).__name__} 残高不足 (${balance:.2f}), 次のプロバイダーへ")
                    continue
                order_id, phone = await client.get_number()
                if order_id and phone:
                    return order_id, phone, client
            except Exception as e:
                logger.error(f"{type(client).__name__} error: {e}")
                continue
        return None, None, None

    @property
    def enabled(self) -> bool:
        return len(self._clients) > 0
