"""2FA secretなどの簡易暗号化。"""
import base64
import hashlib
from cryptography.fernet import Fernet


def _derive_key(master: str) -> bytes:
    digest = hashlib.sha256(master.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plain: str, master_key: str) -> str:
    f = Fernet(_derive_key(master_key))
    return f.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str, master_key: str) -> str:
    f = Fernet(_derive_key(master_key))
    return f.decrypt(token.encode("utf-8")).decode("utf-8")
