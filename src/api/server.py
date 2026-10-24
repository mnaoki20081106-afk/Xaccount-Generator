"""
FastAPI HTTP server for Xaccount-Generator.

All routes under /api/generator/ are protected by HMAC-SHA256 request signing.
The shared secret is read from the GENERATOR_BRIDGE_SECRET environment variable
(min 32 chars). Every inbound request must carry:

    X-Generator-Timestamp  — Unix ms (max 5-minute skew)
    X-Generator-Nonce      — 16–128 URL-safe chars (replay-protected)
    X-Generator-Signature  — lowercase hex HMAC-SHA256 of canonical string

Canonical string:
    <timestamp>\n<nonce>\n<METHOD>\n<path+query>\n<body>
"""
import asyncio
import hashlib
import hmac
import os
import platform
import re
import shutil
import sys
import json
import time
from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import costs
from ..config import load_config, AppConfig
from ..db import Database
from ..models import AccountStatus
from ..proxy.manager import ProxyManager
from ..observers import ObserverPool

_MAX_SKEW_MS = 5 * 60_000
_NONCE_LIFETIME_MS = _MAX_SKEW_MS

_seen_nonces: dict[str, int] = {}
_nonce_lock = asyncio.Lock()


def _purge_old_nonces(now_ms: int) -> None:
    cutoff = now_ms - _NONCE_LIFETIME_MS
    stale = [k for k, ts in _seen_nonces.items() if ts < cutoff]
    for k in stale:
        del _seen_nonces[k]


async def _verify_request(request: Request, body: bytes) -> None:
    secret = os.environ.get("GENERATOR_BRIDGE_SECRET", "").strip()
    if len(secret) < 32:
        raise HTTPException(503, detail="GENERATOR_BRIDGE_SECRET_NOT_CONFIGURED")

    timestamp_str = request.headers.get("x-generator-timestamp", "")
    nonce = request.headers.get("x-generator-nonce", "")
    signature = request.headers.get("x-generator-signature", "")

    if not timestamp_str.isdigit() or len(timestamp_str) < 10:
        raise HTTPException(401, detail="INVALID_TIMESTAMP")

    now_ms = int(time.time() * 1000)
    timestamp_ms = int(timestamp_str)
    if abs(now_ms - timestamp_ms) > _MAX_SKEW_MS:
        raise HTTPException(401, detail="STALE_TIMESTAMP")

    import re
    if not re.fullmatch(r"[A-Za-z0-9_\-]{16,128}", nonce):
        raise HTTPException(401, detail="INVALID_NONCE")

    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise HTTPException(401, detail="INVALID_SIGNATURE")

    path_and_query = str(request.url.path)
    if request.url.query:
        path_and_query += "?" + request.url.query
    canonical = (
        timestamp_str + "\n"
        + nonce + "\n"
        + request.method.upper() + "\n"
        + path_and_query + "\n"
        + body.decode("utf-8", errors="replace")
    )
    expected = hmac.HMAC(
        secret.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(401, detail="INVALID_SIGNATURE")

    async with _nonce_lock:
        _purge_old_nonces(now_ms)
        if nonce in _seen_nonces:
            raise HTTPException(401, detail="REPLAYED_NONCE")
        _seen_nonces[nonce] = now_ms


class _AppState:
    db: Database
    config: AppConfig
    proxy_manager: ProxyManager
    _health_task: Optional[asyncio.Task] = None
    _health_started_at: Optional[float] = None
    _health_finished_at: Optional[float] = None
    _health_result: Optional[dict] = None
    _orchestrator_task: Optional[asyncio.Task] = None
    _orchestrator_running: bool = False
    _orchestrator_target: int = 0
    _orchestrator_started_at: Optional[float] = None


_state = _AppState()
_started_at = time.time()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config_path = os.environ.get("GENERATOR_CONFIG", "config.yaml")
    _state.config = load_config(config_path)
    _state.db = Database(_state.config.db.path)
    await _state.db.connect()
    # オーケストレーターと共有する唯一のProxyManager（状態ファイルの上書き競合を防ぐ）
    _state.proxy_manager = ProxyManager(_state.config.proxy)
    _state.observers = ObserverPool(_state.db, _state.proxy_manager, _state.config.twofa.encryption_key)
    yield
    await _state.db.close()
    if _state._orchestrator_task and not _state._orchestrator_task.done():
        _state._orchestrator_task.cancel()
        try:
            await _state._orchestrator_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Xaccount-Generator API", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _account_to_dict(a) -> dict:
    return {
        "id": a.id,
        "username": a.username,
        "status": a.status.value,
        "shadow_score": a.shadow_score,
        "proxy_id": a.proxy_id,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "last_action_at": a.last_action_at.isoformat() if a.last_action_at else None,
        "sold_at": a.sold_at.isoformat() if a.sold_at else None,
        "notes": a.notes,
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/generator/status")
async def get_status(request: Request):
    body = await request.body()
    await _verify_request(request, body)

    all_accounts = await _state.db.list_accounts(limit=10000)
    counts: dict[str, int] = {}
    for a in all_accounts:
        counts[a.status.value] = counts.get(a.status.value, 0) + 1

    return {
        "total": len(all_accounts),
        "by_status": counts,
        "orchestrator_running": _state._orchestrator_running,
        "orchestrator_target": _state._orchestrator_target,
        "orchestrator_started_at": _state._orchestrator_started_at,
    }


@app.get("/api/generator/accounts")
async def list_accounts(
    request: Request,
    status: Optional[str] = None,
    limit: int = 100,
):
    body = await request.body()
    await _verify_request(request, body)

    status_enum: Optional[AccountStatus] = None
    if status:
        try:
            status_enum = AccountStatus(status)
        except ValueError:
            raise HTTPException(400, detail="INVALID_STATUS")

    accounts = await _state.db.list_accounts(status=status_enum, limit=min(limit, 500))
    return {"accounts": [_account_to_dict(a) for a in accounts]}


@app.get("/api/generator/inventory")
async def get_inventory(request: Request):
    body = await request.body()
    await _verify_request(request, body)

    all_accounts = await _state.db.list_accounts(limit=10000)
    counts: dict[str, int] = {}
    for a in all_accounts:
        counts[a.status.value] = counts.get(a.status.value, 0) + 1

    return {
        "total": len(all_accounts),
        "clean": counts.get("clean", 0),
        "warming": counts.get("warming", 0),
        "created": counts.get("created", 0),
        "soft_shadow": counts.get("soft_shadow", 0),
        "hard_shadow": counts.get("hard_shadow", 0),
        "locked": counts.get("locked", 0),
        "suspended": counts.get("suspended", 0),
        "sold": counts.get("sold", 0),
    }


class PopRequest(BaseModel):
    count: int = 1
    min_warmup_days: int = 0
    allow_soft_shadow: bool = False


@app.post("/api/generator/pop")
async def pop_accounts(request: Request, body_model: PopRequest):
    raw_body = await request.body()
    await _verify_request(request, raw_body)

    if body_model.count < 1 or body_model.count > 200:
        raise HTTPException(400, detail="INVALID_COUNT")

    candidates = await _state.db.list_accounts(status=AccountStatus.CLEAN, limit=1000)
    if body_model.allow_soft_shadow:
        soft = await _state.db.list_accounts(status=AccountStatus.SOFT_SHADOW, limit=500)
        candidates = candidates + soft

    if body_model.min_warmup_days > 0:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=body_model.min_warmup_days)
        candidates = [
            a for a in candidates
            if a.created_at and (
                a.created_at if a.created_at.tzinfo else a.created_at.replace(tzinfo=timezone.utc)
            ) <= cutoff
        ]

    if _state.config.warmup.require_graduation:
        graduated = await _state.db.graduated_account_ids()
        candidates = [a for a in candidates if a.id in graduated]

    selected = candidates[: body_model.count]
    if not selected:
        raise HTTPException(409, detail="OUT_OF_STOCK")

    results = []
    for account in selected:
        await _state.db.update_status(account.id, AccountStatus.SOLD)
        await _state.db.log_action(account.id, "sold", "popped via API")
        results.append({
            "id": account.id,
            "username": account.username,
            "password": account.password,
            "email": account.email,
            "phone": account.phone,
            "auth_token": account.auth_token,
            "ct0": account.ct0,
            "twofa_secret": account.twofa_secret,
            "shadow_score": account.shadow_score,
            "created_at": account.created_at.isoformat() if account.created_at else None,
        })

    return {"ok": True, "popped": len(results), "accounts": results}


class ObserverRequest(BaseModel):
    label: str = ""
    auth_token: str
    ct0: str


@app.get("/api/generator/observers")
async def list_observers(request: Request):
    await _verify_request(request, await request.body())
    return {"observers": await _state.observers.list()}


@app.post("/api/generator/observers")
async def add_observer(request: Request, body_model: ObserverRequest):
    await _verify_request(request, await request.body())
    try:
        return {"ok": True, "observer": await _state.observers.add(
            body_model.label, body_model.auth_token, body_model.ct0)}
    except ValueError as e:
        raise HTTPException(400, detail=str(e))


@app.post("/api/generator/observers/{observer_id}/verify")
async def verify_observer(request: Request, observer_id: int):
    await _verify_request(request, await request.body())
    try:
        return {"ok": True, "observer": await _state.observers.verify(observer_id)}
    except KeyError:
        raise HTTPException(404, detail="OBSERVER_NOT_FOUND")


@app.delete("/api/generator/observers/{observer_id}")
async def delete_observer(request: Request, observer_id: int):
    await _verify_request(request, await request.body())
    if not await _state.observers.remove(observer_id):
        raise HTTPException(404, detail="OBSERVER_NOT_FOUND")
    return {"ok": True}


@app.get("/api/generator/graduation")
async def graduation_overview(request: Request):
    body = await request.body()
    await _verify_request(request, body)

    accounts = await _state.db.list_accounts(limit=100000)
    out = []
    for a in accounts:
        g = await _state.db.get_graduation(a.id)
        if not g:
            continue
        try:
            report = json.loads(g["report"]) if g.get("report") else None
        except ValueError:
            report = None
        out.append({
            "id": a.id, "username": a.username, "passed": bool(g["passed"]),
            "attempts": g["attempts"], "checked_at": g["checked_at"], "report": report,
        })
    return {"graduated": sum(1 for o in out if o["passed"]), "accounts": out}


@app.get("/api/generator/proxy/ip/{session_id}")
async def proxy_ip_history(request: Request, session_id: str):
    body = await request.body()
    await _verify_request(request, body)
    rep = _state.proxy_manager.ip_report(session_id)
    if rep is None:
        raise HTTPException(404, detail="SESSION_NOT_FOUND")
    return rep


class GenerateRequest(BaseModel):
    count: int = 1
    concurrency: int = 2


@app.post("/api/generator/generate")
async def start_generation(request: Request, body_model: GenerateRequest):
    raw_body = await request.body()
    await _verify_request(request, raw_body)

    if body_model.count < 1 or body_model.count > 100:
        raise HTTPException(400, detail="INVALID_COUNT")
    if body_model.concurrency < 1 or body_model.concurrency > 10:
        raise HTTPException(400, detail="INVALID_CONCURRENCY")

    async def _run():
        from ..orchestrator import Orchestrator
        _state._orchestrator_running = True
        _state._orchestrator_target = body_model.count
        _state._orchestrator_started_at = time.time()
        orc = Orchestrator(_state.config, proxy_manager=_state.proxy_manager)
        try:
            # start() もtryの内側に置く。停止要求(cancel)が起動中に届いても
            # 実行中フラグを必ず戻すため。
            await orc.start()
            await orc.generate_accounts(body_model.count, concurrency=body_model.concurrency)
        finally:
            try:
                await orc.stop()
            finally:
                _state._orchestrator_running = False

    if _state._orchestrator_task and not _state._orchestrator_task.done():
        raise HTTPException(409, detail="ORCHESTRATOR_ALREADY_RUNNING")

    _state._orchestrator_task = asyncio.create_task(_run())
    return {"ok": True, "count": body_model.count, "concurrency": body_model.concurrency}


@app.get("/api/generator/config")
async def get_config(request: Request):
    body = await request.body()
    await _verify_request(request, body)

    cfg = _state.config
    return {
        "proxy": {
            "provider": cfg.proxy.provider,
            "host": cfg.proxy.host,
            "port": cfg.proxy.port,
            "country": cfg.proxy.country,
            "sticky_ttl_minutes": cfg.proxy.sticky_ttl_minutes,
            "max_accounts_per_ip": cfg.proxy.max_accounts_per_ip,
        },
        "sms": {
            "provider": cfg.sms.provider,
            "preferred_countries": cfg.sms.preferred_countries,
        },
        "captcha": {
            "provider": cfg.captcha.provider,
            "provider_fallback": cfg.captcha.provider_fallback,
        },
        "factory": {
            "max_per_hour": cfg.factory.max_per_hour,
            "use_phone": cfg.factory.use_phone,
            "setup_profile": cfg.factory.setup_profile,
        },
        "warmup": {
            "min_days_before_sale": cfg.warmup.min_days_before_sale,
            "actions_per_day_soft": cfg.warmup.actions_per_day_soft,
        },
        "db": {"path": cfg.db.path},
    }


# ─────────────────────────────────────────────
# プロキシ管理
# ─────────────────────────────────────────────

_SESSION_ID = re.compile(r"[A-Za-z0-9_\-]{1,64}")


def _health_state() -> dict:
    running = bool(_state._health_task and not _state._health_task.done())
    return {
        "running": running,
        "started_at": _state._health_started_at,
        "finished_at": _state._health_finished_at,
        "result": _state._health_result,
    }


@app.get("/api/generator/proxy")
async def get_proxy(request: Request):
    body = await request.body()
    await _verify_request(request, body)

    cfg = _state.config.proxy
    return {
        "config": {
            "provider": cfg.provider,
            "host": cfg.host,
            "port": cfg.port,
            "country": cfg.country,
            "sticky_ttl_minutes": cfg.sticky_ttl_minutes,
            "max_accounts_per_ip": cfg.max_accounts_per_ip,
        },
        "summary": _state.proxy_manager.status_report(),
        "sessions": _state.proxy_manager.sessions_report(),
        "health_check": _health_state(),
    }


@app.post("/api/generator/proxy/health-check")
async def run_proxy_health_check(request: Request):
    raw_body = await request.body()
    await _verify_request(request, raw_body)

    if _state._health_task and not _state._health_task.done():
        raise HTTPException(409, detail="HEALTH_CHECK_ALREADY_RUNNING")

    async def _run():
        try:
            results = await _state.proxy_manager.health_check_all()
            ok = sum(1 for v in results.values() if v)
            _state._health_result = {"checked": len(results), "healthy": ok, "unhealthy": len(results) - ok}
        except Exception as exc:  # 結果として画面に出す。サーバーは落とさない
            _state._health_result = {"error": str(exc)[:200]}
        finally:
            _state._health_finished_at = time.time()

    _state._health_started_at = time.time()
    _state._health_finished_at = None
    _state._health_result = None
    _state._health_task = asyncio.create_task(_run())
    return {"ok": True}


class ProxyResetRequest(BaseModel):
    circuit: bool = True
    cooldown: bool = False


@app.post("/api/generator/proxy/sessions/{session_id}/reset")
async def reset_proxy_session(session_id: str, request: Request):
    raw_body = await request.body()
    await _verify_request(request, raw_body)

    # Parsed after the signature check so unsigned callers always get 401.
    try:
        body_model = ProxyResetRequest.model_validate_json(raw_body or b"{}")
    except ValueError:
        raise HTTPException(400, detail="INVALID_BODY")

    if not _SESSION_ID.fullmatch(session_id):
        raise HTTPException(400, detail="INVALID_SESSION_ID")
    if not (body_model.circuit or body_model.cooldown):
        raise HTTPException(400, detail="NOTHING_TO_RESET")
    if not _state.proxy_manager.reset_session(
        session_id, circuit=body_model.circuit, cooldown=body_model.cooldown
    ):
        raise HTTPException(404, detail="SESSION_NOT_FOUND")
    return {"ok": True}


@app.delete("/api/generator/proxy/sessions/{session_id}")
async def delete_proxy_session(session_id: str, request: Request):
    body = await request.body()
    await _verify_request(request, body)

    if not _SESSION_ID.fullmatch(session_id):
        raise HTTPException(400, detail="INVALID_SESSION_ID")
    outcome = _state.proxy_manager.remove_session(session_id)
    if outcome == "not_found":
        raise HTTPException(404, detail="SESSION_NOT_FOUND")
    if outcome == "in_use":
        raise HTTPException(409, detail="SESSION_IN_USE")
    return {"ok": True}


# ─────────────────────────────────────────────
# サーバー（VPS）状態
# ─────────────────────────────────────────────

def _read_meminfo() -> Optional[dict]:
    try:
        values: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    values[key] = int(rest.split()[0]) * 1024
        total = values.get("MemTotal")
        available = values.get("MemAvailable")
        if not total or available is None:
            return None
        return {"total": total, "available": available, "used": total - available}
    except (OSError, ValueError):
        return None


def _read_host_uptime() -> Optional[float]:
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _file_size(path: str) -> Optional[int]:
    try:
        return os.path.getsize(path)
    except OSError:
        return None


@app.get("/api/generator/server")
async def get_server_info(request: Request):
    body = await request.body()
    await _verify_request(request, body)

    data_dir = os.path.dirname(os.path.abspath(_state.config.db.path)) or "."
    try:
        usage = shutil.disk_usage(data_dir)
        disk = {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        disk = None
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = None

    return {
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "load_average": load,
        "memory": _read_meminfo(),
        "disk": disk,
        "host_uptime_seconds": _read_host_uptime(),
        "api_uptime_seconds": time.time() - _started_at,
        "db_size_bytes": _file_size(_state.config.db.path),
        "log_size_bytes": _file_size(_state.config.logging.file),
        "orchestrator_running": _state._orchestrator_running,
    }


# ─────────────────────────────────────────────
# コスト（垢の単価）・支払い台帳・VPS契約
# ─────────────────────────────────────────────

def _cost_error(exc: costs.CostError) -> HTTPException:
    return HTTPException(400, detail=str(exc))


def _contract_view(contract: Optional[dict]) -> dict:
    today = datetime.now(timezone.utc).date()
    if not contract:
        return {"configured": False, "label": "", "provider": "", "monthly_fee_jpy": 0,
                "billing_day": None, "started_at": None, "note": None, "next_due": None}
    return {
        "configured": True,
        "label": contract["label"],
        "provider": contract["provider"],
        "monthly_fee_jpy": contract["monthly_fee_jpy"],
        "billing_day": contract["billing_day"],
        "started_at": contract["started_at"],
        "note": contract["note"],
        "next_due": (
            d.isoformat() if (d := costs.next_due_date(contract["billing_day"], today)) else None
        ),
    }


async def _compute_costs() -> dict:
    now = datetime.now(timezone.utc)
    contract = await _state.db.get_vps_contract()
    monthly = float(contract["monthly_fee_jpy"]) if contract else 0.0
    contract_start = (
        costs.parse_date(contract["started_at"]) if contract and contract.get("started_at") else None
    )

    first_account = await _state.db.first_account_created_at()
    first_expense = await _state.db.first_expense_paid_at()
    candidates = []
    if first_account:
        candidates.append(datetime.fromisoformat(first_account))
    if first_expense:
        d = costs.parse_date(first_expense)
        candidates.append(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
    if contract_start:
        candidates.append(datetime(contract_start.year, contract_start.month, contract_start.day, tzinfo=timezone.utc))
    candidates = [c if c.tzinfo else c.replace(tzinfo=timezone.utc) for c in candidates]
    earliest = min(candidates) if candidates else None

    windows = []
    for spec in costs.WINDOWS:
        if spec.days is not None:
            since_dt = now - timedelta(days=spec.days)
            since_iso = since_dt.isoformat()
            since_date = since_dt.date().isoformat()
        else:
            since_iso = None
            since_date = None
        expenses = await _state.db.list_expenses(since=since_date, limit=100000)
        counts = await _state.db.account_status_counts(since_iso)
        windows.append(costs.compute_window(
            spec, now=now, expenses=expenses, status_counts=counts,
            monthly_fee_jpy=monthly, contract_start=contract_start, earliest=earliest,
        ))

    all_expenses = await _state.db.list_expenses(limit=100000)
    proxy_rows = [e for e in all_expenses if e["category"] == "proxy"]
    proxy_gb = sum(e["quantity"] for e in proxy_rows if e["quantity"] and (e["unit"] or "GB") == "GB")
    proxy_gb_cost = sum(e["amount_jpy"] for e in proxy_rows if e["quantity"] and (e["unit"] or "GB") == "GB")
    return {
        "generated_at": now.isoformat(),
        "windows": windows,
        "proxy": {
            "payments": len(proxy_rows),
            "total_paid_jpy": round(sum(e["amount_jpy"] for e in proxy_rows), 2),
            "total_gb": round(proxy_gb, 3),
            "avg_jpy_per_gb": round(proxy_gb_cost / proxy_gb, 2) if proxy_gb > 0 else None,
            "last_paid_at": proxy_rows[0]["paid_at"] if proxy_rows else None,
        },
        "vps": _contract_view(contract),
    }


@app.get("/api/generator/costs")
async def get_costs(request: Request):
    body = await request.body()
    await _verify_request(request, body)
    return await _compute_costs()


@app.get("/api/generator/expenses")
async def list_expenses(request: Request, limit: int = 200):
    body = await request.body()
    await _verify_request(request, body)
    return {"expenses": await _state.db.list_expenses(limit=max(1, min(limit, 1000)))}


class ExpenseRequest(BaseModel):
    category: str
    description: str = ""
    amount: float
    currency: str = "JPY"
    rate_to_jpy: Optional[float] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    paid_at: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/generator/expenses")
async def add_expense(request: Request):
    raw_body = await request.body()
    await _verify_request(request, raw_body)
    try:
        data = ExpenseRequest.model_validate_json(raw_body or b"{}")
    except ValueError:
        raise HTTPException(400, detail="INVALID_BODY")

    try:
        if data.category not in costs.CATEGORIES:
            raise costs.CostError("種類が不正です（" + " / ".join(costs.CATEGORIES) + "）")
        amount_jpy = costs.to_jpy(data.amount, data.currency, data.rate_to_jpy)
        paid = costs.parse_date(data.paid_at, "支払日") if data.paid_at else datetime.now(timezone.utc).date()
        if paid > datetime.now(timezone.utc).date() + timedelta(days=1):
            raise costs.CostError("支払日に未来の日付は指定できません")
        if data.quantity is not None and not (0 < data.quantity <= 1_000_000):
            raise costs.CostError("数量は0より大きい値で指定してください")
        description = data.description.strip()[:200]
        note = (data.note or "").strip()[:500] or None
    except costs.CostError as exc:
        raise _cost_error(exc)

    currency = (data.currency or "JPY").strip().upper()
    expense_id = await _state.db.add_expense(
        category=data.category, description=description, amount_jpy=amount_jpy,
        currency=currency, amount_original=data.amount,
        rate_to_jpy=1.0 if currency == "JPY" else float(data.rate_to_jpy),
        quantity=data.quantity, unit=(data.unit or ("GB" if data.category == "proxy" else None)),
        paid_at=paid.isoformat(), note=note,
    )
    return {"ok": True, "id": expense_id, "amount_jpy": amount_jpy}


@app.delete("/api/generator/expenses/{expense_id}")
async def delete_expense(expense_id: int, request: Request):
    body = await request.body()
    await _verify_request(request, body)
    if not await _state.db.delete_expense(expense_id):
        raise HTTPException(404, detail="EXPENSE_NOT_FOUND")
    return {"ok": True}


@app.get("/api/generator/vps-contract")
async def get_vps_contract(request: Request):
    body = await request.body()
    await _verify_request(request, body)
    return _contract_view(await _state.db.get_vps_contract())


class VpsContractRequest(BaseModel):
    label: str = ""
    provider: str = ""
    monthly_fee_jpy: float = 0
    billing_day: Optional[int] = None
    started_at: Optional[str] = None
    note: Optional[str] = None


@app.put("/api/generator/vps-contract")
async def save_vps_contract(request: Request):
    raw_body = await request.body()
    await _verify_request(request, raw_body)
    try:
        data = VpsContractRequest.model_validate_json(raw_body or b"{}")
    except ValueError:
        raise HTTPException(400, detail="INVALID_BODY")
    try:
        if not (0 <= data.monthly_fee_jpy <= 10_000_000):
            raise costs.CostError("月額は0〜1000万円で指定してください")
        if data.billing_day is not None and not (1 <= data.billing_day <= 31):
            raise costs.CostError("支払日は1〜31で指定してください")
        started = costs.parse_date(data.started_at, "契約開始日").isoformat() if data.started_at else None
    except costs.CostError as exc:
        raise _cost_error(exc)
    await _state.db.save_vps_contract(
        label=data.label.strip()[:100], provider=data.provider.strip()[:100],
        monthly_fee_jpy=data.monthly_fee_jpy, billing_day=data.billing_day,
        started_at=started, note=(data.note or "").strip()[:500] or None,
    )
    return _contract_view(await _state.db.get_vps_contract())


# ─────────────────────────────────────────────
# ログ閲覧・生成ジョブの停止
# ─────────────────────────────────────────────

_LOG_REDACTIONS = (
    # http://user:password@host → パスワードを隠す（プロキシURLなど）
    (re.compile(r"(://[^:@/\s]+:)[^@/\s]+@"), r"\1***@"),
    # key=value 形式の秘密情報
    (re.compile(r"(?i)\b(password|passwd|secret|token|auth_token|ct0|api[_-]?key|twofa_secret|2fa)\b(\s*[=:]\s*)[^\s,;|\"']+"), r"\1\2***"),
)
_MAX_LOG_READ = 512 * 1024


def _redact(line: str) -> str:
    for pattern, replacement in _LOG_REDACTIONS:
        line = pattern.sub(replacement, line)
    return line


def _tail_lines(path: str, lines: int) -> Optional[list[str]]:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - _MAX_LOG_READ))
            data = fh.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").splitlines()
    if size > _MAX_LOG_READ and text:
        text = text[1:]  # 先頭は途中で切れている可能性がある
    return text[-lines:]


@app.get("/api/generator/logs")
async def get_logs(request: Request, lines: int = 200, level: str = ""):
    body = await request.body()
    await _verify_request(request, body)

    lines = max(1, min(lines, 500))
    level = level.strip().upper()
    if level and level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise HTTPException(400, detail="INVALID_LEVEL")
    # 絞り込む場合は多めに読んでから絞る
    raw = _tail_lines(_state.config.logging.file, lines if not level else 500)
    if raw is None:
        return {"available": False, "lines": []}
    if level:
        marker = re.compile(r"\|\s*" + level + r"\s*\|")
        raw = [row for row in raw if marker.search(row)][-lines:]
    return {"available": True, "lines": [_redact(row)[:600] for row in raw]}


@app.post("/api/generator/generate/stop")
async def stop_generation(request: Request):
    raw_body = await request.body()
    await _verify_request(request, raw_body)
    task = _state._orchestrator_task
    if not task or task.done():
        raise HTTPException(409, detail="ORCHESTRATOR_NOT_RUNNING")
    task.cancel()
    return {"ok": True}
