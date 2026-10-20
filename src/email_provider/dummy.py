"""開発用ダミー。実際の受信はしない。"""
from typing import Optional
from .base import EmailProvider


class DummyEmailProvider(EmailProvider):
    async def get_email(self) -> Optional[str]:
        import uuid
        return f"dev_{uuid.uuid4().hex[:10]}@mail.example.com"

    async def wait_for_code(self, email: str, timeout: int = 120) -> Optional[str]:
        return None

    async def release(self, email: str):
        pass
