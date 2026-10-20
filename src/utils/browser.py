"""
Playwright + stealth によるブラウザ起動と指紋隔離。
アカウントごとに独立したコンテキストを持つ。
"""
from pathlib import Path
from typing import Optional
import hashlib
import json

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from loguru import logger

# playwright-stealth は環境によってインポート方法が違う場合がある
try:
    from playwright_stealth import stealth_async
except ImportError:
    stealth_async = None
    logger.warning("playwright-stealth が見つかりません。基本的な起動のみ行います。")


class BrowserManager:
    def __init__(self, headless: bool = True, slow_mo: int = 50, timeout: int = 30000):
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,720",
            ],
        )
        logger.info("Browser launched")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def new_context(
        self,
        proxy_url: Optional[str] = None,
        user_data_dir: Optional[str] = None,
        locale: str = "ja-JP",
        timezone_id: str = "Asia/Tokyo",
    ) -> BrowserContext:
        """
        アカウント専用の隔離コンテキストを作成。
        proxy_url 例: http://user:pass@host:port
        """
        if not self._browser:
            raise RuntimeError("Browser not started. Call start() first.")

        context_options = {
            "locale": locale,
            "timezone_id": timezone_id,
            "viewport": {"width": 1280, "height": 720},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "java_script_enabled": True,
            "ignore_https_errors": True,
        }

        if proxy_url:
            # Playwright の proxy 形式に変換
            # http://user:pass@host:port → server + username + password
            from urllib.parse import urlparse
            parsed = urlparse(proxy_url)
            context_options["proxy"] = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            }
            if parsed.username:
                context_options["proxy"]["username"] = parsed.username
            if parsed.password:
                context_options["proxy"]["password"] = parsed.password

        context = await self._browser.new_context(**context_options)
        context.set_default_timeout(self.timeout)

        # 追加の指紋対策スクリプト
        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['ja-JP', 'ja', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            """
        )

        return context

    async def new_page(self, context: BrowserContext) -> Page:
        page = await context.new_page()
        if stealth_async:
            await stealth_async(page)
        return page

    @staticmethod
    def fingerprint_hash(username: str, extra: str = "") -> str:
        raw = f"{username}:{extra}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    async def save_cookies(self, context: BrowserContext, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        cookies = await context.cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.debug(f"Cookies saved: {path}")

    async def load_cookies(self, context: BrowserContext, path: str):
        p = Path(path)
        if not p.exists():
            return
        with open(p, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        await context.add_cookies(cookies)
        logger.debug(f"Cookies loaded: {path}")
