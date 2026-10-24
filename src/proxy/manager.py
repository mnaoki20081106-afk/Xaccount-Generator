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
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

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
        # 期限切れ → 自動リセット
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

    def mark_ip_lost(self):
        """killswitch(410) 等でスティッキー IP を失った。次に観測する別 IP は想定外の変化として数える。"""
        self._d["ip_lost"] = True

    # ──────────────────────────────────────────

    # ── 出口 IP の履歴（サインアップ→ウォームアップの IP 一致を追跡する） ──
    @property
    def ip_history(self) -> list[dict]:
        return self._d.setdefault("ip_history", [])

    @property
    def signup_ip(self) -> Optional[str]:
        h = self.ip_history
        return h[0]["ip"] if h else None

    @property
    def current_ip(self) -> Optional[str]:
        h = self.ip_history
        return h[-1]["ip"] if h else None

    @property
    def unplanned_ip_changes(self) -> int:
        return self._d.get("unplanned_ip_changes", 0)

    @property
    def expected_expiry(self) -> Optional[datetime]:
        """スティッキー lifetime の満了予定。これ以降の IP 変化は想定内（planned）扱い。"""
        st = _parse_dt(self._d.get("sticky_started_at"))
        return st + timedelta(minutes=self._config.session_lifetime_minutes) if st else None

    def observe_ip(self, ip: str) -> str:
        """出口 IP を観測して履歴に反映。戻り値: baseline / same / planned_change / unplanned_change。"""
        now = datetime.now(timezone.utc)
        hist = self.ip_history
        if not hist:
            hist.append({"ip": ip, "at": now.isoformat(), "planned": False})
            self._d.setdefault("sticky_started_at", now.isoformat())
            return "baseline"
        if hist[-1]["ip"] == ip:
            return "same"
        exp = self.expected_expiry
        planned = bool(exp and now >= exp - timedelta(minutes=5))
        self._d.pop("ip_lost", None)
        hist.append({"ip": ip, "at": now.isoformat(), "planned": planned})
        del hist[:-20]
        if planned:
            self._d["sticky_started_at"] = now.isoformat()
            return "planned_change"
        self._d["unplanned_ip_changes"] = self.unplanned_ip_changes + 1
        return "unplanned_change"

    @property
    def url(self) -> str:
        c = self._config
        sid = self.session_id
        if c.provider.lower() == "iproyal":
            # IPRoyal は username ではなく password の末尾に `_key-value` を足す方式。
            # session は英数字ちょうど8文字、lifetime は 1s〜7d。killswitch で
            # IP を失った時に黙って別 IP へ切り替わらず HTTP 410 を返させる。
            pw = c.password
            if c.country:
                pw += f"_country-{c.country}"
            pw += f"_session-{sid}_lifetime-{c.session_lifetime_minutes}m"
            if c.killswitch:
                pw += "_killswitch-1"
            user = c.username
        else:
            user = f"{c.username}-session-{sid}"
            if c.country:
                user += f"-country-{c.country}"
            pw = c.password
        return f"http://{quote(user, safe='')}:{quote(pw, safe='')}@{c.host}:{c.port}"

    def is_cooling(self) -> bool:
        cu = self.cooldown_until
        return bool(cu and datetime.now(timezone.utc) < cu)

    def is_ready_for_signup(self, max_per_ip: int) -> bool:
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
        """状態ファイルが別プロセス（run デーモンなど）に更新されていたら読み直す。
        APIサーバーとデーモンは別プロセスで同じファイルを共有するため、操作や
        表示の前に呼んで古い状態で上書き・表示するのを避ける。"""
        try:
            mtime = STATE_FILE.stat().st_mtime_ns
        except OSError:
            return
        if mtime != getattr(self, "_state_mtime", None):
            self._load_state()

    def _save_state(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(
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
        os.replace(tmp, STATE_FILE)  # 別プロセスが半端な JSON を読まないよう原子的に置換
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
            "ip_history": [],
            "unplanned_ip_changes": 0,
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
        self.refresh()
        # Reuse the creation session — same IP = consistent fingerprint chain
        sid = self._account_map.get(account_key)
        if sid and sid in self._sessions:
            return ProxySession(self._sessions[sid], self.config)

        # Fallback: warmup-dedicated session
        wu_sid = self._warmup_map.get(account_key)
        if not wu_sid:
            wu_sid = hashlib.md5(f"wu:{account_key}".encode()).hexdigest()[:8]
            self._warmup_map[account_key] = wu_sid
        if wu_sid not in self._sessions:
            self._sessions[wu_sid] = {
                "session_id": wu_sid,
                "account_count": 1,
                "last_signup_at": None,
                "cooldown_until": None,
                "consecutive_failures": 0,
                "circuit_open_until": None,
                "last_exit_ip": "",
                "ip_history": [],
                "unplanned_ip_changes": 0,
            }
        self._save_state()
        return ProxySession(self._sessions[wu_sid], self.config)

    def proxy_for_account(self, account) -> ProxySession:
        """垢が生成された時のセッション（DB の proxy_id）を最優先で返す。
        username をキーにした対応表は、サインアップ中に username が衝突リトライで
        変わると外れて、ウォームアップが別 IP になる。proxy_id なら確実に同じ IP。"""
        self.refresh()
        sid = getattr(account, "proxy_id", None)
        if sid and sid in self._sessions:
            self._account_map[account.username] = sid
            self._save_state()
            return ProxySession(self._sessions[sid], self.config)
        return self.get_warmup_proxy(account.username)

    async def verify_account_ip(self, account) -> dict:
        """ウォームアップ前に、垢のセッションが生成時と同じ出口 IP かを実測する。
        戻り値 ok=False のとき呼び出し側はその垢のアクションを見送ること。
        kind: baseline / same / planned_change / unplanned_change / unreachable"""
        sess = self.proxy_for_account(account)
        info = {"session_id": sess.session_id, "signup_ip": sess.signup_ip}
        if sess.is_circuit_open():
            return {**info, "ok": False, "kind": "unreachable", "ip": None}
        if not await self.health_check(sess):
            return {**info, "ok": False, "kind": "unreachable", "ip": None}
        sess = self._session_obj(sess.session_id)
        kind = sess._d.get("last_ip_kind", "same")
        return {
            **info, "ok": kind != "unplanned_change", "kind": kind, "ip": sess.current_ip,
            "unplanned_changes": sess.unplanned_ip_changes,
        }

    def ip_report(self, session_id: str) -> Optional[dict]:
        self.refresh()
        d = self._sessions.get(session_id)
        if d is None:
            return None
        s = ProxySession(d, self.config)
        return {
            "session_id": session_id,
            "signup_ip": s.signup_ip,
            "current_ip": s.current_ip,
            "unplanned_changes": s.unplanned_ip_changes,
            "planned_changes": sum(1 for h in s.ip_history if h.get("planned")),
            "history": list(s.ip_history),
        }

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
                    if r.status_code == 410:
                        session.mark_ip_lost()
                    if r.status_code == 200:
                        try:
                            data = r.json()
                            ip = data.get("ip") or data.get("query") or "?"
                        except Exception:
                            ip = r.text.strip()[:20]
                        session.record_health_success(ip)
                        if ip and ip != "?":
                            kind = session.observe_ip(ip)
                            session._d["last_ip_kind"] = kind
                            if kind == "unplanned_change":
                                logger.warning(
                                    f"Proxy session={session.session_id} 出口IPが想定外に変化 "
                                    f"→ {ip} (履歴 {len(session.ip_history)})"
                                )
                        self._save_state()
                        logger.debug(
                            f"Proxy OK exit_ip={ip} session={session.session_id}"
                        )
                        return True
            except Exception as e:
                if "410" in str(e):
                    session.mark_ip_lost()
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

    def release_failed_signup(self, account_key: str):
        """サインアップに失敗した時、そのIPに割り当てた枠を返す。
        枠を返さないと、失敗するたびに max_accounts_per_ip を消費し、実際には垢が
        できていないのに3週間のクーリングに入ってしまう（IPの無駄遣い＝単価上昇）。"""
        self.refresh()
        sid = self._account_map.pop(account_key, None)
        d = self._sessions.get(sid) if sid else None
        if d is not None:
            d["account_count"] = max(0, d.get("account_count", 0) - 1)
            if d["account_count"] < self.config.max_accounts_per_ip:
                d["cooldown_until"] = None
        self._save_state()

    # ──────────────────────────────────────────
    # 管理画面向け（読み取り・運用操作）
    # ──────────────────────────────────────────

    def sessions_report(self) -> list[dict]:
        """全セッションの状態一覧。状態の自動リセットなど副作用は起こさない。"""
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
            elif sess.account_count >= max_per_ip:
                state = "full"
            else:
                state = "ready"
            rows.append({
                "session_id": sid,
                "state": state,
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
        """サーキットブレーカー／クーリングを解除する。cooldown=Trueは
        3週間の再利用待ちを飛ばして、割当数も0に戻す（運用者が明示した場合のみ）。"""
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
        ready = total - cooling - circuit_open
        return {
            "total_sessions": total,
            "cooling": cooling,
            "circuit_open": circuit_open,
            "ready": max(ready, 0),
        }
