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

    async def new_page(self, context: BrowserContext, block_resources: bool = True) -> Page:
        """
        block_resources=True でサインアップ時の帯域を最小化。
        画像・フォント・動画・広告トラッキングをすべて遮断。
        1垢あたりの通信量が ~1MB → プロキシコスト $0.001/垢 以下。
        """
        page = await context.new_page()
        if stealth_async:
            await stealth_async(page)
        if block_resources:
            await self._setup_resource_blocking(page)
        return page

    async def _setup_resource_blocking(self, page: Page):
        """不要リソースを遮断して帯域節約。"""
        # 遮断する拡張子
        BLOCKED_EXTENSIONS = (
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
            ".woff", ".woff2", ".ttf", ".otf", ".eot",
            ".mp4", ".webm", ".ogg", ".mp3",
        )
        # 遮断するホスト (広告・解析)
        BLOCKED_HOSTS = (
            "google-analytics.com", "googletagmanager.com",
            "doubleclick.net", "googlesyndication.com",
            "amplitude.com", "segment.io", "segment.com",
            "hotjar.com", "clarity.ms",
            "ads-twitter.com", "t.co/i/",
        )
        # 通す必要があるリソースタイプ
        ALLOWED_TYPES = {"document", "script", "xhr", "fetch", "stylesheet", "websocket"}

        async def handle_route(route):
            req = route.request
            rt = req.resource_type
            url = req.url.lower()

            # 必須リソースは通す
            if rt in ALLOWED_TYPES:
                # ただし広告ホストはスクリプトでも遮断
                if any(h in url for h in BLOCKED_HOSTS):
                    await route.abort()
                    return
                await route.continue_()
                return

            # 拡張子チェック
            if any(url.split("?")[0].endswith(ext) for ext in BLOCKED_EXTENSIONS):
                await route.abort()
                return

            # image / media / font タイプは遮断
            if rt in ("image", "media", "font", "other"):
                await route.abort()
                return

            await route.continue_()

        await page.route("**/*", handle_route)

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
