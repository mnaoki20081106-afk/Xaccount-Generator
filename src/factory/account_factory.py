"""
アカウント生成 — ステートマシン版。
直列ステップではなく _detect_step() で現在地を判定しながら進む。
X の UI 変更でステップ順が変わっても対応できる。
"""
from __future__ import annotations

import asyncio
import re
import secrets
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs
import random

from loguru import logger

from ..models import Account, AccountStatus
from ..proxy.manager import ProxyManager
from ..auth.twofa import TwoFAManager
from ..config import AppConfig
from ..utils.browser import BrowserManager
from ..utils.profile import setup_profile
from ..email_provider.mailtm import MailTmClient
from .api_flow import XApiFlow, HAS_CURL

if HAS_CURL:
    from curl_cffi import requests as curl_requests

# X サインアップで出る FunCaptcha の public key（動的抽出できなかった場合のフォールバック）
# api_flow.py の X_ARKOSE_KEY と一致させること
X_FUNCAPTCHA_KEY_FALLBACK = "2CB16598-CB82-4CF7-B332-5990DB66F3AB"

# ステップ検出用セレクタ
STEP_PROBES: dict[str, str] = {
    "name":     'input[name="name"], input[autocomplete="name"]',
    "phone":    'input[name="phone_number"], input[type="tel"]',
    "email":    'input[name="email"], input[type="email"]',
    "birthday": (
        '[data-testid="birthdateMonth"], [data-testid="birthdateDay"], '
        'select#SELECTOR_1, select[name="month"]'
    ),
    "captcha":  'iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]',
    "code": (
        'input[inputmode="numeric"], '
        'input[name="verification_code"], input[name="verfication_code"], '
        'input[placeholder*="コード"], input[placeholder*="code" i]'
    ),
    "password": 'input[name="password"], input[type="password"], input[autocomplete="new-password"]',
    "username": 'input[name="username"]',
    "done":     'a[data-testid="AppTabBar_Home_Link"], [data-testid="primaryColumn"]',
}

NEXT_SELECTORS = [
    '[data-testid="ocfEnterTextNextButton"]',
    'div[role="button"]:has-text("次へ")',
    'div[role="button"]:has-text("Next")',
    'div[role="button"]:has-text("Sign up")',
    'div[role="button"]:has-text("登録")',
]

ERROR_SELECTOR = '[data-testid="ocfEnterTextError"], [role="alert"]'


def _gen_password(n: int = 14) -> str:
    """X 要件準拠: 大小英数 + 記号を各04文字以上含む。"""
    symbols = "!@#$%^&*"
    pool = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(n))
        if (any(c.isupper() for c in pw)
                and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in symbols for c in pw)):
            return pw


def _gen_username() -> str:
    base = "".join(secrets.choice(string.ascii_lowercase) for _ in range(6))
    return f"{base}{secrets.randbelow(10000):04d}"


def _gen_display_name() -> str:
    from faker import Faker
    return Faker("ja_JP").name()


def _gen_birthdate() -> tuple[int, int, int]:
    """(year, month, day)"""
    return (random.randint(1988, 2002), random.randint(1, 12), random.randint(1, 28))


async def _human(lo: float, hi: float):
    await asyncio.sleep(random.uniform(lo, hi))


class AccountFactory:
    def __init__(
        self,
        config: AppConfig,
        proxy_manager: ProxyManager,
        twofa_manager: TwoFAManager,
        browser_manager: BrowserManager,
    ):
        self.config = config
        self.proxy_manager = proxy_manager
        self.twofa_manager = twofa_manager
        self.browser = browser_manager
        self._captcha_client = None
        self._sms_mgr = None
        self._init_clients()

    def _init_clients(self):
        from ..utils.captcha import get_captcha_client
        self._captcha_client = get_captcha_client(self.config.captcha)
        if self.config.factory.use_phone:
            from ..sms.manager import SmsManager
            self._sms_mgr = SmsManager(self.config.sms)

    # ────────────────────────────────────────────────
    # API フロー (Playwright 不要)
    # ────────────────────────────────────────────────

    async def _create_via_api_flow(
        self,
        email: str,
        display_name: str,
        mail_client,
        proxy,
    ) -> "Account | None":
        """curl_cffi + onboarding/task.json で完結するサインアップ。"""
        cds_key = getattr(self.config.captcha, "cds_api_key", None)
        flow = XApiFlow(email=email, proxy_url=proxy.url, cds_api_key=cds_key)
        flow.full_name = display_name

        async def _fetch_code() -> str | None:
            if mail_client is None:
                return None
            for _ in range(24):
                await asyncio.sleep(5)
                code = await mail_client.get_latest_code()
                if code:
                    return code
            return None

        ok = await flow.async_run(_fetch_code)
        if not ok or not flow.auth_token:
            return None

        twofa_secret = None
        if self.config.twofa.enabled:
            raw = self.twofa_manager.generate_secret()
            twofa_secret = self.twofa_manager.encrypt(raw)

        account = Account(
            username=flow.username or f"api_{int(time.time())}",
            password=flow.password,
            email=email,
            phone=None,
            twofa_secret=twofa_secret,
            auth_token=flow.auth_token,
            ct0=flow.ct0,
            status=AccountStatus.CREATED,
            proxy_id=proxy.session_id,
            fingerprint_hash=BrowserManager.fingerprint_hash(flow.username or email),
            cookies_path=None,
            created_at=datetime.utcnow(),
        )
        logger.success(
            f"[API] @{account.username} created auth_token={flow.auth_token[:8]}..."
        )
        return account

    # ────────────────────────────────────────────────
    # 公開 API
    # ────────────────────────────────────────────────

    async def create_one(self) -> Account | None:
        username = _gen_username()
        password = _gen_password()
        display_name = _gen_display_name()
        email, mail_client = await self._get_email(username)

        if not email:
            logger.error(f"[{username}] email取得失敗")
            return None

        proxy = self.proxy_manager.get_proxy_for_account(username)
        if not await self.proxy_manager.health_check(proxy):
            logger.warning(f"[{username}] proxy unhealthy, skip")
            return None

        # ── API フロー (Playwright 不要、高速) ────────────────────────────────
        if HAS_CURL and not self.config.factory.use_phone:
            api_account = await self._create_via_api_flow(
                email, display_name, mail_client, proxy
            )
            if api_account:
                if mail_client:
                    await mail_client.close()
                return api_account
            logger.warning(f"[{username}] API フロー失敗 — Playwright にフォールバック")
        # ────────────────────────────────────────────────────────────────

        # SMS は電話ステップ到達時に取得（無駄打ち防止）。
        # sms_state はこの呼び出し専用のローカル dict —
        # インスタンス属性にすると concurrency>1 の並行生成で他アカウントの
        # SMS 情報を上書きする競合バグになるため、必ず呼び出し単位で渡す。
        sms_state: dict = {"activation_id": None, "client": None, "phone": None}

        context = None
        try:
            # デバイスフィンガープリントをコンテキストに注入して一貫性を保つ
            try:
                from ..arkose_solver.devices import generate_device
                device = generate_device()
            except Exception:
                device = None
            context = await self.browser.new_context(proxy_url=proxy.url, device=device)
            page = await self.browser.new_page(context, block_resources=True)

            # dialog (通知許可など) を自動 dismiss
            page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

            final_username, success = await self._run_signup_flow(
                page, username, password, email, display_name,
                mail_client=mail_client,
                sms_state=sms_state,
                proxy_url=proxy.url,
            )

            if not success:
                await self._release_sms(sms_state)
                return None

            username = final_username  # 衝突リトライで変更された場合に反映
            phone_number = sms_state.get("phone")

            cookie_path = str(Path("data/cookies") / f"{username}.json")
            await self.browser.save_cookies(context, cookie_path)
            auth_token, ct0 = await self._extract_auth_tokens(context)

            if not await self._verify_alive(auth_token, ct0, proxy.url):
                logger.error(f"[{username}] auth_token はあるが即時凍結/無効 — 失敗扱い")
                await self._release_sms(sms_state)
                return None

            if self.config.factory.setup_profile:
                profile_page = await self.browser.new_page(context, block_resources=False)
                await setup_profile(profile_page, username, display_name)
                await profile_page.close()

            twofa_secret = None
            if self.config.twofa.enabled:
                raw = self.twofa_manager.generate_secret()
                twofa_secret = self.twofa_manager.encrypt(raw)

            account = Account(
                username=username,
                password=password,
                email=email,
                phone=phone_number,
                twofa_secret=twofa_secret,
                auth_token=auth_token,
                ct0=ct0,
                status=AccountStatus.CREATED,
                proxy_id=proxy.session_id,
                fingerprint_hash=BrowserManager.fingerprint_hash(username),
                cookies_path=cookie_path,
                created_at=datetime.utcnow(),
            )
            logger.success(
                f"[{username}] created phone={'yes' if phone_number else 'no'} "
                f"auth_token={'yes' if auth_token else 'no'}"
            )
            return account

        except Exception as e:
            logger.exception(f"[{username}] create failed: {e}")
            await self._release_sms(sms_state)
            return None
        finally:
            if context:
                await context.close()
            if mail_client:
                await mail_client.close()

    # ────────────────────────────────────────────
    # ステートマシン本体
    # ────────────────────────────────────────────

    async def _run_signup_flow(
        self,
        page,
        username: str,
        password: str,
        email: str,
        display_name: str,
        mail_client=None,
        sms_state: dict | None = None,
        proxy_url: str | None = None,
    ) -> tuple[str, bool]:
        """
        returns (final_username, success)
        username は衝突リトライで変わることがある
        """
        if sms_state is None:
            sms_state = {"activation_id": None, "client": None, "phone": None}
        try:
            await page.goto(
                "https://x.com/i/flow/signup",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            await _human(1.5, 3.0)

            # 「アカウントを作成」ボタン (存在すればクリック)
            try:
                btn = page.locator(
                    'div[role="button"]:has-text("アカウントを作成"), '
                    'div[role="button"]:has-text("Create account")'
                ).first
                if await btn.count() and await btn.is_visible():
                    await btn.click()
                    await _human(1.0, 2.0)
            except Exception:
                pass

            # ─── ステートマシン ───
            completed: set[str] = set()
            stall = 0
            last_step = ""
            deadline = time.monotonic() + 600  # 10分タイムアウト

            while time.monotonic() < deadline:
                step = await self._detect_step(page)

                if step == "done":
                    logger.debug(f"[{username}] signup done @ {page.url}")
                    break

                if step == "unknown":
                    stall += 1
                    if stall > 12:
                        logger.error(f"[{username}] unknown 状態でスタック")
                        return username, False
                    await asyncio.sleep(1.5)
                    continue

                # captcha は completed に入れず毎回処理
                if step == "captcha":
                    await self._handle_captcha(page, username, proxy_url)
                    stall = 0
                    await _human(2.0, 3.5)
                    continue

                # 同ステップが続く = エラーまたはスタック
                if step == last_step:
                    stall += 1
                    err = await self._get_error_text(page)
                    if err:
                        logger.warning(f"[{username}] step={step} error: {err}")
                    if stall > 8:
                        logger.error(f"[{username}] step={step} でスタック")
                        return username, False
                    await asyncio.sleep(1.5)
                    continue

                stall = 0
                last_step = step

                try:
                    if step == "name":
                        await self._handle_name(page, display_name)
                    elif step == "email":
                        await self._handle_email(page, email)
                    elif step == "phone":
                        await self._handle_phone(page, sms_state, username)
                    elif step == "birthday":
                        await self._handle_birthday(page, username)
                    elif step == "code":
                        ok = await self._handle_code(
                            page, username, mail_client,
                            sms_state.get("activation_id"), sms_state.get("client"),
                        )
                        if not ok:
                            return username, False
                    elif step == "password":
                        await self._handle_password(page, password)
                    elif step == "username":
                        username = await self._handle_username(page, username)
                    completed.add(step)
                except Exception as e:
                    logger.error(f"[{username}] handler {step} failed: {e}")
                    stall += 1
                    await asyncio.sleep(2.0)

            # auth_token が出るまで最大10秒リトライ
            for _ in range(10):
                cookies = await page.context.cookies("https://x.com")
                if any(c["name"] == "auth_token" for c in cookies):
                    break
                await asyncio.sleep(1.0)

            # 成功判定
            url = page.url
            if "home" in url:
                return username, True
            html = await page.content()
            if username in html or display_name in html:
                return username, True

            logger.warning(f"[{username}] signup uncertain url={url}")
            return username, False

        except Exception as e:
            logger.error(f"[{username}] signup flow error: {e}")
            return username, False

    # ────────────────────────────────────────────
    # ステップハンドラ群
    # ────────────────────────────────────────────

    async def _handle_name(self, page, display_name: str):
        logger.debug(f"STEP name → {display_name}")
        loc = page.locator('input[name="name"], input[autocomplete="name"]').first
        await loc.fill(display_name)
        await _human(0.8, 1.5)
        await self._click_next(page)
        await _human(1.0, 2.0)

    async def _handle_email(self, page, email: str):
        logger.debug(f"STEP email → {email}")
        loc = page.locator('input[name="email"], input[type="email"]').first
        await loc.fill(email)
        await _human(0.8, 1.5)
        await self._click_next(page)
        await _human(1.0, 2.0)

    async def _handle_phone(self, page, sms_state: dict, username: str):
        """
        電話番号ステップに到達した時点で初めて SMS 番号を購入する（無駄打ち防止）。
        以前のバージョンは phone_number が常に None で渡されており、
        use_phone=true でも電話認証が一度も実行されていなかった。
        """
        phone_number = sms_state.get("phone")

        if not phone_number and self._sms_mgr and self._sms_mgr.enabled:
            activation_id, phone_number, client = await self._sms_mgr.get_number()
            if phone_number:
                sms_state["activation_id"] = activation_id
                sms_state["client"] = client
                sms_state["phone"] = phone_number
                logger.info(f"[{username}] SMS番号取得 +{phone_number}")

        if not phone_number:
            logger.warning(f"[{username}] phone step but no number — trying email switch")
            await self._try_switch_to_email(page)
            return

        logger.debug(f"STEP phone → +{phone_number}")
        loc = page.locator('input[name="phone_number"], input[type="tel"]').first
        phone = phone_number if phone_number.startswith("+") else f"+{phone_number}"
        await loc.fill(phone)
        await _human(0.8, 1.5)
        await self._click_next(page)
        await _human(1.0, 2.0)

    async def _try_switch_to_email(self, page):
        for sel in [
            'span:has-text("代わりにメールアドレスを登録する")',
            'span:has-text("Use email instead")',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    await _human(0.5, 1.0)
                    return
            except Exception:
                pass

    async def _handle_birthday(self, page, username: str):
        logger.debug("STEP birthday")
        year, month, day = _gen_birthdate()

        async def _try_testid(testid: str, value: str) -> bool:
            try:
                loc = page.get_by_test_id(testid)
                if not await loc.count():
                    return False
                tag = await loc.first.evaluate("el => el.tagName.toLowerCase()")
                if tag == "select":
                    await loc.first.select_option(value=str(value))
                else:
                    # div ベースのカスタムセレクト
                    await loc.first.click()
                    await _human(0.2, 0.5)
                    # role="option" でラベル検索
                    opt = page.get_by_role("option", name=re.compile(rf"^{value}$"))
                    if await opt.count() == 0:
                        # 0-indexed でのフォールバック
                        opt = page.get_by_role("option").nth(int(value) - 1)
                    await opt.first.click()
                return True
            except Exception as e:
                logger.debug(f"birthday {testid}: {e}")
                return False

        ok_m = await _try_testid("birthdateMonth", str(month))
        await _human(0.3, 0.7)
        ok_d = await _try_testid("birthdateDay", str(day))
        await _human(0.3, 0.7)
        ok_y = await _try_testid("birthdateYear", str(year))

        if not (ok_m and ok_d and ok_y):
            # 旧 UI フォールバック (select#SELECTOR_1/2/3)
            months_en = [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December",
            ]
            for sel, label_en, label_jp, val in [
                ("select#SELECTOR_1", months_en[month - 1], f"{month}月", str(month)),
                ("select#SELECTOR_2", str(day), str(day), str(day)),
                ("select#SELECTOR_3", str(year), str(year), str(year)),
            ]:
                try:
                    s = page.locator(sel).first
                    if not await s.count():
                        continue
                    try:
                        await s.select_option(label=label_en)
                    except Exception:
                        try:
                            await s.select_option(label=label_jp)
                        except Exception:
                            await s.select_option(value=val)
                    await _human(0.3, 0.7)
                except Exception as e:
                    logger.debug(f"birthday fallback {sel}: {e}")

        logger.debug(f"birthdate {year}-{month:02d}-{day:02d}")
        await _human(0.8, 1.5)
        await self._click_next(page)
        await _human(2.0, 4.0)

    async def _handle_captcha(self, page, username: str, proxy_url: str | None):
        if not self._captcha_client:
            logger.warning(f"[{username}] FunCaptcha 検出 — CapSolverキー未設定、スキップ")
            return

        # iframe の src から public_key を動的抽出
        public_key = X_FUNCAPTCHA_KEY_FALLBACK
        try:
            iframe = page.locator('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"]').first
            if await iframe.count():
                src = await iframe.get_attribute("src") or ""
                qs = parse_qs(urlparse(src).query)
                extracted = (qs.get("public_key") or qs.get("pk") or [None])[0]
                if extracted:
                    public_key = extracted
                    logger.debug(f"[{username}] FunCaptcha key 動的取得: {public_key}")
        except Exception:
            pass

        logger.info(f"[{username}] FunCaptcha 解決中 key={public_key}")
        token = await self._captcha_client.solve_funcaptcha(
            website_url="https://x.com",
            website_public_key=public_key,
            proxy=proxy_url,
        )
        if not token:
            logger.warning(f"[{username}] Captcha 解決失敗")
            return

        # トークン注入: hidden input + postMessage + X コールバック の3段構え
        await page.evaluate("""
            (token) => {
                // 1) hidden input
                for (const n of ['funcaptcha_token', 'arkose_token', 'fc-token']) {
                    const el = document.querySelector(`input[name="${n}"]`);
                    if (el) {
                        el.value = token;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
                // 2) Arkose iframe へ postMessage
                for (const f of document.querySelectorAll('iframe')) {
                    try {
                        if (f.src && /arkoselabs|funcaptcha/.test(f.src)) {
                            f.contentWindow.postMessage(
                                JSON.stringify({
                                    eventId: 'challenge-complete',
                                    payload: { sessionToken: token }
                                }), '*'
                            );
                        }
                    } catch (e) {}
                }
                // 3) X 独自コールバック (存在すれば)
                try {
                    const pp = window.__NEXT_DATA__?.props?.pageProps;
                    if (pp?.onCaptchaVerified) pp.onCaptchaVerified(token);
                } catch (e) {}
            }
        """, token)
        await _human(1.0, 2.0)
        logger.debug(f"[{username}] Captcha トークン注入完了")

    async def _handle_code(
        self, page, username: str,
        mail_client, activation_id: str | None, sms_client_used
    ) -> bool:
        logger.debug(f"[{username}] STEP code")
        code = None

        if mail_client:
            # X は "Twitter" ブランドを廃止済みで確認メールの件名も変わっているため、
            # 件名フィルタを固定文字列にすると一致せずコード取得が失敗しやすい。
            # 新規作成した使い捨てアドレスには他のメールが来ないので、
            # 件名フィルタなしで本文中の6桁コードを直接探す方が安全。
            code = await mail_client.wait_for_code(subject_keyword="", timeout=150)
        elif activation_id and sms_client_used:
            code = await sms_client_used.wait_for_sms(activation_id, timeout_s=150)

        if not code:
            logger.warning(f"[{username}] 認証コード取得タイムアウト")
            return False

        loc = page.locator(
            'input[inputmode="numeric"], input[name="verification_code"], '
            'input[data-testid="ocfEnterTextTextInput"]'
        ).first
        await loc.fill(code)
        await _human(0.8, 1.5)
        await self._click_next(page)
        await _human(2.0, 4.0)
        logger.debug(f"[{username}] code={code}")
        return True

    async def _handle_password(self, page, password: str):
        logger.debug("STEP password")
        loc = page.locator(
            'input[name="password"], input[type="password"]'
        ).first
        await loc.fill(password)
        await _human(0.8, 1.5)
        await self._click_next(page)
        await _human(2.0, 4.0)

    async def _handle_username(self, page, username: str) -> str:
        """username 衝突時に3回までリトライ。最終的に使ったusernameを返す。"""
        logger.debug(f"STEP username → {username}")
        loc = page.locator(
            'input[name="username"], input[data-testid="ocfEnterTextTextInput"]'
        ).first
        for attempt in range(3):
            await loc.clear()
            await loc.fill(username)
            await _human(0.5, 1.0)
            await self._click_next(page)
            await _human(1.5, 3.0)

            err = await self._get_error_text(page)
            if err and any(kw in err for kw in ["使用", "taken", "既に", "already"]):
                logger.warning(f"username={username} 衝突 ({attempt+1}/3) → 再生成")
                username = _gen_username()
                continue
            break
        return username

    # ────────────────────────────────────────────
    # 共通ヘルパー
    # ────────────────────────────────────────────

    async def _detect_step(self, page) -> str:
        for step, sel in STEP_PROBES.items():
            try:
                if step == "done":
                    if "home" in page.url:
                        return "done"
                    continue
                if step == "captcha":
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        return "captcha"
                    continue
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return step
            except Exception:
                continue
        return "unknown"

    async def _click_next(self, page) -> bool:
        for sel in NEXT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    return True
            except Exception:
                continue
        try:
            await page.keyboard.press("Enter")
            return True
        except Exception:
            return False

    async def _get_error_text(self, page) -> str | None:
        try:
            loc = page.locator(ERROR_SELECTOR).first
            if await loc.count() and await loc.is_visible():
                return (await loc.inner_text()).strip()
        except Exception:
            pass
        return None

    async def _get_email(self, username: str) -> tuple[str | None, object]:
        """
        provider:
          "outlook" → Outlook のみ
          "mailtm"  → mail.tm のみ
          "auto" or "" → Outlook 優先、失敗したら mail.tm (X が mail.tm 系ドメインを
                          弾くケースが多いので Outlook を主にする)
        """
        provider = (self.config.email.provider or "auto").lower()

        async def _try_outlook():
            from ..email_provider.outlook import OutlookClient
            proxy = self.proxy_manager.get_proxy_for_account(username)
            client = OutlookClient(
                browser_manager=self.browser,
                captcha_client=self._captcha_client,
                proxy_url=proxy.url,
                domain=self.config.email.outlook_domain,
                client_id=self.config.email.outlook_client_id,
            )
            ok = await client.create_account()
            if ok and client.address:
                return client.address, client
            await client.close()
            return None, None

        async def _try_mailtm():
            client = MailTmClient()
            ok = await client.create_account()
            if ok and client.address:
                return client.address, client
            await client.close()
            return None, None

        if provider == "outlook":
            return await _try_outlook()
        if provider == "mailtm":
            return await _try_mailtm()
        if provider in ("auto", ""):
            logger.debug("[email] auto: Outlook を試行")
            addr, client = await _try_outlook()
            if addr:
                return addr, client
            logger.warning("[email] Outlook 失敗 — mail.tm にフォールバック")
            return await _try_mailtm()

        logger.warning(f"email provider '{provider}' 未実装 — mail.tm にフォールバック")
        return await _try_mailtm()

    async def _release_sms(self, sms_state: dict):
        """サインアップ失敗時に SMS 番号を解放（残高消費を止める）。"""
        client = sms_state.get("client")
        aid = sms_state.get("activation_id")
        if client and aid:
            try:
                await client.cancel(aid)
                logger.debug(f"SMS番号解放 id={aid}")
            except Exception as e:
                logger.debug(f"SMS番号解放失敗 id={aid}: {e}")

    async def _extract_auth_tokens(self, context) -> tuple[str | None, str | None]:
        try:
            cookies = await context.cookies()
            auth_token = next((c["value"] for c in cookies if c["name"] == "auth_token"), None)
            ct0 = next((c["value"] for c in cookies if c["name"] == "ct0"), None)
            return auth_token, ct0
        except Exception:
            return None, None

    async def _verify_alive(
        self, auth_token: str | None, ct0: str | None, proxy_url: str | None
    ) -> bool:
        """
        auth_token の cookie が立っているだけでは「作成直後に即凍結」を
        見逃す。verify_credentials.json を一度叩いて実際に生存しているか確認する。
        """
        if not auth_token or not HAS_CURL:
            return bool(auth_token)
        try:
            session = curl_requests.Session(impersonate="chrome136")
            session.cookies.set("auth_token", auth_token, domain=".x.com")
            if ct0:
                session.cookies.set("ct0", ct0, domain=".x.com")
                session.headers.update({"x-csrf-token": ct0})
            if proxy_url:
                session.proxies.update({"all": proxy_url})
            r = session.get(
                "https://api.x.com/1.1/account/verify_credentials.json",
                params={"include_entities": "false", "skip_status": "true"},
                timeout=20,
            )
            if r.status_code != 200:
                logger.warning(f"verify_credentials HTTP {r.status_code}")
                return False
            return not bool(r.json().get("suspended"))
        except Exception as e:
            logger.warning(f"verify_credentials error: {e}")
            # 確認自体が失敗した場合は判定不能 — auth_token の有無だけで判断
            return bool(auth_token)
