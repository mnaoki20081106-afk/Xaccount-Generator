"""
mail.tm 使い捨てメールAPI統合。
無料・登録不要・APIキー不要。
"""
import asyncio
import random
import string
from typing import Optional
import httpx
from loguru import logger

BASE_URL = "https://api.mail.tm"


class MailTmClient:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=20.0)
        self._token: Optional[str] = None
        self._account_id: Optional[str] = None
        self.address: Optional[str] = None
        self.password: Optional[str] = None

    async def create_account(self) -> bool:
        domains = await self._get_domains()
        if not domains:
            logger.error("[mail.tm] no domains available")
            return False

        domain = domains[0]
        local = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        self.address = f"{local}@{domain}"
        self.password = "".join(random.choices(string.ascii_letters + string.digits, k=16))

        try:
            r = await self._client.post(
                f"{BASE_URL}/accounts",
                json={"address": self.address, "password": self.password},
            )
            if r.status_code not in (200, 201):
                logger.warning(f"[mail.tm] account create failed: {r.status_code} {r.text[:200]}")
                return False

            data = r.json()
            self._account_id = data.get("id")
            logger.debug(f"[mail.tm] created {self.address}")
            return await self._login()

        except Exception as e:
            logger.error(f"[mail.tm] create error: {e}")
            return False

    async def _login(self) -> bool:
        try:
            r = await self._client.post(
                f"{BASE_URL}/token",
                json={"address": self.address, "password": self.password},
            )
            if r.status_code != 200:
                return False
            self._token = r.json().get("token")
            return bool(self._token)
        except Exception as e:
            logger.error(f"[mail.tm] login error: {e}")
            return False

    async def wait_for_code(
        self,
        subject_keyword: str = "",
        timeout: int = 120,
        poll_interval: int = 5,
    ) -> Optional[str]:
        """
        メール受信を待ち、6桁コードを抽出して返す。
        timeout秒以内に届かなければNoneを返す。
        """
        if not self._token:
            return None

        headers = {"Authorization": f"Bearer {self._token}"}
        elapsed = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                r = await self._client.get(f"{BASE_URL}/messages", headers=headers)
                if r.status_code != 200:
                    continue

                messages = r.json().get("hydra:member", [])
                for msg in messages:
                    subj = msg.get("subject", "")
                    if subject_keyword and subject_keyword.lower() not in subj.lower():
                        continue

                    msg_id = msg.get("id")
                    full = await self._get_message(msg_id, headers)
                    if full:
                        code = self._extract_code(full)
                        if code:
                            logger.debug(f"[mail.tm] code found: {code}")
                            return code

            except Exception as e:
                logger.warning(f"[mail.tm] poll error: {e}")

        logger.warning(f"[mail.tm] timeout waiting for code ({timeout}s)")
        return None

    async def _get_message(self, msg_id: str, headers: dict) -> Optional[str]:
        try:
            r = await self._client.get(f"{BASE_URL}/messages/{msg_id}", headers=headers)
            if r.status_code == 200:
                data = r.json()
                return data.get("text") or data.get("html") or ""
        except Exception:
            pass
        return None

    def _extract_code(self, text: str) -> Optional[str]:
        import re
        # 6桁数字コードを探す（Xは通常6桁）
        matches = re.findall(r"\b([0-9]{6})\b", text)
        return matches[0] if matches else None

    async def _get_domains(self) -> list[str]:
        try:
            r = await self._client.get(f"{BASE_URL}/domains")
            if r.status_code == 200:
                return [d["domain"] for d in r.json().get("hydra:member", [])]
        except Exception as e:
            logger.error(f"[mail.tm] domain fetch error: {e}")
        return []

    async def close(self):
        await self._client.aclose()
