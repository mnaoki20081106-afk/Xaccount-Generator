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
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import load_config, AppConfig
from ..db import Database
from ..models import AccountStatus

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
    _orchestrator_task: Optional[asyncio.Task] = None
    _orchestrator_running: bool = False
    _orchestrator_target: int = 0
    _orchestrator_started_at: Optional[float] = None


_state = _AppState()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    config_path = os.environ.get("GENERATOR_CONFIG", "config.yaml")
    _state.config = load_config(config_path)
    _state.db = Database(_state.config.db.path)
    await _state.db.connect()
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
        orc = Orchestrator(_state.config)
        await orc.start()
        try:
            await orc.generate_accounts(body_model.count, concurrency=body_model.concurrency)
        finally:
            await orc.stop()
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
