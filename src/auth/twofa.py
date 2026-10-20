"""TOTP 2FA管理。"""
import pyotp
from loguru import logger

from ..utils.crypto import encrypt_secret, decrypt_secret


class TwoFAManager:
    def __init__(self, encryption_key: str):
        self.encryption_key = encryption_key

    def generate_secret(self) -> str:
        return pyotp.random_base32()

    def get_totp_code(self, secret: str) -> str:
        totp = pyotp.TOTP(secret)
        return totp.now()

    def encrypt(self, secret: str) -> str:
        return encrypt_secret(secret, self.encryption_key)

    def decrypt(self, encrypted: str) -> str:
        return decrypt_secret(encrypted, self.encryption_key)

    def provisioning_uri(self, secret: str, username: str) -> str:
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name="XAccount")
