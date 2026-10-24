import asyncio
import io

import pyotp
import qrcode

from src.auth import twofa_enroll
from src.auth.twofa_enroll import enable_totp_2fa

SECRET = "JBSWY3DPEHPK3PXP"


def _qr_png(secret: str = SECRET) -> bytes:
    buf = io.BytesIO()
    qrcode.make(f"otpauth://totp/X:user?secret={secret}&issuer=X").save(buf, "PNG")
    return buf.getvalue()


class _Locator:
    def __init__(self, page, name):
        self.page, self.name = page, name

    @property
    def first(self):
        return self

    async def click(self, timeout=None):
        if self.name in self.page.missing:
            raise TimeoutError(self.name)
        self.page.clicked.append(self.name)

    async def wait_for(self, state, timeout=None):
        if state == "hidden" and self.page.code_rejected:
            raise TimeoutError("still visible")

    async def fill(self, value):
        self.page.filled = value


class _Page:
    def __init__(self, shots, code_rejected=False, missing=()):
        self.shots = list(shots)
        self.code_rejected = code_rejected
        self.missing = set(missing)
        self.clicked, self.filled, self.url = [], None, None

    async def goto(self, url, wait_until=None):
        self.url = url

    async def wait_for_timeout(self, ms):
        pass

    async def screenshot(self):
        return self.shots.pop(0) if len(self.shots) > 1 else self.shots[0]

    def get_by_text(self, text):
        return _Locator(self, text)

    def locator(self, sel):
        return _Locator(self, sel)


def run(coro):
    return asyncio.run(coro)


def test_success_returns_secret_and_enters_valid_totp():
    page = _Page([b"not-an-image", _qr_png()])
    assert run(enable_totp_2fa(page)) == SECRET
    assert page.url == twofa_enroll.LOGIN_VERIFICATION_URL
    assert page.clicked == ["Authentication app", "Get started", "Next", "Confirm"]
    assert pyotp.TOTP(SECRET).verify(page.filled)


def test_rejected_code_returns_none():
    assert run(enable_totp_2fa(_Page([_qr_png()], code_rejected=True))) is None


def test_unreadable_qr_returns_none():
    assert run(enable_totp_2fa(_Page([b"nope"]), qr_attempts=3)) is None


def test_missing_button_returns_none_instead_of_raising():
    assert run(enable_totp_2fa(_Page([_qr_png()], missing={"Get started"}))) is None
