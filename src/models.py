from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class AccountStatus(str, Enum):
    CREATED = "created"
    WARMING = "warming"
    CLEAN = "clean"
    GRADUATED = "graduated"     # ウォームアップ完了、検索TOP表示を確認済み
    SOFT_SHADOW = "soft_shadow"
    HARD_SHADOW = "hard_shadow"
    LOCKED = "locked"
    SUSPENDED = "suspended"
    GRADUATED = "graduated"
    SOLD = "sold"


class Account(BaseModel):
    id: Optional[int] = None
    username: str
    password: str
    email: Optional[str] = None
    email_password: Optional[str] = None
    email_token: Optional[str] = None
    email_token_verification: Optional[str] = None
    phone: Optional[str] = None
    twofa_secret: Optional[str] = None  # encrypted
    auth_token: Optional[str] = None    # X session cookie (Token)
    ct0: Optional[str] = None           # X CSRF token
    status: AccountStatus = AccountStatus.CREATED
    shadow_score: float = 0.0
    proxy_id: Optional[str] = None
    fingerprint_hash: Optional[str] = None
    cookies_path: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_action_at: Optional[datetime] = None
    sold_at: Optional[datetime] = None
    notes: Optional[str] = None

    class Config:
        from_attributes = True

    def export_line(self) -> str:
        """アカウントを共有フォーマットの1行にシリアライズする。"""
        def v(x): return x or ""
        return (
            f"Username:{self.username} "
            f"Password:{self.password} "
            f"Email:{v(self.email)} "
            f"Email Password:{v(self.email_password)} "
            f"Email Token:{v(self.email_token)} "
            f"Email Token Verification:{v(self.email_token_verification)} "
            f"Phone:{v(self.phone)} "
            f"2FA:{v(self.twofa_secret)} "
            f"Token:{v(self.auth_token)}"
        )
