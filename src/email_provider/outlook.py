"""
Outlook / Hotmail アカウント生成 + Graph API メール受信。

フロー:
  1. patchright でサインアップ (ステートマシン方式)
  2. サインアップ中の回復メール検証は一時 MailTmClient で処理
  3. 登録後に OAuth2 authorization code → refresh_token を取得
  4. wait_for_code() は Graph API でポーリング

MailTmClient と同じインターフェース:
  create_account() / address / wait_for_code() / get_latest_code() / close()
"""
from __future__ import annotations

import asyncio
import random
import re
import secrets
import string
import time
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx
from loguru import logger

# ──────────────────────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────────────────────

SIGNUP_URL = "https://signup.live.com/signup"

# Microsoft Graph / OAuth2
_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
_GRAPH_MESSAGES = "https://graph.microsoft.com/v1.0/me/messages"
_SCOPES = "https://graph.microsoft.com/Mail.Read offline_access"

# デフォルト client_id: Outlook Web App の公開 ID
_DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
_REDIRECT_URI = "https://localhost"

# ステップ検出プローブ
_STEP_PROBES = {
    "username":  ['input[name="MemberName"]', '#floatingLabelInput10', 'input[aria-label*="メール"]'],
    "password":  ['input[type="password"]', 'input[name="Password"]', '#PasswordInput'],
    "firstname": ['input[name="FirstName"]', '#FirstName', 'input[aria-label*="名"]'],
    "birthday":  ['select[name="BirthYear"]', '#BirthYear', 'input[name="BirthYear"]'],
    "captcha":   ['iframe#enforcementFrame'],
    "phone":     ['input[name="PhoneNumber"]', 'input[type="tel"]', '#idTxtBx_PhoneNumber',
                  'input[aria-label*="電話"]', 'input[placeholder*="phone" i]'],
    "recovery":  ['input[name="MemberName"][type="email"]', 'input[aria-label*="回復"]',
                  'input[placeholder*="email" i]'],
    "verify":    ['input[name="otc"]', '#idTxtBx_OTC_Password', 'input[aria-label*="コード"]',
                  'input[maxlength="6"]', 'input[maxlength="8"]'],
    "agree":     ['input[id*="iAccrualCancel"]', 'input[value*="同意"]', 'input[value*="Agree"]',
                  'button:has-text("同意")'],
    "done":      ['#meControl', '[data-task="profile"]', 'a[href*="account.microsoft.com"]'],
}

_NEXT_SELECTORS = [
    '[data-testid="primaryButton"]',
    'input[type="submit"]',
    'button[type="submit"]',
    '#iSignupAction',
    'input[value*="次"]',
    'input[value*="Next"]',
    'button:has-text("次へ")',
]

# ──────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────

def _gen_local(length: int | None = None) -> str:
    length = length or random.randint(11, 17)
    chars = string.ascii_lowercase + string.digits
    return secrets.choice(string.ascii_lowercase) + "".join(
        secrets.choice(chars) for _ in range(length - 1)
    )


def _gen_password(n: int = 14) -> str:
    symbols = "!@#$%^&*"
    pool = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(n))
        if (any(c.isupper() for c in pw)
                and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in symbols for c in pw)):
            return pw


def _gen_name() -> tuple[str, str]:
    first = secrets.choice(string.ascii_uppercase) + "".join(
        secrets.choice(string.ascii_lowercase) for _ in range(random.randint(3, 7))
    )
    last = secrets.choice(string.ascii_uppercase) + "".join(
        secrets.choice(string.ascii_lowercase) for _ in range(random.randint(3, 7))
    )
    return first, last


async def _human(lo: float = 0.5, hi: float = 1.5):
    await asyncio.sleep(random.uniform(lo, hi))


def _iso_ago(seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    dt = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ──────────────────────────────────────────────────────────────
# OutlookClient
# ──────────────────────────────────────────────────────────────

class OutlookClient:
    """
    Outlook アカウントを新規作成し、Graph API でメール受信する。
    account_factory.py の _get_email() から MailTmClient の代替として使う。
    """

    def __init__(
        self,
        browser_manager,
        captcha_client=None,
        proxy_url: str | None = None,
        domain: str = "outlook.com",
        client_id: str = _DEFAULT_CLIENT_ID,
        sms_client=None,
    ):
        self.browser = browser_manager
        self.captcha_client = captcha_client
        self.proxy_url = proxy_url
        self.domain = domain
        self.client_id = client_id
        self.sms_client = sms_client  # FiveSimClient or compatible — for phone verification

        # 登録後に設定される
        self.address: str | None = None
        self.password: str | None = None
        self._refresh_token: str | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._context = None

    # ──────────────────────────────────────────
    # 公開 API
    # ──────────────────────────────────────────

    async def create_account(self) -> bool:
        local = _gen_local()
        password = _gen_password()
        email = f"{local}@{self.domain}"

        self._context = await self.browser.new_context(proxy_url=self.proxy_url)
        page = await self.browser.new_page(self._context, block_resources=False)
        page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

        try:
            ok = await self._run_signup(page, local, password, email)
            if not ok:
                return False

            # OAuth2 で refresh_token 取得（失敗しても wait_for_code は後で再試行可能）
            await self._acquire_tokens(page)

            self.address = email
            self.password = password
            logger.success(f"[Outlook] {email} 作成完了 token={'yes' if self._refresh_token else 'no'}")
            return True

        except Exception as e:
            logger.exception(f"[Outlook] create_account failed: {e}")
            return False
        finally:
            await page.close()

    async def wait_for_code(
        self,
        subject_keyword: str = "",
        timeout: int = 150,
        poll_interval: int = 5,
    ) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code = await self.get_latest_code(subject_keyword=subject_keyword)
            if code:
                return code
            await asyncio.sleep(poll_interval)
        logger.warning(f"[Outlook] {subject_keyword} コードタイムアウト ({timeout}s)")
        return None

    async def get_latest_code(self, subject_keyword: str = "") -> str | None:
        token = await self._ensure_access_token()
        if not token:
            logger.debug("[Outlook] access_token なし — コード取得スキップ")
            return None

        proxy_kwargs = {"proxy": self.proxy_url} if self.proxy_url else {}
        try:
            async with httpx.AsyncClient(timeout=30, **proxy_kwargs) as client:
                params: dict = {
                    "$top": "10",
                    "$select": "subject,body,receivedDateTime",
                    "$orderby": "receivedDateTime DESC",
                    "$filter": f"receivedDateTime gt {_iso_ago(600)}",
                }
                r = await client.get(
                    _GRAPH_MESSAGES,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Prefer": 'outlook.body-content-type="text"',
                    },
                    params=params,
                )
                for msg in r.json().get("value", []):
                    text = (
                        msg.get("body", {}).get("content", "")
                        + " "
                        + msg.get("subject", "")
                    )
                    if subject_keyword and subject_keyword.lower() not in text.lower():
                        continue
                    m = re.search(r"\b(\d{6,8})\b", text)
                    if m:
                        return m.group(1)
        except Exception as e:
            logger.debug(f"[Outlook] graph fetch failed: {e}")
        return None

    async def close(self):
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

    # ──────────────────────────────────────────
    # サインアップ ステートマシン
    # ──────────────────────────────────────────

    async def _run_signup(self, page, local: str, password: str, email: str) -> bool:
        await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        await _human(1.5, 3.0)

        first_name, last_name = _gen_name()
        year = random.randint(1988, 2002)
        month = random.randint(1, 12)
        day = random.randint(1, 28)

        # 回復メール用の一時クライアント（サインアップ中に必要な場合）
        recovery_client = None
        recovery_address = None
        # 電話認証用（sms_clientがあれば使う）
        phone_order_id: str | None = None
        phone_number: str | None = None

        completed: set[str] = set()
        stall = 0
        last_step = ""
        deadline = time.monotonic() + 600

        while time.monotonic() < deadline:
            step = await self._detect_step(page)

            if step == "done":
                logger.debug(f"[Outlook] signup done url={page.url}")
                break

            if step == "unknown":
                stall += 1
                # done 的な URL になっていれば抜ける
                url = page.url
                if any(k in url for k in ("account.microsoft", "outlook.live", "outlook.com/mail")):
                    logger.debug(f"[Outlook] done via URL: {url}")
                    break
                if stall > 15:
                    logger.error(f"[Outlook] unknown 状態でスタック url={url}")
                    return False
                await asyncio.sleep(1.5)
                continue

            if step == "captcha":
                await self._handle_captcha(page)
                stall = 0
                await _human(2.0, 4.0)
                continue

            if step == last_step:
                stall += 1
                if stall > 8:
                    logger.error(f"[Outlook] step={step} でスタック")
                    return False
                await asyncio.sleep(1.5)
                continue

            stall = 0
            last_step = step

            try:
                if step == "username" and step not in completed:
                    await self._fill(page, _STEP_PROBES["username"], local)
                    # ドメイン変更 (hotmail.com の場合)
                    if self.domain == "hotmail.com":
                        await self._switch_domain(page)
                    await _human(0.5, 1.0)
                    await self._click_next(page)
                    await _human(1.5, 3.0)

                elif step == "password" and step not in completed:
                    await self._fill(page, _STEP_PROBES["password"], password)
                    await _human(0.5, 1.0)
                    await self._click_next(page)
                    await _human(1.5, 3.0)

                elif step == "firstname" and step not in completed:
                    await self._fill(page, ['input[name="FirstName"]', '#FirstName'], first_name)
                    await _human(0.3, 0.6)
                    await self._fill(page, ['input[name="LastName"]', '#LastName'], last_name)
                    await _human(0.5, 1.0)
                    await self._click_next(page)
                    await _human(1.5, 3.0)

                elif step == "birthday" and step not in completed:
                    await self._fill_birthday(page, year, month, day)
                    await _human(0.5, 1.0)
                    await self._click_next(page)
                    await _human(1.5, 3.0)

                elif step == "phone" and step not in completed:
                    result = await self._handle_phone(page)
                    if result == "skip":
                        completed.add(step)
                    elif result == "ok":
                        phone_order_id = getattr(self, "_last_phone_order_id", None)
                        phone_number = getattr(self, "_last_phone_number", None)
                        completed.add(step)
                    else:
                        # Phone failed — try skip
                        if not await self._try_skip(page):
                            logger.error("[Outlook] phone step 失敗かつスキップ不可")
                            return False

                elif step == "recovery" and step not in completed:
                    # 回復メール要求 — 一時メールで対応
                    if recovery_client is None:
                        from .mailtm import MailTmClient
                        recovery_client = MailTmClient()
                        ok = await recovery_client.create_account()
                        recovery_address = recovery_client.address if ok else None

                    if recovery_address:
                        logger.debug(f"[Outlook] recovery email → {recovery_address}")
                        await self._fill(page, _STEP_PROBES["recovery"], recovery_address)
                        await _human(0.5, 1.0)
                        await self._click_next(page)
                        await _human(2.0, 4.0)
                    else:
                        # スキップ試行
                        if not await self._try_skip(page):
                            logger.error("[Outlook] recovery email 取得失敗かつスキップ不可")
                            return False

                elif step == "verify" and step not in completed:
                    code = None
                    # 電話認証コードを優先
                    if phone_order_id and self.sms_client:
                        code = await self.sms_client.wait_for_sms(phone_order_id, timeout_s=90)
                    # 次に回復メールコード
                    if not code and recovery_client:
                        code = await recovery_client.wait_for_code(
                            subject_keyword="Microsoft", timeout=120
                        )
                    if not code:
                        logger.error("[Outlook] verify code 取得失敗")
                        return False
                    await self._fill(page, _STEP_PROBES["verify"], code)
                    await _human(0.5, 1.0)
                    await self._click_next(page)
                    await _human(2.0, 4.0)

                elif step == "agree":
                    await self._click_first(page, _STEP_PROBES["agree"])
                    await _human(1.0, 2.0)
                    completed.add("agree")
                    continue  # stall リセットのため continue

                completed.add(step)

            except Exception as e:
                logger.error(f"[Outlook] handler {step} failed: {e}")
                stall += 1
                await asyncio.sleep(2.0)

        if recovery_client:
            await recovery_client.close()

        url = page.url
        return any(k in url for k in (
            "account.microsoft", "outlook.live", "outlook.com/mail",
            "privacynotice", "live.com/mail"
        ))

    # ──────────────────────────────────────────
    # ステップ検出
    # ──────────────────────────────────────────

    async def _detect_step(self, page) -> str:
        url = page.url
        # done 判定を URL で優先
        if any(k in url for k in ("account.microsoft", "outlook.live", "outlook.com/mail")):
            return "done"

        for step, selectors in _STEP_PROBES.items():
            try:
                for sel in selectors:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible(timeout=500):
                        return step
            except Exception:
                continue
        return "unknown"

    # ──────────────────────────────────────────
    # 各ステップハンドラ
    # ──────────────────────────────────────────

    async def _fill_birthday(self, page, year: int, month: int, day: int):
        for sel, val in [
            ('select[name="BirthYear"]', str(year)),
            ('select[name="BirthMonth"]', str(month)),
            ('select[name="BirthDay"]', str(day)),
            ('#BirthYear', str(year)),
            ('#BirthMonth', str(month)),
            ('#BirthDay', str(day)),
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=800):
                    tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                    if tag == "select":
                        await loc.select_option(value=val)
                    else:
                        await loc.fill(val)
                    await _human(0.2, 0.5)
            except Exception:
                continue

    async def _handle_captcha(self, page):
        if not self.captcha_client:
            logger.warning("[Outlook] captcha 検出 — solver 未設定、30s 待機")
            await asyncio.sleep(30)
            return

        public_key = None
        try:
            frame = page.locator("iframe#enforcementFrame").first
            src = await frame.get_attribute("src") or ""
            m = re.search(r'[?&](?:public_key|pk)=([A-Za-z0-9\-]+)', src)
            if m:
                public_key = m.group(1)
                logger.debug(f"[Outlook] captcha key: {public_key}")
        except Exception:
            pass

        if not public_key:
            logger.warning("[Outlook] captcha public_key 抽出失敗")
            return

        # CapBypass を優先 (Outlook FunCaptcha の成功率が高い)
        # CaptchaManager の場合は内部プロバイダーから CapBypassClient を探す
        capbypass = None
        if hasattr(self.captcha_client, "_providers"):
            for entry in self.captcha_client._providers:
                if type(entry.client).__name__ == "CapBypassClient" and not entry.disabled:
                    capbypass = entry.client
                    break

        token = None
        if capbypass:
            try:
                token = await capbypass.solve_funcaptcha(
                    website_url="https://signup.live.com",
                    website_public_key=public_key,
                    proxy=self.proxy_url,
                )
                if token:
                    logger.debug("[Outlook] captcha solved via CapBypass")
            except Exception as e:
                logger.debug(f"[Outlook] CapBypass failed: {e}, falling back")
                token = None

        if not token:
            token = await self.captcha_client.solve_funcaptcha(
                website_url="https://signup.live.com",
                website_public_key=public_key,
                proxy=self.proxy_url,
            )
        if not token:
            logger.warning("[Outlook] captcha 解決失敗")
            return

        await page.evaluate("""
            (token) => {
                for (const n of ['funcaptcha_token', 'arkose_token', 'fc-token']) {
                    const el = document.querySelector(`input[name="${n}"]`);
                    if (el) {
                        el.value = token;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
                const f = document.querySelector('iframe#enforcementFrame');
                if (f) {
                    try {
                        f.contentWindow.postMessage(JSON.stringify({
                            eventId: 'challenge-complete',
                            payload: { sessionToken: token }
                        }), '*');
                    } catch(e) {}
                }
            }
        """, token)
        await _human(1.0, 2.0)
        logger.debug("[Outlook] captcha トークン注入完了")

    async def _handle_phone(self, page) -> str:
        """
        電話番号ステップを処理。
        sms_client があれば番号を取得して入力、なければスキップを試みる。
        戻り値: "ok" | "skip" | "fail"
        """
        # まずスキップできるか試す
        if await self._try_skip(page):
            logger.debug("[Outlook] phone step スキップ成功")
            return "skip"

        if not self.sms_client:
            logger.debug("[Outlook] sms_client 未設定 — phone スキップ試行のみ")
            return "fail"

        try:
            order_id, phone = await self.sms_client.get_number()
            if not order_id or not phone:
                logger.warning("[Outlook] 電話番号取得失敗")
                return "fail"

            # +国番号なしで入力（Microsoft は自動補完する）
            phone_input = phone.lstrip("+")
            filled = await self._fill(page, _STEP_PROBES["phone"], phone_input)
            if not filled:
                await self.sms_client.cancel(order_id)
                return "fail"

            await _human(0.5, 1.0)
            await self._click_next(page)
            await _human(2.0, 4.0)

            # order_id を保存して verify ステップで SMS を取得できるようにする
            self._last_phone_order_id = order_id
            self._last_phone_number = phone_input
            logger.debug(f"[Outlook] phone={phone_input} 入力完了 order={order_id}")
            return "ok"
        except Exception as e:
            logger.error(f"[Outlook] _handle_phone error: {e}")
            return "fail"

    async def _switch_domain(self, page):
        """@hotmail.com に切り替える。"""
        try:
            for sel in ['button.domain-select', '[aria-haspopup="listbox"]', '[aria-haspopup="true"]']:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.click()
                    await _human(0.3, 0.6)
                    break
            opt = page.locator('[role="option"]:text-is("@hotmail.com")').first
            if await opt.count():
                await opt.click()
        except Exception:
            pass

    async def _try_skip(self, page) -> bool:
        for sel in [
            'a:has-text("Skip")', 'button:has-text("Skip")',
            'a:has-text("スキップ")', 'button:has-text("スキップ")',
            'input[value*="skip" i]',
        ]:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=800):
                    await loc.click()
                    await _human(0.5, 1.0)
                    return True
            except Exception:
                continue
        return False

    # ──────────────────────────────────────────
    # OAuth2 / Graph API
    # ──────────────────────────────────────────

    async def _acquire_tokens(self, page):
        """サインアップ完了済みページから OAuth2 code を取得して refresh_token に交換。"""
        auth_url = (
            f"{_AUTH_URL}"
            f"?client_id={self.client_id}"
            f"&response_type=code"
            f"&redirect_uri={_REDIRECT_URI}"
            f"&scope={_SCOPES.replace(' ', '%20')}"
            f"&response_mode=query"
            f"&prompt=none"
        )
        try:
            # localhost へのリダイレクトは失敗するが URL に code が乗る
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass

        url = page.url
        m = re.search(r'[?&]code=([^&]+)', url)
        if not m:
            logger.debug("[Outlook] auth code 未取得 — token なし")
            return

        code = m.group(1)
        proxy_kwargs = {"proxy": self.proxy_url} if self.proxy_url else {}
        try:
            async with httpx.AsyncClient(timeout=30, **proxy_kwargs) as client:
                r = await client.post(_TOKEN_URL, data={
                    "client_id": self.client_id,
                    "code": code,
                    "redirect_uri": _REDIRECT_URI,
                    "grant_type": "authorization_code",
                    "scope": _SCOPES,
                })
                data = r.json()
                if "refresh_token" in data:
                    self._refresh_token = data["refresh_token"]
                    self._access_token = data.get("access_token")
                    self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
                    logger.debug("[Outlook] refresh_token 取得成功")
                else:
                    logger.debug(f"[Outlook] token exchange failed: {data.get('error_description', data)}")
        except Exception as e:
            logger.debug(f"[Outlook] token exchange error: {e}")

    async def _ensure_access_token(self) -> str | None:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        if not self._refresh_token:
            return None

        proxy_kwargs = {"proxy": self.proxy_url} if self.proxy_url else {}
        try:
            async with httpx.AsyncClient(timeout=30, **proxy_kwargs) as client:
                r = await client.post(_TOKEN_URL, data={
                    "client_id": self.client_id,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                    "scope": _SCOPES,
                })
                data = r.json()
                if "access_token" in data:
                    self._access_token = data["access_token"]
                    self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
                    if "refresh_token" in data:
                        self._refresh_token = data["refresh_token"]
                    return self._access_token
        except Exception as e:
            logger.debug(f"[Outlook] token refresh error: {e}")
        return None

    # ──────────────────────────────────────────
    # 共通ヘルパー
    # ──────────────────────────────────────────

    async def _fill(self, page, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1500):
                    await loc.fill(value)
                    return True
            except Exception:
                continue
        return False

    async def _click_first(self, page, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible(timeout=1500):
                    await loc.click()
                    return True
            except Exception:
                continue
        return False

    async def _click_next(self, page) -> bool:
        return await self._click_first(page, _NEXT_SELECTORS)
