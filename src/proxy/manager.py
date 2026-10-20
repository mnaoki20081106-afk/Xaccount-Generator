"""
IPRoyalなど安い住宅用プロキシの管理。
スティッキーセッションと簡易ヘルスチェックを提供。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
import httpx
from loguru import logger

from ..config import ProxyConfig


@dataclass
class ProxySession:
    host: str
    port: int
    username: str
    password: str
    session_id: str
    assigned_at: datetime
    ttl_minutes: int

    @property
    def url(self) -> str:
        # IPRoyal形式の例（実際のフォーマットはダッシュボードに合わせて調整）
        user = f"{self.username}"
        if self.session_id:
            user = f"{self.username}-session-{self.session_id}"
        return f"http://{user}:{self.password}@{self.host}:{self.port}"

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.assigned_at + timedelta(minutes=self.ttl_minutes)


class ProxyManager:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self._sessions: dict[str, ProxySession] = {}

    def get_proxy_for_account(self, account_key: str) -> ProxySession:
        """アカウントに紐づくプロキシセッションを返す。期限切れなら新規発行。"""
        session = self._sessions.get(account_key)
        if session and not session.is_expired:
            return session

        # 新しいスティッキーセッションを発行
        import uuid
        session_id = str(uuid.uuid4())[:8]
        new_session = ProxySession(
            host=self.config.host,
            port=self.config.port,
            username=self.config.username,
            password=self.config.password,
            session_id=session_id,
            assigned_at=datetime.utcnow(),
            ttl_minutes=self.config.sticky_ttl_minutes,
        )
        self._sessions[account_key] = new_session
        logger.debug(f"New proxy session for {account_key}: {session_id}")
        return new_session

    async def health_check(self, session: ProxySession) -> bool:
        """簡易的にプロキシ経由で外部に到達できるか確認。"""
        try:
            async with httpx.AsyncClient(
                proxies=session.url, timeout=15.0, follow_redirects=True
            ) as client:
                r = await client.get("https://api.ipify.org?format=json")
                if r.status_code == 200:
                    ip = r.json().get("ip")
                    logger.debug(f"Proxy OK, exit IP: {ip}")
                    return True
        except Exception as e:
            logger.warning(f"Proxy health check failed: {e}")
        return False

    def release(self, account_key: str):
        self._sessions.pop(account_key, None)
