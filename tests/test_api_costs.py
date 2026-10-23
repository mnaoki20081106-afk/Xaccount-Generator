"""Cost ledger, VPS contract, logs and stop endpoints of the HTTP API."""
import asyncio
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

SECRET = "x" * 40


def _sign(method, path, body=""):
    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex
    canonical = "\n".join([timestamp, nonce, method, path, body])
    signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {"x-generator-timestamp": timestamp, "x-generator-nonce": nonce,
            "x-generator-signature": signature, "content-type": "application/json"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    log_file = tmp_path / "bot.log"
    config = {
        "proxy": {"provider": "fixture", "host": "proxy.test", "port": 1000,
                  "username": "user", "password": "pw", "max_accounts_per_ip": 3},
        "twofa": {"encryption_key": "k" * 32},
        "db": {"path": str(tmp_path / "accounts.db")},
        "logging": {"file": str(log_file)},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    monkeypatch.setenv("GENERATOR_BRIDGE_SECRET", SECRET)
    monkeypatch.setenv("GENERATOR_CONFIG", str(config_path))
    import src.proxy.manager as manager_module
    monkeypatch.setattr(manager_module, "STATE_FILE", tmp_path / "proxy_sessions.json")

    from src.api import server
    with TestClient(server.app) as client:
        def call(method, path, payload=None):
            body = json.dumps(payload) if payload is not None else ""
            return client.request(method, path, content=body or None, headers=_sign(method, path, body))
        yield call, server, log_file


def _seed_accounts(server, counts):
    """counts: {status: n}; all created 'now'. Uses its own sqlite connection because the
    app's connection belongs to the TestClient's event loop."""
    import aiosqlite

    async def run():
        async with aiosqlite.connect(server._state.config.db.path) as conn:
            i = 0
            for status, n in counts.items():
                for _ in range(n):
                    i += 1
                    await conn.execute(
                        "INSERT INTO accounts (username,password,status,created_at) VALUES (?,?,?,?)",
                        (f"u{i}", "p", status, datetime.now(timezone.utc).isoformat()),
                    )
            await conn.commit()

    asyncio.run(run())


def test_every_new_route_requires_a_signature(api):
    call, server, _ = api
    client = TestClient(server.app)
    for method, path in [
        ("GET", "/api/generator/costs"), ("GET", "/api/generator/expenses"),
        ("POST", "/api/generator/expenses"), ("DELETE", "/api/generator/expenses/1"),
        ("GET", "/api/generator/vps-contract"), ("PUT", "/api/generator/vps-contract"),
        ("GET", "/api/generator/logs"), ("POST", "/api/generator/generate/stop"),
    ]:
        assert client.request(method, path).status_code == 401, (method, path)


def test_expense_ledger_converts_currency_and_validates(api):
    call, _, _ = api
    today = datetime.now(timezone.utc).date().isoformat()
    ok = call("POST", "/api/generator/expenses", {
        "category": "proxy", "description": "iproyal 10GB", "amount": 20, "currency": "usd",
        "rate_to_jpy": 150.5, "quantity": 10, "paid_at": today})
    assert ok.status_code == 200, ok.text
    assert ok.json()["amount_jpy"] == 3010

    rows = call("GET", "/api/generator/expenses").json()["expenses"]
    assert rows[0]["unit"] == "GB" and rows[0]["quantity"] == 10 and rows[0]["currency"] == "USD"

    bad = [
        {"category": "nope", "amount": 1},
        {"category": "proxy", "amount": 0},
        {"category": "proxy", "amount": 5, "currency": "USD"},
        {"category": "proxy", "amount": 5, "paid_at": "2999-01-01"},
        {"category": "proxy", "amount": 5, "paid_at": "not-a-date"},
        {"category": "proxy", "amount": 5, "quantity": -3},
    ]
    for payload in bad:
        assert call("POST", "/api/generator/expenses", payload).status_code == 400, payload

    assert call("DELETE", f"/api/generator/expenses/{rows[0]['id']}").status_code == 200
    assert call("DELETE", f"/api/generator/expenses/{rows[0]['id']}").status_code == 404
    assert call("GET", "/api/generator/expenses").json()["expenses"] == []


def test_costs_report_unit_price_from_ledger_vps_and_accounts(api):
    call, server, _ = api
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=45)).isoformat()
    call("POST", "/api/generator/expenses", {"category": "proxy", "amount": 3000, "quantity": 5, "paid_at": today.isoformat()})
    call("POST", "/api/generator/expenses", {"category": "sms", "amount": 1000, "paid_at": today.isoformat()})
    call("POST", "/api/generator/expenses", {"category": "proxy", "amount": 9000, "quantity": 15, "paid_at": old})
    _seed_accounts(server, {"clean": 2, "warming": 3, "hard_shadow": 1})

    data = call("GET", "/api/generator/costs").json()
    windows = {w["key"]: w for w in data["windows"]}

    # The 45-day-old payment is outside the 30-day window but inside "all".
    assert windows["30d"]["costs"]["proxy"] == 3000
    assert windows["all"]["costs"]["proxy"] == 12000
    assert windows["30d"]["costs"]["sms"] == 1000
    assert windows["30d"]["accounts"] == {"created": 6, "alive": 5, "ready": 2, "failed": 1}
    assert windows["30d"]["unit_cost"]["per_alive"] == 800  # (3000+1000)/5
    assert windows["30d"]["unit_cost"]["per_ready"] == 2000
    assert windows["all"]["unit_cost"]["per_alive"] == pytest.approx(13000 / 5, abs=0.01)

    assert data["proxy"]["payments"] == 2
    assert data["proxy"]["total_gb"] == 20
    assert data["proxy"]["avg_jpy_per_gb"] == 600  # 12000 / 20
    assert data["vps"]["configured"] is False


def test_vps_contract_feeds_the_unit_cost_and_reports_next_due_date(api):
    call, server, _ = api
    assert call("PUT", "/api/generator/vps-contract", {"monthly_fee_jpy": -1}).status_code == 400
    assert call("PUT", "/api/generator/vps-contract", {"monthly_fee_jpy": 3000, "billing_day": 32}).status_code == 400
    assert call("PUT", "/api/generator/vps-contract", {"monthly_fee_jpy": 3000, "started_at": "bad"}).status_code == 400

    started = (datetime.now(timezone.utc).date() - timedelta(days=10)).isoformat()
    saved = call("PUT", "/api/generator/vps-contract", {
        "label": "main", "provider": "ConoHa", "monthly_fee_jpy": 3000, "billing_day": 31,
        "started_at": started, "note": "n"})
    assert saved.status_code == 200, saved.text
    view = saved.json()
    assert view["configured"] is True and view["provider"] == "ConoHa" and view["next_due"]
    assert call("GET", "/api/generator/vps-contract").json()["monthly_fee_jpy"] == 3000

    _seed_accounts(server, {"clean": 1})
    data = call("GET", "/api/generator/costs").json()
    windows = {w["key"]: w for w in data["windows"]}
    # ~10 days of a 3000 yen/month contract, nothing else.
    assert 900 < windows["30d"]["costs"]["vps"] < 1100
    assert windows["30d"]["costs"]["total"] == windows["30d"]["costs"]["vps"]
    assert windows["30d"]["unit_cost"]["per_alive"] == windows["30d"]["costs"]["vps"]

    # Saving again updates the single row instead of adding another.
    call("PUT", "/api/generator/vps-contract", {"monthly_fee_jpy": 6000})
    assert call("GET", "/api/generator/vps-contract").json()["monthly_fee_jpy"] == 6000


def test_logs_are_tailed_filtered_and_secrets_are_masked(api):
    call, _, log_file = api
    assert call("GET", "/api/generator/logs").json() == {"available": False, "lines": []}
    log_file.write_text("\n".join([
        "2026-10-23 03:00:00.000 | INFO     | src.x:f:1 - started",
        "2026-10-23 03:00:01.000 | WARNING  | src.x:f:2 - proxy http://user:hunter2@geo.example:12321 failed",
        "2026-10-23 03:00:02.000 | ERROR    | src.x:f:3 - login auth_token=abcdef123 ct0: zzz999 password=letmein",
        "2026-10-23 03:00:03.000 | INFO     | src.x:f:4 - done",
    ]), encoding="utf-8")

    everything = call("GET", "/api/generator/logs?lines=10").json()
    assert everything["available"] is True and len(everything["lines"]) == 4
    joined = "\n".join(everything["lines"])
    for secret in ("hunter2", "abcdef123", "zzz999", "letmein"):
        assert secret not in joined
    assert "://user:***@geo.example" in joined

    assert len(call("GET", "/api/generator/logs?lines=2").json()["lines"]) == 2
    errors = call("GET", "/api/generator/logs?level=error").json()["lines"]
    assert len(errors) == 1 and "ERROR" in errors[0]
    assert call("GET", "/api/generator/logs?level=bogus").status_code == 400


def test_stop_cancels_a_running_job_and_clears_the_running_flag(api, monkeypatch):
    call, server, _ = api
    assert call("POST", "/api/generator/generate/stop").status_code == 409

    import sys
    import types
    events = {"started": False, "stopped": False}

    class FakeOrchestrator:
        def __init__(self, config, proxy_manager=None):
            self.proxy_manager = proxy_manager

        async def start(self):
            events["started"] = True

        async def generate_accounts(self, count, concurrency=1):
            await asyncio.sleep(3600)

        async def stop(self):
            events["stopped"] = True

    fake = types.ModuleType("src.orchestrator")
    fake.Orchestrator = FakeOrchestrator
    monkeypatch.setitem(sys.modules, "src.orchestrator", fake)

    assert call("POST", "/api/generator/generate", {"count": 1, "concurrency": 1}).status_code == 200

    def status():
        return call("GET", "/api/generator/status").json()

    deadline = time.time() + 5
    while not (status()["orchestrator_running"] and events["started"]) and time.time() < deadline:
        time.sleep(0.02)
    assert status()["orchestrator_running"] is True
    assert call("POST", "/api/generator/generate", {"count": 1, "concurrency": 1}).status_code == 409, "already running"

    assert call("POST", "/api/generator/generate/stop").status_code == 200
    deadline = time.time() + 5
    while (status()["orchestrator_running"] or not events["stopped"]) and time.time() < deadline:
        time.sleep(0.02)
    assert status()["orchestrator_running"] is False
    assert events["stopped"] is True
    assert call("POST", "/api/generator/generate/stop").status_code == 409


def test_proxy_state_changes_made_by_another_process_are_picked_up(api, tmp_path):
    call, server, _ = api
    manager = server._state.proxy_manager
    assert call("GET", "/api/generator/proxy").json()["sessions"] == []

    # A separate process (the `run` daemon) records a session in the shared state file.
    import src.proxy.manager as manager_module
    manager_module.STATE_FILE.write_text(json.dumps({
        "sessions": {"daemon01": {"session_id": "daemon01", "account_count": 2, "last_signup_at": None,
                                   "cooldown_until": None, "consecutive_failures": 0,
                                   "circuit_open_until": None, "last_exit_ip": "198.51.100.7"}},
        "account_map": {"a": "daemon01"}, "warmup_map": {}}), encoding="utf-8")
    sessions = call("GET", "/api/generator/proxy").json()["sessions"]
    assert [s["session_id"] for s in sessions] == ["daemon01"]
    assert sessions[0]["assigned_accounts"] == 1

    # And operations act on the fresh state, then persist it for the other process.
    assert call("DELETE", "/api/generator/proxy/sessions/daemon01").status_code == 409
    assert "daemon01" in json.loads(manager_module.STATE_FILE.read_text())["sessions"]
    assert manager is server._state.proxy_manager
