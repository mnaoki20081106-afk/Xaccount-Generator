"""
キャプチャソルバー統合。
CapSolver / 2Captcha / AntiCaptcha / Cap-Bypass を共通 IF で扱う。
CaptchaManager がプロバイダー間の自動フォールバックと残高チェックを担当。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx
from loguru import logger


def _proxy_parts(proxy: str | None) -> dict:
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
# OSSArkoseSolver — ローカル無料 Arkose solver
# ──────────────────────────────────────────────────────────────

class OSSArkoseSolver:
    """
    src/arkose_solver/ のローカル実装で FunCaptcha を無料解決。
    CaptchaManager の先頭プロバイダーとして登録し、失敗時のみ有料へフォールバック。

    suppressed=True (sup=1) のトークンは X が拒否しやすいため None を返し
    次のプロバイダーへ渡す。solve() は同期なので ThreadPoolExecutor で実行。
    """
    name = "OSSArkose"
    _SURL = "https://client-api.arkoselabs.com"

    def __init__(self, user_agent: str | None = None):
        self.user_agent = user_agent

    async def get_balance(self) -> float:
        return float("inf")  # 無料なので残高概念なし

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        blob: str = "",
        **_,
    ) -> str | None:
        try:
            from ..arkose_solver import Solver
        except ImportError:
            logger.warning("OSSArkose: arkose_solver モジュールが見つかりません")
            return None

        import asyncio

        def _run() -> str | None:
            solver = Solver(
                surl=self._SURL,
                public_key=website_public_key,
                site=website_url,
                proxy=proxy,
                user_agent=self.user_agent,
                language="ja",
            )
            if blob:
                solver.data_blob = blob
            result = solver.solve()
            if result.suppressed:
                logger.warning("OSSArkose: suppressed トークン (sup=1) — 有料へフォールバック")
                return None
            logger.info(f"OSSArkose: 解決成功 timings={result.timings}")
            return result.token

        try:
            loop = asyncio.get_event_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, _run),
                timeout=float(timeout),
            )
        except asyncio.TimeoutError:
            logger.warning("OSSArkose: タイムアウト")
            return None
        except Exception as e:
            logger.warning(f"OSSArkose: 解決失敗: {e}")
            return None


# ──────────────────────────────────────────────────────────────
# CapSolver
# ──────────────────────────────────────────────────────────────

class CapSolverClient:
    BASE_URL = "https://api.capsolver.com"
    name = "CapSolver"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_balance(self) -> float:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.post(
                    f"{self.BASE_URL}/getBalance",
                    json={"clientKey": self.api_key},
                )
                data = r.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
            except Exception as e:
                logger.debug(f"CapSolver getBalance: {e}")
        return 0.0

    async def _run_task(
        self, task: dict, timeout: int = 120, poll_interval: int = 4
    ) -> dict | None:
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

            for _ in range(timeout // poll_interval):
                await asyncio.sleep(poll_interval)
                try:
                    r = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    if rd.get("status") == "ready":
                        return rd.get("solution", {})
                    if rd.get("errorId"):
                        logger.error(f"CapSolver task error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("CapSolver timeout")
        return None

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        blob: str = "",
    ) -> str | None:
        task: dict = {
            "type": "FunCaptchaTaskProxyLess",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if blob:
            task["funcaptchaApiJSSubdomain"] = "https://client-api.arkoselabs.com"
            task["data"] = f'{{"blob": "{blob}"}}'
        if proxy:
            task["type"] = "FunCaptchaTask"
            task.update(_proxy_parts(proxy))

        sol = await self._run_task(task, timeout=timeout)
        if sol:
            token = sol.get("token")
            if token:
                logger.debug(f"CapSolver funcaptcha solved: {token[:40]}...")
                return token
        return None

    async def solve_recaptcha_v2(
        self,
        website_url: str,
        website_key: str,
        proxy: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        task: dict = {
            "type": "ReCaptchaV2TaskProxyLess",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        sol = await self._run_task(task, timeout=timeout)
        if sol:
            return sol.get("gRecaptchaResponse")
        return None

    async def solve_image_to_text(self, base64_image: str) -> str | None:
        task = {"type": "ImageToTextTask", "body": base64_image}
        sol = await self._run_task(task, timeout=60)
        if sol:
            return sol.get("text")
        return None


# ──────────────────────────────────────────────────────────────
# 2Captcha
# ──────────────────────────────────────────────────────────────

class TwoCaptchaClient:
    BASE_URL = "https://api.2captcha.com"
    name = "2Captcha"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_balance(self) -> float:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.get(
                    f"{self.BASE_URL}/getBalance",
                    params={"key": self.api_key, "action": "getbalance", "json": 1},
                )
                data = r.json()
                if data.get("status") == 1:
                    return float(data.get("request", 0))
            except Exception as e:
                logger.debug(f"2Captcha getBalance: {e}")
        return 0.0

    async def _run_task(self, task: dict, timeout: int = 120) -> dict | None:
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
                        return rd.get("solution", {})
                    if rd.get("errorId"):
                        logger.error(f"2Captcha error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("2Captcha timeout")
        return None

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        blob: str = "",
    ) -> str | None:
        task: dict = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if blob:
            task["data"] = f'{{"blob": "{blob}"}}'
        if proxy:
            task["type"] = "FunCaptchaTask"
            parts = _proxy_parts(proxy)
            task.update({
                "proxyType": parts.get("proxyType", "http"),
                "proxyAddress": parts.get("proxyAddress", ""),
                "proxyPort": parts.get("proxyPort", 80),
            })
            if "proxyLogin" in parts:
                task["proxyLogin"] = parts["proxyLogin"]
                task["proxyPassword"] = parts.get("proxyPassword", "")

        sol = await self._run_task(task, timeout=timeout)
        if sol:
            token = sol.get("token")
            if token:
                logger.debug(f"2Captcha funcaptcha solved: {token[:40]}...")
                return token
        return None

    async def solve_recaptcha_v2(
        self,
        website_url: str,
        website_key: str,
        proxy: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        task: dict = {
            "type": "ReCaptchaV2TaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        sol = await self._run_task(task, timeout=timeout)
        if sol:
            return sol.get("gRecaptchaResponse")
        return None

    async def solve_image_to_text(self, base64_image: str) -> str | None:
        task = {"type": "ImageToTextTask", "body": base64_image}
        sol = await self._run_task(task, timeout=60)
        if sol:
            return sol.get("text")
        return None


# ──────────────────────────────────────────────────────────────
# AntiCaptcha
# ──────────────────────────────────────────────────────────────

class AntiCaptchaClient:
    BASE_URL = "https://api.anti-captcha.com"
    name = "AntiCaptcha"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_balance(self) -> float:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.post(
                    f"{self.BASE_URL}/getBalance",
                    json={"clientKey": self.api_key},
                )
                data = r.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
            except Exception as e:
                logger.debug(f"AntiCaptcha getBalance: {e}")
        return 0.0

    async def _run_task(self, task: dict, timeout: int = 120) -> dict | None:
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
                        return rd.get("solution", {})
                    if rd.get("errorId"):
                        logger.error(f"AntiCaptcha error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("AntiCaptcha timeout")
        return None

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        blob: str = "",
    ) -> str | None:
        task: dict = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if proxy:
            task["type"] = "FunCaptchaTask"
            task.update(_proxy_parts(proxy))

        sol = await self._run_task(task, timeout=timeout)
        if sol:
            token = sol.get("token")
            if token:
                logger.debug(f"AntiCaptcha funcaptcha solved: {token[:40]}...")
                return token
        return None

    async def solve_recaptcha_v2(
        self,
        website_url: str,
        website_key: str,
        proxy: str | None = None,
        timeout: int = 120,
    ) -> str | None:
        task: dict = {
            "type": "NoCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websiteKey": website_key,
        }
        sol = await self._run_task(task, timeout=timeout)
        if sol:
            return sol.get("gRecaptchaResponse")
        return None

    async def solve_image_to_text(self, base64_image: str) -> str | None:
        task = {"type": "ImageToTextTask", "body": base64_image}
        sol = await self._run_task(task, timeout=60)
        if sol:
            return sol.get("text")
        return None


# ──────────────────────────────────────────────────────────────
# Cap-Bypass (capbypass.com)
# ──────────────────────────────────────────────────────────────

class CapBypassClient:
    """
    capbypass.com — Outlook の FunCaptcha / Arkose Labs に強い。
    API 構造は CapSolver と互換。
    """
    BASE_URL = "https://capbypass.com/api"
    name = "CapBypass"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def get_balance(self) -> float:
        async with httpx.AsyncClient(timeout=15) as c:
            try:
                r = await c.post(
                    f"{self.BASE_URL}/getBalance",
                    json={"clientKey": self.api_key},
                )
                data = r.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
            except Exception as e:
                logger.debug(f"CapBypass getBalance: {e}")
        return 0.0

    async def _run_task(self, task: dict, timeout: int = 120) -> dict | None:
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                resp = await client.post(
                    f"{self.BASE_URL}/createTask",
                    json={"clientKey": self.api_key, "task": task},
                )
                data = resp.json()
                if data.get("errorId"):
                    logger.error(f"CapBypass createTask: {data.get('errorDescription')}")
                    return None
                task_id = data["taskId"]
            except Exception as e:
                logger.error(f"CapBypass createTask failed: {e}")
                return None

            for _ in range(timeout // 4):
                await asyncio.sleep(4)
                try:
                    r = await client.post(
                        f"{self.BASE_URL}/getTaskResult",
                        json={"clientKey": self.api_key, "taskId": task_id},
                    )
                    rd = r.json()
                    if rd.get("status") == "ready":
                        return rd.get("solution", {})
                    if rd.get("errorId"):
                        logger.error(f"CapBypass task error: {rd.get('errorDescription')}")
                        return None
                except Exception:
                    continue
        logger.warning("CapBypass timeout")
        return None

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        blob: str = "",
    ) -> str | None:
        task: dict = {
            "type": "FunCaptchaTaskProxyLess",
            "websiteURL": website_url,
            "websitePublicKey": website_public_key,
        }
        if blob:
            task["data"] = f'{{"blob": "{blob}"}}'
        if proxy:
            task["type"] = "FunCaptchaTask"
            task.update(_proxy_parts(proxy))

        sol = await self._run_task(task, timeout=timeout)
        if sol:
            token = sol.get("token")
            if token:
                logger.debug(f"CapBypass funcaptcha solved: {token[:40]}...")
                return token
        return None

    async def solve_image_to_text(self, base64_image: str) -> str | None:
        task = {"type": "ImageToTextTask", "body": base64_image}
        sol = await self._run_task(task, timeout=60)
        if sol:
            return sol.get("text")
        return None


# ──────────────────────────────────────────────────────────────
# CaptchaManager — 自動フォールバック付きファサード
# ──────────────────────────────────────────────────────────────

_AnyClient = OSSArkoseSolver | CapSolverClient | TwoCaptchaClient | AntiCaptchaClient | CapBypassClient


@dataclass
class _ProviderEntry:
    client: _AnyClient
    consecutive_failures: int = field(default=0)
    disabled: bool = field(default=False)

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self):
        self.consecutive_failures += 1
        if self.consecutive_failures >= 3:
            self.disabled = True
            logger.warning(f"{self.client.name} disabled after 3 consecutive failures")


class CaptchaManager:
    """
    複数プロバイダーを順番に試す。
    - 残高不足 → 次へ
    - 解決失敗 3 連続 → そのプロバイダーを無効化
    - 全滅 → None
    """

    def __init__(self, providers: list[_AnyClient]):
        self._entries = [_ProviderEntry(p) for p in providers]

    @property
    def has_provider(self) -> bool:
        return any(not e.disabled for e in self._entries)

    def _active_entries(self):
        return [e for e in self._entries if not e.disabled]

    async def solve_funcaptcha(
        self,
        website_url: str,
        website_public_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        blob: str = "",
        min_balance: float = 0.02,
    ) -> str | None:
        for entry in self._active_entries():
            try:
                balance = await entry.client.get_balance()
                if balance < min_balance:
                    logger.warning(
                        f"{entry.client.name} balance ${balance:.3f} < ${min_balance}, skip"
                    )
                    continue

                token = await entry.client.solve_funcaptcha(
                    website_url, website_public_key,
                    proxy=proxy, timeout=timeout, blob=blob,
                )
                if token:
                    entry.record_success()
                    return token

                logger.warning(f"{entry.client.name} returned no token")
                entry.record_failure()

            except Exception as e:
                logger.error(f"{entry.client.name} exception: {e}")
                entry.record_failure()

        logger.error("All captcha providers failed for funcaptcha")
        return None

    async def solve_recaptcha_v2(
        self,
        website_url: str,
        website_key: str,
        proxy: str | None = None,
        timeout: int = 120,
        min_balance: float = 0.02,
    ) -> str | None:
        for entry in self._active_entries():
            if not hasattr(entry.client, "solve_recaptcha_v2"):
                continue
            try:
                balance = await entry.client.get_balance()
                if balance < min_balance:
                    continue

                token = await entry.client.solve_recaptcha_v2(
                    website_url, website_key, proxy=proxy, timeout=timeout
                )
                if token:
                    entry.record_success()
                    return token
                entry.record_failure()

            except Exception as e:
                logger.error(f"{entry.client.name} recaptcha exception: {e}")
                entry.record_failure()

        logger.error("All captcha providers failed for reCAPTCHA v2")
        return None

    async def solve_image_to_text(
        self, base64_image: str, min_balance: float = 0.005
    ) -> str | None:
        for entry in self._active_entries():
            if not hasattr(entry.client, "solve_image_to_text"):
                continue
            try:
                balance = await entry.client.get_balance()
                if balance < min_balance:
                    continue
                text = await entry.client.solve_image_to_text(base64_image)
                if text:
                    entry.record_success()
                    return text
                entry.record_failure()
            except Exception as e:
                logger.error(f"{entry.client.name} image exception: {e}")
                entry.record_failure()

        return None

    async def log_balances(self):
        for entry in self._entries:
            try:
                bal = await entry.client.get_balance()
                status = "DISABLED" if entry.disabled else f"${bal:.3f}"
                logger.info(f"  {entry.client.name}: {status}")
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────
# ファクトリ
# ──────────────────────────────────────────────────────────────

def _make_client(provider: str, key: str) -> _AnyClient | None:
    if not key:
        return None
    p = provider.lower()
    if p == "capsolver":
        return CapSolverClient(key)
    if p == "2captcha":
        return TwoCaptchaClient(key)
    if p in ("anticaptcha", "anti-captcha"):
        return AntiCaptchaClient(key)
    if p in ("capbypass", "cap-bypass", "cap_bypass"):
        return CapBypassClient(key)
    return None


def get_captcha_client(config) -> _AnyClient | None:
    """後方互換: 単一クライアントを返す。新コードは get_captcha_manager を使うこと。"""
    c = _make_client(config.provider, config.api_key)
    if c:
        return c
    return _make_client(
        getattr(config, "provider_fallback", ""),
        getattr(config, "api_key_fallback", ""),
    )


def get_captcha_manager(config) -> CaptchaManager | None:
    """
    設定から CaptchaManager を構築。
    OSS (無料) → primary → fallback の順でプロバイダーを登録。
    OSSのみでも動作 (paid キーなし = None にならない)。
    """
    clients: list[_AnyClient] = []

    # OSSソルバーを先頭に (無料、curl_cffi が必要)
    try:
        from ..arkose_solver import Solver as _  # availability check
        clients.append(OSSArkoseSolver())
        logger.debug("CaptchaManager: OSSArkose を先頭プロバイダーとして登録 (無料)")
    except ImportError:
        logger.debug("CaptchaManager: arkose_solver 未インストール、OSS をスキップ")

    primary = _make_client(config.provider, config.api_key)
    if primary:
        clients.append(primary)

    fallback = _make_client(
        getattr(config, "provider_fallback", ""),
        getattr(config, "api_key_fallback", ""),
    )
    if fallback:
        clients.append(fallback)

    if not clients:
        return None
    return CaptchaManager(clients)
