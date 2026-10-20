"""
メールプロバイダの抽象ベース。
"""
from abc import ABC, abstractmethod
from typing import Optional


class EmailProvider(ABC):
    @abstractmethod
    async def get_email(self) -> Optional[str]:
        """新しいメールアドレスを1つ取得"""
        ...

    @abstractmethod
    async def wait_for_code(self, email: str, timeout: int = 120) -> Optional[str]:
        """認証コードが届くまで待機して返す"""
        ...

    @abstractmethod
    async def release(self, email: str):
        """使い終わったアドレスを解放（必要な場合）"""
        ...
