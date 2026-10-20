"""
CapSolver 統合。X サインアップで出る FunCaptcha を解く。
capsolver.com のAPIキーが必要 ($0.6/1000件)。
"""
import asyncio
import httpx
from loguru import logger


class CapSolverClient:
    BASE_URL = "https://api.capsolver.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        """
        FunCaptcha (Arkose Labs) を解く。
        X の場合: website_public_key = "2F4F0B28-BC94-4271-8AD7-A51662E3C91C"
        返り値: token 文字列 or None (失敗)
        """
        payload: dict = {
            "clientKey": self.api_key,
            "task": {
                "type": "FunCaptchaTaskProxyLess",
                "websiteURL": website_url,
                "websitePublicKey": website_public_key,
            },
        }
        if proxy:
            payload["task"]["type"] = "FunCaptchaTask"
            # proxy format: "http://user:pass@host:port"
            from urllib.parse import urlparse
            p = urlparse(proxy)
            payload["task"]["proxyType"] = p.scheme
            payload["task"]["proxyAddress"] = p.hostname
            payload["task"]["proxyPort"] = p.port
            if p.username:
                payload["task"]["proxyLogin"] = p.username
                payload["task"]["proxyPassword"] = p.password

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(f"{self.BASE_URL}/createTask", json=payload)
                data = resp.json()
                if data.get("errorId"):
                    logger.error(f"CapSolver createTask error: {data.get('errorDescription')}")
                    return None
                task_id = data["taskId"]
            except Exception as e:
                logger.error(f"CapSolver createTask failed: {e}")
                return None

            # poll for result
            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                try:
                    result = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rdata = result.json()
                    if rdata.get("status") == "ready":
                        token = rdata["solution"].get("token")
                        logger.debug(f"CapSolver solved: {token[:30]}...")
                        return token
                    if rdata.get("errorId"):
                        logger.error(f"CapSolver error: {rdata.get('errorDescription')}")
                        return None
                except Exception:
                    continue

        logger.warning("CapSolver timeout")
        return None

    async def solve_recaptcha_v2(
        self, website_url: str, website_key: str, proxy: str | None = None, timeout: int = 120
    ) -> str | None:
        payload: dict = {
            "clientKey": self.api_key,
            "task": {
                "type": "ReCaptchaV2TaskProxyLess",
                "websiteURL": website_url,
                "websiteKey": website_key,
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(f"{self.BASE_URL}/createTask", json=payload)
                data = resp.json()
                if data.get("errorId"):
                    return None
                task_id = data["taskId"]
            except Exception as e:
                logger.error(f"CapSolver reCAPTCHA failed: {e}")
                return None

            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                try:
                    result = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rdata = result.json()
                    if rdata.get("status") == "ready":
                        return rdata["solution"].get("gRecaptchaResponse")
                    if rdata.get("errorId"):
                        return None
                except Exception:
                    continue
        return None
