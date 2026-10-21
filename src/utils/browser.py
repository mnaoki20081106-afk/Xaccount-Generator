"""
Playwright + patchright によるブラウザ起動と指紋隔離。
アカウントごとに独立したコンテキストを持つ。
"""
from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Optional

try:
    from patchright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    _USING_PATCHRIGHT = False

from loguru import logger

_stealth_async = None
if not _USING_PATCHRIGHT:
    try:
        from playwright_stealth import stealth_async as _stealth_async
    except ImportError:
        pass

# Chrome バージョン池（TLS JA3 指紋 curl_cffi と揃える）
_CHROME_VERSIONS = [
    ("136", 22), ("135", 20), ("134", 15), ("133", 12),
    ("131", 9),  ("130", 8),  ("128", 6),  ("124", 4),
    ("122", 2),  ("120", 1),
]
_UA_VERSIONS, _UA_WEIGHTS = zip(*_CHROME_VERSIONS)

# sec-ch-ua GREASE token per major
_GREASE = {
    "136": "Not_A Brand", "135": "Not?A_Brand", "134": "Not)A;Brand",
    "133": "Not-A.Brand", "131": "Not=A?Brand", "130": "Not:A-Brand",
    "128": "Not A;Brand",  "124": "Not;A=Brand",
    "122": " Not A;Brand", "120": "Not A Brand",
}


def _pick_chrome_version() -> str:
    return random.choices(_UA_VERSIONS, weights=_UA_WEIGHTS, k=1)[0]


def _make_ua(major: str) -> str:
    return (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


def _make_sec_ch_ua(major: str) -> str:
    grease = _GREASE.get(major, "Not A;Brand")
    return f'"{grease}";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'


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
            "--disable-features=TranslateUI",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not _USING_PATCHRIGHT:
            launch_args += [
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
            ]

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
        if not self._browser:
            raise RuntimeError("Browser not started. Call start() first.")

        major = _pick_chrome_version()
        ua = _make_ua(major)
        sec_ch_ua = _make_sec_ch_ua(major)

        vp_w, vp_h = 1280, 720
        if device:
            sc = device.get("screen", {})
            vp_w = min(sc.get("availWidth", 1280), sc.get("width", 1280))
            vp_h = sc.get("availHeight", 720)

        context_options: dict = {
            "locale": locale,
            "timezone_id": timezone_id,
            "viewport": {"width": vp_w, "height": vp_h},
            "screen": {"width": vp_w, "height": vp_h},
            "user_agent": ua,
            "java_script_enabled": True,
            "ignore_https_errors": True,
            "color_scheme": random.choice(["light", "light", "dark"]),
            "extra_http_headers": {
                "sec-ch-ua": sec_ch_ua,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "accept-language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            "permissions": ["clipboard-read"],
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

        cores = device["hardwareConcurrency"] if device else random.choice([4, 6, 8, 12, 16])
        memory = device["deviceMemory"] if device else random.choice([4, 8, 8, 16])
        rtt = random.randint(20, 120)
        downlink = round(random.uniform(1.5, 10.0), 1)
        # ランダムなキャンバスノイズシード（毎コンテキストで異なる）
        canvas_noise = random.randint(1, 9999)

        await context.add_init_script(f"""
            (() => {{
                // webdriver フラグ除去
                Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});

                // chrome オブジェクト注入
                if (!window.chrome) {{
                    window.chrome = {{
                        app: {{ isInstalled: false }},
                        runtime: {{
                            onConnect: null,
                            onMessage: null,
                        }},
                    }};
                }}

                // 言語
                Object.defineProperty(navigator, 'languages', {{
                    get: () => ['ja-JP', 'ja', 'en-US', 'en']
                }});

                // プラグイン（空でないことが重要）
                Object.defineProperty(navigator, 'plugins', {{
                    get: () => {{
                        const a = [1, 2, 3, 4, 5];
                        a.item = i => a[i];
                        a.namedItem = name => null;
                        a.refresh = () => {{}};
                        return a;
                    }}
                }});
                Object.defineProperty(navigator, 'mimeTypes', {{
                    get: () => {{ const a = []; a.item = i => null; a.namedItem = n => null; return a; }}
                }});

                // ハードウェア
                Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {cores} }});
                Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {memory} }});

                // ネットワーク
                const _conn = {{
                    effectiveType: '4g',
                    rtt: {rtt},
                    downlink: {downlink},
                    saveData: false,
                    onchange: null,
                    addEventListener: () => {{}},
                    removeEventListener: () => {{}},
                    dispatchEvent: () => true,
                }};
                Object.defineProperty(navigator, 'connection', {{ get: () => _conn }});

                // Permissions API — 通知は拒否済みとしてみせる（自動化検知回避）
                const _origQuery = window.Permissions && window.Permissions.prototype.query;
                if (_origQuery) {{
                    window.Permissions.prototype.query = function(parameters) {{
                        return Promise.resolve({{ state: 'denied', name: parameters.name }});
                    }};
                }}

                // WebRTC leak 防止 — ローカル IP を晒さない
                if (window.RTCPeerConnection) {{
                    const _origRTC = window.RTCPeerConnection;
                    window.RTCPeerConnection = function(config) {{
                        if (config && config.iceServers) config.iceServers = [];
                        return new _origRTC(config);
                    }};
                    Object.setPrototypeOf(window.RTCPeerConnection, _origRTC);
                }}

                // Canvas 指紋ノイズ（微細ピクセル変動）
                const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
                HTMLCanvasElement.prototype.toDataURL = function(type) {{
                    const ctx = this.getContext('2d');
                    if (ctx) {{
                        const seed = {canvas_noise};
                        const pixel = ctx.getImageData(0, 0, 1, 1);
                        pixel.data[0] = (pixel.data[0] + (seed % 3)) & 0xFF;
                        ctx.putImageData(pixel, 0, 0);
                    }}
                    return _toDataURL.apply(this, arguments);
                }};
                const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
                CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {{
                    const data = _getImageData.apply(this, arguments);
                    const seed = {canvas_noise};
                    for (let i = 0; i < data.data.length; i += 100) {{
                        data.data[i] = (data.data[i] + (seed & 1)) & 0xFF;
                    }}
                    return data;
                }};

                // AudioContext 指紋ノイズ
                if (window.AudioContext || window.webkitAudioContext) {{
                    const AC = window.AudioContext || window.webkitAudioContext;
                    const _origCreateOscillator = AC.prototype.createOscillator;
                    AC.prototype.createOscillator = function() {{
                        const osc = _origCreateOscillator.apply(this, arguments);
                        const _origStart = osc.start.bind(osc);
                        osc.start = function(when) {{
                            return _origStart(when || 0);
                        }};
                        return osc;
                    }};
                }}

                // screen 系
                try {{
                    Object.defineProperty(window, 'outerWidth', {{ get: () => {vp_w} }});
                    Object.defineProperty(window, 'outerHeight', {{ get: () => {vp_h} }});
                    Object.defineProperty(window, 'innerWidth', {{ get: () => {vp_w} }});
                    Object.defineProperty(window, 'innerHeight', {{ get: () => {vp_h} }});
                }} catch(e) {{}}

                // 自動化チェック関数を潰す
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
            }})();
        """, timeout=0)

        return context

    async def new_page(self, context: BrowserContext, block_resources: bool = True) -> Page:
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
            "ads-twitter.com",
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

    async def extract_auth_tokens(self, context: BrowserContext) -> tuple[str | None, str | None]:
        """X の auth_token と ct0 をクッキーから抽出する。"""
        cookies = await context.cookies("https://x.com")
        auth_token = None
        ct0 = None
        for ck in cookies:
            if ck.get("name") == "auth_token":
                auth_token = ck.get("value")
            elif ck.get("name") == "ct0":
                ct0 = ck.get("value")
        return auth_token, ct0
