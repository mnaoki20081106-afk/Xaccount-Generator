"""
キャプチャソルバー統合。CapSolver / 2Captcha / AntiCaptcha を共通 IF で扱う。
"""
import asyncio
from urllib.parse import urlparse
import httpx
from loguru import logger


def _proxy_parts(proxy: str | None) -> dict:
    """proxy URL → provider API 用フィールド dict。"""
    if not proxy:
        return {}
    p = urlparse(proxy)
    out = {
        "proxyType": p.scheme or "http",
        "proxyAddress": p.hostname,
        "proxyPort": p.port or 80,
    }
    if p.username:
        out["proxyLogin"] = p.username
        out["proxyPassword"] = p.password or ""
    return out


# ──────────────────────────────────────────────────────────────
# CapSolver
# ──────────────────────────────────────────────────────────────

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
        task: dict = {
            "type": "FunCaptchaTaskProxyLess",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if proxy:
            task["type"] = "FunCaptchaTask"
            task.update(_proxy_parts(proxy))

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.BASE_URL}/createTask",
                    json={"clientKey": self.api_key, "task": task},
                )
                data = resp.json()
                if data.get("errorId"):
                    logger.error(f"CapSolver createTask: {data.get('errorDescription')}")
                    return None
                task_id = data["taskId"]
            except Exception as e:
                logger.error(f"CapSolver createTask failed: {e}")
                return None

            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                try:
                    r = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    if rd.get("status") == "ready":
                        token = rd["solution"].get("token")
                        logger.debug(f"CapSolver solved: {token[:30]}...")
                        return token
                    if rd.get("errorId"):
                        logger.error(f"CapSolver error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("CapSolver timeout")
        return None

    async def solve_recaptcha_v2(
        self, website_url: str, website_key: str,
        proxy: str | None = None, timeout: int = 120,
    ) -> str | None:
        task: dict = {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.BASE_URL}/createTask",
                    json={"clientKey": self.api_key, "task": task},
                )
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
                    r = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    if rd.get("status") == "ready":
                        return rd["solution"].get("gRecaptchaResponse")
                    if rd.get("errorId"):
                        return None
                except Exception:
                    continue
        return None


# ──────────────────────────────────────────────────────────────
# 2Captcha
# ──────────────────────────────────────────────────────────────

class TwoCaptchaClient:
    """2captcha.com API。CapSolver とほぼ同じ createTask/getTaskResult 構造。"""

    BASE_URL = "https://api.2captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        task: dict = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if proxy:
            task["type"] = "FunCaptchaTask"
            parts = _proxy_parts(proxy)
            task["proxyType"] = parts.get("proxyType", "http")
            task["proxyAddress"] = parts.get("proxyAddress", "")
            task["proxyPort"] = parts.get("proxyPort", 80)
            if "proxyLogin" in parts:
                task["proxyLogin"] = parts["proxyLogin"]
                task["proxyPassword"] = parts.get("proxyPassword", "")

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.BASE_URL}/createTask",
                    json={"clientKey": self.api_key, "task": task},
                )
                data = resp.json()
                if data.get("errorId"):
                    logger.error(f"2Captcha createTask: {data.get('errorDescription')}")
                    return None
                task_id = data["taskId"]
            except Exception as e:
                logger.error(f"2Captcha createTask failed: {e}")
                return None

            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                try:
                    r = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    if rd.get("status") == "ready":
                        token = rd["solution"].get("token")
                        logger.debug(f"2Captcha solved: {token[:30] if token else 'None'}...")
                        return token
                    if rd.get("errorId"):
                        logger.error(f"2Captcha error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("2Captcha timeout")
        return None


# ──────────────────────────────────────────────────────────────
# AntiCaptcha
# ──────────────────────────────────────────────────────────────

class AntiCaptchaClient:
    """anti-captcha.com API。CapSolver と同構造。"""

    BASE_URL = "https://api.anti-captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        task: dict = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if proxy:
            task["type"] = "FunCaptchaTask"
            task.update(_proxy_parts(proxy))

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.BASE_URL}/createTask",
                    json={"clientKey": self.api_key, "task": task},
                )
                data = resp.json()
                if data.get("errorId"):
                    logger.error(f"AntiCaptcha createTask: {data.get('errorDescription')}")
                    return None
                task_id = data["taskId"]
            except Exception as e:
                logger.error(f"AntiCaptcha createTask failed: {e}")
                return None

            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                try:
                    r = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    if rd.get("status") == "ready":
                        token = rd["solution"].get("token")
                        logger.debug(f"AntiCaptcha solved: {token[:30] if token else 'None'}...")
                        return token
                    if rd.get("errorId"):
                        logger.error(f"AntiCaptcha error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("AntiCaptcha timeout")
        return None


# ──────────────────────────────────────────────────────────────
# ファクトリ
# ──────────────────────────────────────────────────────────────

def get_captcha_client(config):
    """
    config.captcha から適切なクライアントを返す。
    provider の優先順: config.captcha.provider → config.captcha.provider_fallback
    両方 none / キーなし → None
    """
    def _make(provider: str, key: str):
        if not key:
            return None
        p = provider.lower()
        if p == "capsolver":
            return CapSolverClient(key)
        if p == "2captcha":
            return TwoCaptchaClient(key)
        if p in ("anticaptcha", "anti-captcha"):
            return AntiCaptchaClient(key)
        return None

    client = _make(config.provider, config.api_key)
    if client:
        return client
    # フォールバック
    fb_provider = getattr(config, "provider_fallback", "")
    fb_key = getattr(config, "api_key_fallback", "")
    return _make(fb_provider, fb_key)
