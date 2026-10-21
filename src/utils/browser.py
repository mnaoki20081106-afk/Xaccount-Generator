"""
Playwright + patchright によるブラウザ起動と指紋隔離。
アカウントごとに独立したコンテキストを持つ。
"""
from pathlib import Path
from typing import Optional
import hashlib
import json
import random

# patchright は Runtime.enable leak を修正した drop-in replacement。
# 未インストールなら playwright にフォールバック。
try:
    from patchright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = False

from loguru import logger

# patchright は playwright-stealth を不要にするので、そちらは試みない。
# patchright なしの場合のみ stealth を適用する。
_stealth_async = None
if not _USING_PATCHRIGHT:
    try:
        from playwright_stealth import stealth_async as _stealth_async
    except ImportError:
        pass

# Chrome バージョン池（実使用分布に合わせた重み付き）
_CHROME_VERSIONS = [
    ("136", 18), ("135", 20), ("134", 16), ("133", 14),
    ("131", 10), ("130", 9), ("128", 6), ("124", 4),
    ("122", 2), ("120", 1),
]
_UA_VERSIONS, _UA_WEIGHTS = zip(*_CHROME_VERSIONS)


def _pick_chrome_ua() -> str:
    v = random.choices(_UA_VERSIONS, weights=_UA_WEIGHTS, k=1)[0]
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{v}.0.0.0 Safari/537.36"
    )


class BrowserManager:
    def __init__(self, headless: bool = True, slow_mo: int = 50, timeout: int = 30000):
        self.headless = headless
        self.slow_mo = slow_mo
        self.timeout = timeout
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self):
        self._playwright = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-infobars",
            "--window-size=1280,720",
        ]
        # patchright はこれらを内部で処理するので手動追加しない。
        # playwright fallback 時のみ明示的に追加。
        if not _USING_PATCHRIGHT:
            launch_args.append("--disable-blink-features=AutomationControlled")

        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=launch_args,
        )
        mode = "patchright" if _USING_PATCHRIGHT else "playwright"
        logger.info(f"Browser launched ({mode})")

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
        device: Optional[dict] = None,
    ) -> BrowserContext:
        """
        アカウント専用の隔離コンテキストを作成。
        device: arkose_solver.devices.generate_device() の戻り値。
                渡すと hardwareConcurrency / deviceMemory / screen を一致させる。
        """
        if not self._browser:
            raise RuntimeError("Browser not started. Call start() first.")

        ua = _pick_chrome_ua()

        # device データからビューポートを決定
        vp_w, vp_h = 1280, 720
        if device:
            sc = device.get("screen", {})
            vp_w = min(sc.get("availWidth", 1280), sc.get("width", 1280))
            vp_h = sc.get("availHeight", 720)

        context_options: dict = {
            "locale": locale,
            "timezone_id": timezone_id,
            "viewport": {"width": vp_w, "height": vp_h},
            "user_agent": ua,
            "java_script_enabled": True,
            "ignore_https_errors": True,
        }

        if proxy_url:
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

        # 指紋注入スクリプト
        cores = device["hardwareConcurrency"] if device else random.choice([4, 8, 12, 16])
        memory = device["deviceMemory"] if device else random.choice([4, 8, 16])
        await context.add_init_script(f"""
            (() => {{
                // webdriver フラグ除去（patchright でも念のため）
                Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});
                // chrome オブジェクト
                if (!window.chrome) window.chrome = {{ runtime: {{}} }};
                // 言語
                Object.defineProperty(navigator, 'languages', {{
                    get: () => ['ja-JP', 'ja', 'en-US', 'en']
                }});
                // プラグイン（空でないことが重要）
                Object.defineProperty(navigator, 'plugins', {{
                    get: () => {{ const a = [1,2,3,4,5]; a.item = i => a[i]; return a; }}
                }});
                // デバイス系: devices pool から一致させる
                Object.defineProperty(navigator, 'hardwareConcurrency', {{
                    get: () => {cores}
                }});
                Object.defineProperty(navigator, 'deviceMemory', {{
                    get: () => {memory}
                }});
            }})();
        """)

        return context

    async def new_page(self, context: BrowserContext, block_resources: bool = True) -> Page:
        """
        block_resources=True でサインアップ時の帯域を最小化。
        """
        page = await context.new_page()
        if _stealth_async:
            await _stealth_async(page)
        if block_resources:
            await self._setup_resource_blocking(page)
        return page

    async def _setup_resource_blocking(self, page: Page):
        BLOCKED_EXTENSIONS = (
            ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".mp4", ".webm", ".ogg", ".mp3",
        )
        BLOCKED_HOSTS = (
            "google-analytics.com", "googletagmanager.com",
            "doubleclick.net", "googlesyndication.com",
            "amplitude.com", "segment.io", "segment.com",
            "hotjar.com", "clarity.ms",
            "ads-twitter.com", "t.co/i/",
        )
        ALLOWED_TYPES = {"document", "script", "xhr", "fetch", "stylesheet", "websocket"}

        async def handle_route(route):
            req = route.request
            rt = req.resource_type
            url = req.url.lower()

            if rt in ALLOWED_TYPES:
                if any(h in url for h in BLOCKED_HOSTS):
                    await route.abort()
                    return
                await route.continue_()
                return

            if any(url.split("?")[0].endswith(ext) for ext in BLOCKED_EXTENSIONS):
                await route.abort()
                return

            if rt in ("image", "media", "other"):
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
