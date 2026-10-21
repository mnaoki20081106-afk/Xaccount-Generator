"""
住宅用プロキシ管理。
- 1 スティッキーセッション に最大 max_accounts_per_ip 垢まで割り当て
- 上限に達したら COOLDOWN_WEEKS 週間クーリング、その後リセットして再利用
- セッション状態は data/proxy_sessions.json に永続保存
"""
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import httpx
from loguru import logger

from ..config import ProxyConfig

COOLDOWN_WEEKS = 3          # 上限到達後に休ませる週数
SIGNUP_INTERVAL_HOURS = 2   # 同一 IP での連続サインアップ間隔

STATE_FILE = Path("data/proxy_sessions.json")


def _now_iso() -> str:
    return datetime.utcnow().isoformat()

def _parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class ProxySession:
    def __init__(self, data: dict, config: ProxyConfig):
        self._d = data
        self._config = config

    @property
    def session_id(self) -> str:
        return self._d["session_id"]

    @property
    def account_count(self) -> int:
        return self._d.get("account_count", 0)

    @property
    def last_signup_at(self) -> datetime | None:
        return _parse_dt(self._d.get("last_signup_at"))

    @property
    def cooldown_until(self) -> datetime | None:
        return _parse_dt(self._d.get("cooldown_until"))

    @property
    def url(self) -> str:
        c = self._config
        user = f"{c.username}-session-{self.session_id}-country-{c.country}"
        return f"http://{user}:{c.password}@{c.host}:{c.port}"

    def is_cooling(self) -> bool:
        if not self.cooldown_until:
            return False
        return datetime.utcnow() < self.cooldown_until

    def is_ready_for_signup(self, max_per_ip: int) -> bool:
        """新規垢のサインアップに使えるか。"""
        if self.is_cooling():
            return False
        # クーリング明けならカウントリセット済みかチェック
        if self.account_count >= max_per_ip:
            return False
        # 前回サインアップから SIGNUP_INTERVAL_HOURS 時間以上空いているか
        last = self.last_signup_at
        if last:
            elapsed = (datetime.utcnow() - last).total_seconds() / 3600
            if elapsed < SIGNUP_INTERVAL_HOURS:
                return False
        return True

    def record_signup(self, max_per_ip: int):
        """サインアップ1件記録。上限に達したらクーリング開始。"""
        self._d["account_count"] = self.account_count + 1
        self._d["last_signup_at"] = _now_iso()
        if self._d["account_count"] >= max_per_ip:
            until = datetime.utcnow() + timedelta(weeks=COOLDOWN_WEEKS)
            self._d["cooldown_until"] = until.isoformat()
            logger.info(
                f"Proxy session={self.session_id} 上限到達 → "
                f"クーリング {COOLDOWN_WEEKS}週間 (解除: {until.strftime('%Y-%m-%d')})"
            )

    def maybe_reset_cooldown(self):
        """クーリング期間が終わっていたらリセット。"""
        cu = self.cooldown_until
        if cu and datetime.utcnow() >= cu:
            self._d["account_count"] = 0
            self._d["cooldown_until"] = None
            self._d["last_signup_at"] = None
            logger.info(f"Proxy session={self.session_id} クーリング終了 → 再利用可")


class ProxyManager:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self._sessions: dict[str, dict] = {}         # session_id → raw dict
        self._account_map: dict[str, str] = {}       # account_key → session_id
        self._warmup_map: dict[str, str] = {}        # account_key → warmup session_id
        self._load_state()

    # ──────────────────────────────────────────
    # 永続化
    # ──────────────────────────────────────────

    def _load_state(self):
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text())
                self._sessions = raw.get("sessions", {})
                self._account_map = raw.get("account_map", {})
                logger.debug(f"Proxy state loaded: {len(self._sessions)} sessions")
            except Exception as e:
                logger.warning(f"proxy state load error: {e}")

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                {"sessions": self._sessions, "account_map": self._account_map},
                indent=2,
                ensure_ascii=False,
            )
        )

    def _session_obj(self, session_id: str) -> ProxySession:
        return ProxySession(self._sessions[session_id], self.config)

    # ──────────────────────────────────────────
    # サインアップ用プロキシ取得
    # ──────────────────────────────────────────

    def get_proxy_for_account(self, account_key: str) -> ProxySession:
        """
        サインアップ用。同一IPは max_accounts_per_ip 垢まで使い回し、
        クーリング後に再利用する。
        """
        # 既に割り当て済みなら返す
        if account_key in self._account_map:
            sid = self._account_map[account_key]
            if sid in self._sessions:
                return self._session_obj(sid)

        max_per_ip = self.config.max_accounts_per_ip

        # クーリング明けのセッションをリセット
        for d in self._sessions.values():
            ProxySession(d, self.config).maybe_reset_cooldown()

        # 既存セッションから使えるものを探す（古い順 = 安定した IP）
        for sid, d in sorted(self._sessions.items(), key=lambda x: x[1].get("last_signup_at") or ""):
            sess = ProxySession(d, self.config)
            if sess.is_ready_for_signup(max_per_ip):
                sess.record_signup(max_per_ip)
                self._account_map[account_key] = sid
                self._save_state()
                logger.info(
                    f"[{account_key}] proxy reuse session={sid} "
                    f"count={sess.account_count}/{max_per_ip}"
                )
                return sess

        # 新規セッション発行
        sid = str(uuid.uuid4())[:8]
        new_d: dict = {
            "session_id": sid,
            "account_count": 0,
            "last_signup_at": None,
            "cooldown_until": None,
        }
        self._sessions[sid] = new_d
        sess = ProxySession(new_d, self.config)
        sess.record_signup(max_per_ip)
        self._account_map[account_key] = sid
        self._save_state()
        logger.info(f"[{account_key}] new proxy session={sid}")
        return sess

    # ──────────────────────────────────────────
    # ウォームアップ用プロキシ取得
    # ──────────────────────────────────────────

    def get_warmup_proxy(self, account_key: str) -> ProxySession:
        """
        ウォームアップ用。垢ごとに固有セッションIDで実質別IP。
        account_key ベースのハッシュで固定するので再起動しても同じIP。
        """
        sid = self._warmup_map.get(account_key)
        if not sid:
            # account_key から決定論的なID生成
            import hashlib
            sid = hashlib.md5(f"wu:{account_key}".encode()).hexdigest()[:8]
            self._warmup_map[account_key] = sid

        if sid not in self._sessions:
            self._sessions[sid] = {
                "session_id": sid,
                "account_count": 1,
                "last_signup_at": None,
                "cooldown_until": None,
            }
        return ProxySession(self._sessions[sid], self.config)

    # ──────────────────────────────────────────
    # ヘルスチェック
    # ──────────────────────────────────────────

    async def health_check(self, session: ProxySession) -> bool:
        try:
            async with httpx.AsyncClient(
                proxies=session.url, timeout=15.0, follow_redirects=True
            ) as client:
                r = await client.get("https://api.ipify.org?format=json")
                if r.status_code == 200:
                    ip = r.json().get("ip", "?")
                    logger.debug(f"Proxy OK exit_ip={ip} session={session.session_id}")
                    return True
        except Exception as e:
            logger.warning(f"Proxy health check failed session={session.session_id}: {e}")
        return False

    # ──────────────────────────────────────────
    # ユーティリティ
    # ──────────────────────────────────────────

    def release(self, account_key: str):
        self._account_map.pop(account_key, None)

    def status_report(self) -> dict:
        """現在のセッション状況サマリ。"""
        total = len(self._sessions)
        cooling = sum(1 for d in self._sessions.values()
                      if ProxySession(d, self.config).is_cooling())
        ready = total - cooling
        return {"total_sessions": total, "cooling": cooling, "ready": ready}
