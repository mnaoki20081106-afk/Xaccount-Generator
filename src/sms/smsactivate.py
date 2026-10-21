"""
sms-activate.io 統合。X (Twitter) の電話番号認証用。
APIキーは https://sms-activate.io のダッシュボードで取得。
料金: X用番号 $0.10〜0.30/件（国によって異なる）
"""
import asyncio
import httpx
from loguru import logger


# X (Twitter) の service ID
X_SERVICE = "tw"

# コスパのいい国コード (安い順、X登録に使える)
# ru=ロシア, in=インド, id=インドネシア, ph=フィリピン
CHEAP_COUNTRIES = ["ru", "in", "id", "ph", "ua", "kz"]


class SmsActivateClient:
    BASE_URL = "https://api.sms-activate.io/stubs/handler_api.php"

    def __init__(self, api_key: str, preferred_countries: list[str] | None = None):
        self.api_key = api_key
        self.preferred_countries = preferred_countries or CHEAP_COUNTRIES

    async def get_number(
        self, service: str = X_SERVICE, timeout_s: int = 60
    ) -> tuple[str, str] | tuple[None, None]:
        """
        番号を取得する。
        返り値: (activation_id, phone_number) or (None, None)
        電話番号は国際形式の数字のみ (例: "79123456789")
        """
        for country in self.preferred_countries:
            result = await self._request_number(service, country)
            if result:
                return result
        logger.error("全ての国で番号取得失敗")
        return None, None

    async def _request_number(
        self, service: str, country: str
    ) -> tuple[str, str] | tuple[None, None]:
        params = {
            "api_key": self.api_key,
            "action": "getNumber",
            "service": service,
            "country": country,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.get(self.BASE_URL, params=params)
                text = resp.text.strip()
                # レスポンス: "ACCESS_NUMBER:12345678:79123456789"
                if text.startswith("ACCESS_NUMBER:"):
                    parts = text.split(":")
                    activation_id = parts[1]
                    phone = parts[2]
                    logger.info(f"番号取得成功 country={country} phone=+{phone}")
                    return activation_id, phone
                elif text == "NO_NUMBERS":
                    logger.debug(f"country={country} 在庫なし")
                elif text == "NO_BALANCE":
                    logger.error("sms-activate 残高不足")
                    return None, None
                else:
                    logger.debug(f"country={country} response={text}")
            except Exception as e:
                logger.error(f"sms-activate getNumber error: {e}")
        return None, None

    async def wait_for_sms(
        self, activation_id: str, timeout_s: int = 120, poll_interval: int = 5
    ) -> str | None:
        """
        SMSコードが届くまで待つ。
        返り値: 認証コード文字列 or None
        """
        params = {
            "api_key": self.api_key,
            "action": "getStatus",
            "id": activation_id,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            for _ in range(timeout_s // poll_interval):
                await asyncio.sleep(poll_interval)
                try:
                    resp = await client.get(self.BASE_URL, params=params)
                    text = resp.text.strip()
                    # "STATUS_OK:123456"
                    if text.startswith("STATUS_OK:"):
                        code = text.split(":")[1]
                        logger.info(f"SMS受信 id={activation_id} code={code}")
                        await self._set_status(client, activation_id, 6)  # 6=完了
                        return code
                    elif text == "STATUS_CANCEL":
                        logger.warning(f"activation cancelled id={activation_id}")
                        return None
                    # STATUS_WAIT_CODE は継続
                except Exception as e:
                    logger.debug(f"getStatus error: {e}")
        logger.warning(f"SMS待機タイムアウト id={activation_id}")
        await self._cancel(activation_id)
        return None

    async def _set_status(self, client: httpx.AsyncClient, activation_id: str, status: int):
        params = {
            "api_key": self.api_key,
            "action": "setStatus",
            "id": activation_id,
            "status": status,
        }
        try:
            await client.get(self.BASE_URL, params=params)
        except Exception:
            pass

    async def _cancel(self, activation_id: str):
        params = {
            "api_key": self.api_key,
            "action": "setStatus",
            "id": activation_id,
            "status": 8,  # 8=キャンセル
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.get(self.BASE_URL, params=params)
            except Exception:
                pass

    async def get_balance(self) -> float:
        params = {"api_key": self.api_key, "action": "getBalance"}
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(self.BASE_URL, params=params)
                text = resp.text.strip()
                # "ACCESS_BALANCE:12.50"
                if text.startswith("ACCESS_BALANCE:"):
                    return float(text.split(":")[1])
            except Exception:
                pass
        return 0.0
