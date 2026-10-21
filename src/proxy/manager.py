"""
住宅用プロキシ管理。
- 1 スティッキーセッション に最大 max_accounts_per_ip 垢まで割り当て
- 上限に達したら COOLDOWN_WEEKS 週間クーリング、その後リセットして再利用
- セッション状態は data/proxy_sessions.json に永続保存
- サーキットブレーカー: 連続失敗 CIRCUIT_THRESHOLD 回でセッション一時遮断
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from ..config import ProxyConfig

COOLDOWN_WEEKS = 3
SIGNUP_INTERVAL_HOURS = 2
# サーキットブレーカー設定
CIRCUIT_THRESHOLD = 4       # 連続失敗でオープン
CIRCUIT_RESET_MINUTES = 30  # オープン後この時間経過で半オープン→再試行

STATE_FILE = Path("data/proxy_sessions.json")

_CHECK_URLS = [
    "https://api.ipify.org?format=json",
    "https://api.my-ip.io/ip.json",
    "https://ip.seeip.org/jsonip",
]


def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _parse_dt(s: str | None) -> Optional[datetime]:
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
    def last_signup_at(self) -> Optional[datetime]:
        return _parse_dt(self._d.get("last_signup_at"))

    @property
    def cooldown_until(self) -> Optional[datetime]:
        return _parse_dt(self._d.get("cooldown_until"))

    # ── サーキットブレーカー ──
    @property
    def consecutive_failures(self) -> int:
        return self._d.get("consecutive_failures", 0)

    @property
    def circuit_open_until(self) -> Optional[datetime]:
        return _parse_dt(self._d.get("circuit_open_until"))

    def is_circuit_open(self) -> bool:
        until = self.circuit_open_until
        if until and datetime.utcnow() < until:
            return True
        # 期限切れ → 自動リセット
        if until and datetime.utcnow() >= until:
            self._d["circuit_open_until"] = None
            self._d["consecutive_failures"] = 0
        return False

    def record_health_failure(self):
        fails = self.consecutive_failures + 1
        self._d["consecutive_failures"] = fails
        if fails >= CIRCUIT_THRESHOLD:
            until = datetime.utcnow() + timedelta(minutes=CIRCUIT_RESET_MINUTES)
            self._d["circuit_open_until"] = until.isoformat()
            logger.warning(
                f"Proxy session={self.session_id} circuit OPEN "
                f"({fails} failures) until {until.strftime('%H:%M')}"
            )

    def record_health_success(self, exit_ip: str = ""):
        self._d["consecutive_failures"] = 0
        self._d["circuit_open_until"] = None
        self._d["last_exit_ip"] = exit_ip

    # ──────────────────────────────────────────

    @property
    def url(self) -> str:
        c = self._config
        user = f"{c.username}-session-{self.session_id}-country-{c.country}"
        return f"http://{user}:{c.password}@{c.host}:{c.port}"

    def is_cooling(self) -> bool:
        cu = self.cooldown_until
        return bool(cu and datetime.utcnow() < cu)

    def is_ready_for_signup(self, max_per_ip: int) -> bool:
        if self.is_cooling():
            return False
        if self.is_circuit_open():
            return False
        if self.account_count >= max_per_ip:
            return False
        last = self.last_signup_at
        if last:
            elapsed = (datetime.utcnow() - last).total_seconds() / 3600
            if elapsed < SIGNUP_INTERVAL_HOURS:
                return False
        return True

    def record_signup(self, max_per_ip: int):
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
        cu = self.cooldown_until
        if cu and datetime.utcnow() >= cu:
            self._d["account_count"] = 0
            self._d["cooldown_until"] = None
            self._d["last_signup_at"] = None
            logger.info(f"Proxy session={self.session_id} クーリング終了 → 再利用可")


class ProxyManager:
    def __init__(self, config: ProxyConfig):
        self.config = config
        self._sessions: dict[str, dict] = {}
        self._account_map: dict[str, str] = {}
        self._warmup_map: dict[str, str] = {}
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
        if account_key in self._account_map:
            sid = self._account_map[account_key]
            if sid in self._sessions:
                return self._session_obj(sid)

        max_per_ip = self.config.max_accounts_per_ip

        for d in self._sessions.values():
            ProxySession(d, self.config).maybe_reset_cooldown()

        for sid, d in sorted(
            self._sessions.items(),
            key=lambda x: x[1].get("last_signup_at") or "",
        ):
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

        sid = str(uuid.uuid4())[:8]
        new_d: dict = {
            "session_id": sid,
            "account_count": 0,
            "last_signup_at": None,
            "cooldown_until": None,
            "consecutive_failures": 0,
            "circuit_open_until": None,
            "last_exit_ip": "",
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
        sid = self._warmup_map.get(account_key)
        if not sid:
            sid = hashlib.md5(f"wu:{account_key}".encode()).hexdigest()[:8]
            self._warmup_map[account_key] = sid

        if sid not in self._sessions:
            self._sessions[sid] = {
                "session_id": sid,
                "account_count": 1,
                "last_signup_at": None,
                "cooldown_until": None,
                "consecutive_failures": 0,
                "circuit_open_until": None,
                "last_exit_ip": "",
            }
        return ProxySession(self._sessions[sid], self.config)

    # ──────────────────────────────────────────
    # ヘルスチェック（サーキットブレーカー連携）
    # ──────────────────────────────────────────

    async def health_check(self, session: ProxySession) -> bool:
        if session.is_circuit_open():
            logger.debug(f"Proxy session={session.session_id} circuit OPEN, skip health check")
            return False

        for check_url in _CHECK_URLS:
            try:
                async with httpx.AsyncClient(
                    proxy=session.url, timeout=12.0, follow_redirects=True
                ) as client:
                    r = await client.get(check_url)
                    if r.status_code == 200:
                        try:
                            ip = r.json().get("ip") or r.json().get("query") or "?"
                        except Exception:
                            ip = r.text.strip()[:20]
                        session.record_health_success(ip)
                        self._save_state()
                        logger.debug(
                            f"Proxy OK exit_ip={ip} session={session.session_id}"
                        )
                        return True
            except Exception as e:
                logger.debug(f"Health check {check_url} failed: {e}")
                continue

        session.record_health_failure()
        self._save_state()
        logger.warning(f"Proxy health check failed session={session.session_id}")
        return False

    async def health_check_all(self, concurrency: int = 5) -> dict[str, bool]:
        """全セッションのヘルスチェックを並列実行。結果を {session_id: bool} で返す。"""
        import asyncio

        sem = asyncio.Semaphore(concurrency)
        results: dict[str, bool] = {}

        async def _check(sid: str):
            async with sem:
                sess = self._session_obj(sid)
                ok = await self.health_check(sess)
                results[sid] = ok

        await asyncio.gather(*[_check(sid) for sid in list(self._sessions.keys())])
        ok_count = sum(1 for v in results.values() if v)
        logger.info(f"Proxy health: {ok_count}/{len(results)} healthy")
        return results

    # ──────────────────────────────────────────
    # ユーティリティ
    # ──────────────────────────────────────────

    def release(self, account_key: str):
        self._account_map.pop(account_key, None)

    def status_report(self) -> dict:
        total = len(self._sessions)
        cooling = sum(
            1 for d in self._sessions.values()
            if ProxySession(d, self.config).is_cooling()
        )
        circuit_open = sum(
            1 for d in self._sessions.values()
            if ProxySession(d, self.config).is_circuit_open()
        )
        ready = total - cooling - circuit_open
        return {
            "total_sessions": total,
            "cooling": cooling,
            "circuit_open": circuit_open,
            "ready": max(ready, 0),
        }
