"""
5sim.net 統合。sms-activate より X 用番号が安い ($0.04〜0.08/件)。
APIキー: https://5sim.net/settings/profile
"""
import asyncio
import httpx
from loguru import logger

X_PRODUCT = "twitter"

# 西側を優先 — 安国 (russia/india 等) は X に弾かれやすい
CHEAP_COUNTRIES = ["usa", "england", "germany", "france", "netherlands",
                   "ukraine", "india", "indonesia", "philippines"]


class FiveSimClient:
    BASE_URL = "https://5sim.net/v1"

    def __init__(self, api_key: str, preferred_countries: list[str] | None = None):
        self.api_key = api_key
        self.preferred_countries = preferred_countries or CHEAP_COUNTRIES
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    async def get_prices(self, product: str = X_PRODUCT) -> dict:
        """国別の価格一覧を取得して安い順にソート。"""
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/guest/prices",
                    params={"product": product},
                    headers=self._headers,
                )
                return resp.json()
            except Exception as e:
                logger.error(f"5sim prices error: {e}")
                return {}

    async def get_number(
        self, product: str = X_PRODUCT, timeout_s: int = 60
    ) -> tuple[str, str] | tuple[None, None]:
        """
        最安の国から番号を取得。
        返り値: (order_id, phone_number) or (None, None)
        電話番号は国際形式 e.g. "79123456789"
        """
        for country in self.preferred_countries:
            result = await self._buy_number(product, country)
            if result:
                return result
        logger.error("5sim: 全ての国で番号取得失敗")
        return None, None

    async def _buy_number(
        self, product: str, country: str
    ) -> tuple[str, str] | tuple[None, None]:
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/user/buy/activation/{country}/any/{product}",
                    headers=self._headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    order_id = str(data["id"])
                    phone = data["phone"].lstrip("+")
                    logger.info(f"5sim 番号取得 country={country} phone=+{phone} price=${data.get('price', '?')}")
                    return order_id, phone
                elif resp.status_code == 400:
                    text = resp.text
                    if "not found" in text.lower() or "no free" in text.lower():
                        logger.debug(f"5sim country={country} 在庫なし")
                    else:
                        logger.debug(f"5sim country={country} status=400 {text[:80]}")
                elif resp.status_code == 401:
                    logger.error("5sim APIキー無効")
                    return None, None
            except Exception as e:
                logger.error(f"5sim buy error country={country}: {e}")
        return None, None

    async def wait_for_sms(
        self, order_id: str, timeout_s: int = 90, poll_interval: int = 5
    ) -> str | None:
        """SMSコードが届くまでポーリング。"""
        async with httpx.AsyncClient(timeout=15) as client:
            for _ in range(timeout_s // poll_interval):
                await asyncio.sleep(poll_interval)
                try:
                    resp = await client.get(
                        f"{self.BASE_URL}/user/check/{order_id}",
                        headers=self._headers,
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    status = data.get("status", "")
                    sms_list = data.get("sms", [])
                    if sms_list:
                        code = sms_list[-1].get("code", "")
                        if code:
                            logger.info(f"5sim SMS受信 order={order_id} code={code}")
                            await self._finish_order(client, order_id)
                            return code
                    if status in ("CANCELED", "BANNED", "TIMEOUT"):
                        logger.warning(f"5sim order={order_id} status={status}")
                        return None
                except Exception as e:
                    logger.debug(f"5sim check error: {e}")
        logger.warning(f"5sim SMS待機タイムアウト order={order_id}")
        await self._cancel_order(order_id)
        return None

    async def _finish_order(self, client: httpx.AsyncClient, order_id: str):
        try:
            await client.get(
                f"{self.BASE_URL}/user/finish/{order_id}",
                headers=self._headers,
            )
        except Exception:
            pass

    async def _cancel_order(self, order_id: str):
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                await client.get(
                    f"{self.BASE_URL}/user/cancel/{order_id}",
                    headers=self._headers,
                )
            except Exception:
                pass

    async def get_balance(self) -> float:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.get(
                    f"{self.BASE_URL}/user/profile",
                    headers=self._headers,
                )
                if resp.status_code == 200:
                    return float(resp.json().get("balance", 0))
            except Exception:
                pass
        return 0.0
