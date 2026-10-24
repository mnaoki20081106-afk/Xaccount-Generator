from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class AccountStatus(str, Enum):
    CREATED = "created"
    WARMING = "warming"
    CLEAN = "clean"
    SOFT_SHADOW = "soft_shadow"
    HARD_SHADOW = "hard_shadow"
    LOCKED = "locked"
    SUSPENDED = "suspended"
    SOLD = "sold"


class Account(BaseModel):
    id: Optional[int] = None
    username: str
    password: str
    email: Optional[str] = None
    email_password: Optional[str] = None  # encrypted
    email_token: Optional[str] = None
    email_token_verification: Optional[str] = None
    phone: Optional[str] = None
    twofa_secret: Optional[str] = None  # encrypted
    auth_token: Optional[str] = None    # X session cookie
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
