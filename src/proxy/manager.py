"""
住宅用プロキシ管理。
- 1 スティッキーセッション に最大 max_accounts_per_ip 垢まで割り当て
- 上限に達したら COOLDOWN_WEEKS 週間クーリング、その後リセットして再利用
- セッション状態は data/proxy_sessions.json に永続保存
- サーキットブレーカー: 連続失敗 CIRCUIT_THRESHOLD 回でセッション一時遮断
- warmup_only フラグ: ウォームアップ専用セッションはサインアップに使わない
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from ..config import ProxyConfig

COOLDOWN_WEEKS = 3
SIGNUP_INTERVAL_HOURS = 2
CIRCUIT_THRESHOLD = 4
CIRCUIT_RESET_MINUTES = 30

STATE_FILE = Path("data/proxy_sessions.json")

_CHECK_URLS = [
    "https://api.ipify.org?format=json",
    "https://api.my-ip.io/ip.json",
    "https://ip.seeip.org/jsonip",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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

    @property
    def warmup_only(self) -> bool:
        return bool(self._d.get("warmup_only", False))

    # ── サーキットブレーカー ──
    @property
    def consecutive_failures(self) -> int:
        return self._d.get("consecutive_failures", 0)

    @property
    def circuit_open_until(self) -> Optional[datetime]:
        return _parse_dt(self._d.get("circuit_open_until"))

    def is_circuit_open(self) -> bool:
        until = self.circuit_open_until
        if until and datetime.now(timezone.utc) < until:
            return True
        if until and datetime.now(timezone.utc) >= until:
            self._d["circuit_open_until"] = None
            self._d["consecutive_failures"] = 0
        return False

    def record_health_failure(self):
        fails = self.consecutive_failures + 1
        self._d["consecutive_failures"] = fails
        if fails >= CIRCUIT_THRESHOLD:
            until = datetime.now(timezone.utc) + timedelta(minutes=CIRCUIT_RESET_MINUTES)
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
        user = f"{c.username}-session-{self.session_id}"
        if c.country:
            user += f"-country-{c.country}"
        return f"http://{user}:{c.password}@{c.host}:{c.port}"

    def is_cooling(self) -> bool:
        cu = self.cooldown_until
        return bool(cu and datetime.now(timezone.utc) < cu)

    def is_ready_for_signup(self, max_per_ip: int) -> bool:
        if self.warmup_only:
            return False
        if self.is_cooling():
            return False
        if self.is_circuit_open():
            return False
        if self.account_count >= max_per_ip:
            return False
        last = self.last_signup_at
        if last:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if elapsed < SIGNUP_INTERVAL_HOURS:
                return False
        return True

    def record_signup(self, max_per_ip: int):
        self._d["account_count"] = self.account_count + 1
        self._d["last_signup_at"] = _now_iso()
        if self._d["account_count"] >= max_per_ip:
            until = datetime.now(timezone.utc) + timedelta(weeks=COOLDOWN_WEEKS)
            self._d["cooldown_until"] = until.isoformat()
            logger.info(
                f"Proxy session={self.session_id} 上限到達 → "
                f"クーリング {COOLDOWN_WEEKS}週間 (解除: {until.strftime('%Y-%m-%d')})"
            )

    def maybe_reset_cooldown(self):
        cu = self.cooldown_until
        if cu and datetime.now(timezone.utc) >= cu:
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
                self._state_mtime = STATE_FILE.stat().st_mtime_ns
                raw = json.loads(STATE_FILE.read_text())
                self._sessions = raw.get("sessions", {})
                self._account_map = raw.get("account_map", {})
                self._warmup_map = raw.get("warmup_map", {})
                logger.debug(f"Proxy state loaded: {len(self._sessions)} sessions")
            except Exception as e:
                logger.warning(f"proxy state load error: {e}")

    def refresh(self):
        """状態ファイルが別プロセスに更新されていたら読み直す。"""
        try:
            mtime = STATE_FILE.stat().st_mtime_ns
        except OSError:
            return
        if mtime != getattr(self, "_state_mtime", None):
            self._load_state()

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                {
                    "sessions": self._sessions,
                    "account_map": self._account_map,
                    "warmup_map": self._warmup_map,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        try:
            self._state_mtime = STATE_FILE.stat().st_mtime_ns
        except OSError:
            pass

    def _session_obj(self, session_id: str) -> ProxySession:
        return ProxySession(self._sessions[session_id], self.config)

    # ──────────────────────────────────────────
    # サインアップ用プロキシ取得
    # ──────────────────────────────────────────

    def get_proxy_for_account(self, account_key: str) -> ProxySession:
        self.refresh()
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
            if sess.is_ready_for_signup(max_per_ip):  # warmup_only 除外は is_ready_for_signup 内
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
            "warmup_only": False,
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
        """ウォームアップ用プロキシを返す。
        サインアップ時と同じスティッキーセッションを優先して再利用する。
        """
        self.refresh()
        # 生成時のセッションを再利用 — 同一IPで連続性を保つ
        sid = self._account_map.get(account_key)
        if sid and sid in self._sessions:
            return ProxySession(self._sessions[sid], self.config)

        # フォールバック: ウォームアップ専用セッション
        wu_sid = self._warmup_map.get(account_key)
        if not wu_sid:
            wu_sid = hashlib.md5(f"wu:{account_key}".encode()).hexdigest()[:8]
            self._warmup_map[account_key] = wu_sid
        if wu_sid not in self._sessions:
            self._sessions[wu_sid] = {
                "session_id": wu_sid,
                "account_count": 0,   # サインアップ用のカウンタに含めない
                "last_signup_at": None,
                "cooldown_until": None,
                "consecutive_failures": 0,
                "circuit_open_until": None,
                "last_exit_ip": "",
                "warmup_only": True,  # サインアップ候補から除外
            }
        self._save_state()
        return ProxySession(self._sessions[wu_sid], self.config)

    async def get_warmup_proxy_verified(
        self, account_key: str
    ) -> tuple[ProxySession, bool]:
        """
        ウォームアップ用プロキシを取得し、ヘルスチェックで出口IPを確認する。
        戻り値: (session, ip_consistent)
          ip_consistent=True  — 出口IPがサインアップ時と一致（または初回で比較対象なし）
          ip_consistent=False — IPが変化（スティッキーセッション期限切れ等）、続行はするがログ警告
        """
        sess = self.get_warmup_proxy(account_key)
        previous_ip = sess._d.get("last_exit_ip") or ""

        ok = await self.health_check(sess)
        if not ok:
            logger.warning(
                f"[{account_key}] warmup proxy health check failed session={sess.session_id}"
            )
            return sess, False

        current_ip = sess._d.get("last_exit_ip") or ""
        if previous_ip and current_ip and previous_ip != current_ip:
            logger.warning(
                f"[{account_key}] warmup IP changed: {previous_ip} → {current_ip} "
                f"(sticky session rotated — session={sess.session_id})"
            )
            return sess, False

        return sess, True

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
                            data = r.json()
                            ip = data.get("ip") or data.get("query") or "?"
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

    # ──────────────────────────────────────────
    # 管理画面向け（読み取り・運用操作）
    # ──────────────────────────────────────────

    def sessions_report(self) -> list[dict]:
        """全セッションの状態一覧。副作用なし。"""
        self.refresh()
        now = datetime.now(timezone.utc)
        max_per_ip = self.config.max_accounts_per_ip
        assigned: dict[str, int] = {}
        for sid in list(self._account_map.values()) + list(self._warmup_map.values()):
            assigned[sid] = assigned.get(sid, 0) + 1

        rows = []
        for sid, d in self._sessions.items():
            sess = ProxySession(d, self.config)
            cooldown_until = sess.cooldown_until
            circuit_until = sess.circuit_open_until
            cooling = bool(cooldown_until and now < cooldown_until)
            circuit_open = bool(circuit_until and now < circuit_until)
            if circuit_open:
                state = "circuit_open"
            elif cooling:
                state = "cooling"
            elif sess.warmup_only:
                state = "warmup_only"
            elif sess.account_count >= max_per_ip:
                state = "full"
            else:
                state = "ready"
            rows.append({
                "session_id": sid,
                "state": state,
                "warmup_only": sess.warmup_only,
                "account_count": sess.account_count,
                "max_accounts": max_per_ip,
                "assigned_accounts": assigned.get(sid, 0),
                "consecutive_failures": sess.consecutive_failures,
                "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
                "circuit_open_until": circuit_until.isoformat() if circuit_until else None,
                "last_signup_at": sess.last_signup_at.isoformat() if sess.last_signup_at else None,
                "last_exit_ip": d.get("last_exit_ip") or None,
            })
        rows.sort(key=lambda r: r["last_signup_at"] or "", reverse=True)
        return rows

    def reset_session(self, session_id: str, *, circuit: bool = True, cooldown: bool = False) -> bool:
        self.refresh()
        d = self._sessions.get(session_id)
        if d is None:
            return False
        if circuit:
            d["consecutive_failures"] = 0
            d["circuit_open_until"] = None
        if cooldown:
            d["cooldown_until"] = None
            d["account_count"] = 0
            d["last_signup_at"] = None
        self._save_state()
        logger.info(f"Proxy session={session_id} reset (circuit={circuit}, cooldown={cooldown})")
        return True

    def remove_session(self, session_id: str) -> str:
        """未使用のセッションだけ削除する。垢が紐付いているものは削除しない。"""
        self.refresh()
        if session_id not in self._sessions:
            return "not_found"
        in_use = (
            session_id in self._account_map.values()
            or session_id in self._warmup_map.values()
        )
        if in_use:
            return "in_use"
        del self._sessions[session_id]
        self._save_state()
        logger.info(f"Proxy session={session_id} removed")
        return "removed"

    def status_report(self) -> dict:
        self.refresh()
        total = len(self._sessions)
        cooling = sum(
            1 for d in self._sessions.values()
            if ProxySession(d, self.config).is_cooling()
        )
        circuit_open = sum(
            1 for d in self._sessions.values()
            if ProxySession(d, self.config).is_circuit_open()
        )
        warmup_only = sum(
            1 for d in self._sessions.values()
            if d.get("warmup_only")
        )
        ready = total - cooling - circuit_open - warmup_only
        return {
            "total_sessions": total,
            "cooling": cooling,
            "circuit_open": circuit_open,
            "warmup_only": warmup_only,
            "ready": max(ready, 0),
        }
