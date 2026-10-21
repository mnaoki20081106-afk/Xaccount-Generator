"""
5sim.net 統合。sms-activate より X 用番号が安い ($0.04〜0.08/件)。
APIキー: https://5sim.net/settings/profile

改善点:
- get_prices() で在庫ありの国のみ取得し動的に国リストを組む
- _buy_number() にリトライ付き（429 / 503 対応）
- wait_for_sms() のコード抽出を複数パターンに対応
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

import httpx
from loguru import logger

X_PRODUCT = "twitter"

# 西側を優先 — 安国 (russia/india 等) は X に弾かれやすい
PREFERRED_COUNTRIES = [
    "usa", "england", "germany", "france", "netherlands",
    "sweden", "norway", "denmark", "finland", "austria",
    "ukraine", "poland",
]

_CODE_RE = re.compile(r"\b(\d{5,8})\b")


class FiveSimClient:
    BASE_URL = "https://5sim.net/v1"

    def __init__(self, api_key: str, preferred_countries: list[str] | None = None):
        self.api_key = api_key
        self.preferred_countries = preferred_countries or PREFERRED_COUNTRIES
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        }

    async def get_prices(self, product: str = X_PRODUCT) -> dict:
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

    async def _get_best_countries(self, product: str) -> list[str]:
        """
        価格APIを叩いて在庫あり＋preferredリスト内の国を
        コスパ順（低単価・高在庫）に並べ替えて返す。
        失敗時はフォールバックリストをそのまま返す。
        """
        try:
            prices = await self.get_prices(product)
            scored: list[tuple[float, int, str]] = []
            for country, products in prices.items():
                if country.lower() not in [c.lower() for c in self.preferred_countries]:
                    continue
                info = products.get(product, {})
                cost = float(info.get("cost", 999))
                count = int(info.get("count", 0))
                if count > 0:
                    scored.append((cost, -count, country.lower()))
            scored.sort()
            best = [c for _, _, c in scored]
            if best:
                return best
        except Exception as e:
            logger.debug(f"5sim get_best_countries error: {e}")
        return self.preferred_countries

    async def get_number(
        self, product: str = X_PRODUCT, timeout_s: int = 60
    ) -> tuple[Optional[str], Optional[str]]:
        countries = await self._get_best_countries(product)
        for country in countries:
            result = await self._buy_number(product, country)
            if result[0]:
                return result
        logger.error("5sim: 全ての国で番号取得失敗")
        return None, None

    async def _buy_number(
        self,
        product: str,
        country: str,
        retries: int = 2,
    ) -> tuple[Optional[str], Optional[str]]:
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            if attempt:
                await asyncio.sleep(2 ** attempt)

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
                        logger.info(
                            f"5sim 番号取得 country={country} "
                            f"phone=+{phone} price=${data.get('price', '?')}"
                        )
                        return order_id, phone

                    if resp.status_code == 429:
                        logger.warning(f"5sim rate limit country={country}, retry {attempt+1}")
                        await asyncio.sleep(5)
                        continue

                    if resp.status_code == 400:
                        body = resp.text.lower()
                        if "not found" in body or "no free" in body or "sold out" in body:
                            logger.debug(f"5sim country={country} 在庫なし")
                            return None, None
                        logger.debug(f"5sim country={country} 400: {resp.text[:100]}")
                        return None, None

                    if resp.status_code == 401:
                        logger.error("5sim APIキー無効")
                        return None, None

                    if resp.status_code in (502, 503, 504):
                        logger.warning(f"5sim {resp.status_code}, retry {attempt+1}")
                        continue

                except Exception as e:
                    last_exc = e
                    logger.warning(f"5sim buy error country={country} attempt={attempt}: {e}")

        if last_exc:
            logger.error(f"5sim buy failed country={country}: {last_exc}")
        return None, None

    async def wait_for_sms(
        self,
        order_id: str,
        timeout_s: int = 90,
        poll_interval: int = 5,
    ) -> Optional[str]:
        deadline = time.monotonic() + timeout_s
        async with httpx.AsyncClient(timeout=15) as client:
            while time.monotonic() < deadline:
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
                        # sms[].code 優先、なければ text からの正規表現抽出
                        for sms in reversed(sms_list):
                            code = sms.get("code", "")
                            if not code:
                                m = _CODE_RE.search(sms.get("text", ""))
                                if m:
                                    code = m.group(1)
                            if code:
                                logger.info(
                                    f"5sim SMS受信 order={order_id} code={code}"
                                )
                                await self._finish_order(client, order_id)
                                return code

                    if status in ("CANCELED", "BANNED", "TIMEOUT"):
                        logger.warning(f"5sim order={order_id} terminal status={status}")
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

    async def cancel(self, order_id: str):
        await self._cancel_order(order_id)

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
