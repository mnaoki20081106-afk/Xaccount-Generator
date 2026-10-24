"""X 側で 2FA(認証アプリ/TOTP)を有効化し、登録した秘密鍵を返す。

参考実装: Dev380/twittergen の enable_2fa()
  設定画面 login_verification → "Authentication app" → "Get started" →
  画面に出る QR コードから secret を取り出す → "Next" →
  その secret で生成した TOTP を入力 → "Confirm"
twittergen は英語UIの文言を前提にしているため、呼び出し側は locale="en-US" の
コンテキストを渡すこと。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlparse

import pyotp
from loguru import logger

LOGIN_VERIFICATION_URL = "https://x.com/settings/account/login_verification"


def parse_totp_secret(otpauth_uri: str) -> Optional[str]:
    """otpauth://totp/...?secret=XXXX&... から secret を取り出す。"""
    if not otpauth_uri.startswith("otpauth://"):
        return None
    secret = parse_qs(urlparse(otpauth_uri).query).get("secret")
    return secret[0] if secret else None


def decode_qr_secret(png_bytes: bytes) -> Optional[str]:
    """スクリーンショット中の QR コードをデコードして TOTP secret を返す。"""
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        return None
    data, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
    return parse_totp_secret(data) if data else None


async def enable_totp_2fa(page, *, step_wait_ms: int = 1000, confirm_wait_ms: int = 4000) -> Optional[str]:
    """ログイン済みの page で 2FA を有効化する。成功時のみ平文の secret を返す。"""
    await page.goto(LOGIN_VERIFICATION_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(step_wait_ms)
    await page.get_by_text("Authentication app").first.click()
    await page.wait_for_timeout(step_wait_ms)
    await page.get_by_text("Get started").first.click()
    await page.wait_for_timeout(step_wait_ms * 2)

    secret = decode_qr_secret(await page.screenshot())
    if not secret:
        logger.warning("2FA: QRコードから secret を取得できず")
        return None

    await page.get_by_text("Next").first.click()
    await page.wait_for_timeout(step_wait_ms)

    code_input = page.locator("input[type=text]").first
    await code_input.fill(pyotp.TOTP(secret).now())
    await page.wait_for_timeout(step_wait_ms)
    await page.get_by_text("Confirm").first.click()
    await page.wait_for_timeout(confirm_wait_ms)

    # コードが拒否されると入力欄が残る。残っていれば有効化できていない扱いにする。
    if await code_input.count() and await code_input.is_visible():
        logger.warning("2FA: 確認コードが受理されなかった（入力欄が残っている）")
        return None
    return secret
