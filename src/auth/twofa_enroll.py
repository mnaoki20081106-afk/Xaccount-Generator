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


async def _decode_when_rendered(page, attempts: int, interval_ms: int) -> Optional[str]:
    """QR は非同期に描画されるため、読めるまで数回スクリーンショットを撮り直す。"""
    for _ in range(attempts):
        secret = decode_qr_secret(await page.screenshot())
        if secret:
            return secret
        await page.wait_for_timeout(interval_ms)
    return None


async def enable_totp_2fa(
    page,
    *,
    step_wait_ms: int = 1000,
    click_timeout_ms: int = 15000,
    qr_attempts: int = 10,
    confirm_timeout_ms: int = 10000,
    stage: Optional[dict] = None,
) -> Optional[str]:
    """ログイン済みの page で 2FA を有効化する。成功時のみ平文の secret を返す。

    どの段階で失敗しても例外は出さず None を返す（呼び出し側は 2FA なしで納品する）。
    stage を渡すと、QR が読めず失敗した場合に stage["failed_at"] = "qr" を入れる
    （リソースブロック有りで QR が出なかった場合に、ブロック無しで再試行するため）。
    """
    async def click(text: str):
        await page.get_by_text(text).first.click(timeout=click_timeout_ms)

    try:
        await page.goto(LOGIN_VERIFICATION_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(step_wait_ms)
        await click("Authentication app")
        await page.wait_for_timeout(step_wait_ms)
        await click("Get started")

        secret = await _decode_when_rendered(page, qr_attempts, step_wait_ms)
        if not secret:
            logger.warning("2FA: QRコードから secret を取得できず")
            if stage is not None:
                stage["failed_at"] = "qr"
            return None

        await click("Next")

        code_input = page.locator("input[type=text]").first
        await code_input.wait_for(state="visible", timeout=click_timeout_ms)
        await code_input.fill(pyotp.TOTP(secret).now())
        await page.wait_for_timeout(step_wait_ms)
        await click("Confirm")

        # 受理されれば入力欄は消える。拒否されると残る（残れば有効化できていない扱い）。
        try:
            await code_input.wait_for(state="hidden", timeout=confirm_timeout_ms)
        except Exception:
            logger.warning("2FA: 確認コードが受理されなかった（入力欄が残っている）")
            return None
        return secret
    except Exception as e:
        logger.warning(f"2FA: 有効化に失敗: {type(e).__name__}: {e}")
        return None
