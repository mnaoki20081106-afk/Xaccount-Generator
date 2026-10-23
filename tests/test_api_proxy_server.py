"""Proxy-session management and server-status endpoints of the HTTP API."""
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


def _sign(method: str, path: str, body: str = "") -> dict:
    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex
    canonical = "\n".join([timestamp, nonce, method, path, body])
    signature = hmac.new(SECRET.encode(), canonical.encode(), hashlib.sha256).hexdigest()
    return {
        "x-generator-timestamp": timestamp,
        "x-generator-nonce": nonce,
        "x-generator-signature": signature,
        "content-type": "application/json",
    }


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


@pytest.fixture
def api(tmp_path, monkeypatch):
    config = {
        "proxy": {
            "provider": "fixture", "host": "proxy.test", "port": 1000,
            "username": "user", "password": "secret-password", "max_accounts_per_ip": 3,
        },
        "twofa": {"encryption_key": "k" * 32},
        "db": {"path": str(tmp_path / "accounts.db")},
        "logging": {"file": str(tmp_path / "bot.log")},
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    state_file = tmp_path / "proxy_sessions.json"
    state_file.write_text(json.dumps({
        "sessions": {
            "ready01": {"session_id": "ready01", "account_count": 1, "last_signup_at": _iso(-timedelta(hours=5)),
                        "cooldown_until": None, "consecutive_failures": 0, "circuit_open_until": None,
                        "last_exit_ip": "203.0.113.5"},
            "cool001": {"session_id": "cool001", "account_count": 3, "last_signup_at": _iso(-timedelta(days=1)),
                        "cooldown_until": _iso(timedelta(days=20)), "consecutive_failures": 0,
                        "circuit_open_until": None, "last_exit_ip": ""},
            "open001": {"session_id": "open001", "account_count": 0, "last_signup_at": None,
                        "cooldown_until": None, "consecutive_failures": 4,
                        "circuit_open_until": _iso(timedelta(minutes=20)), "last_exit_ip": ""},
            "free001": {"session_id": "free001", "account_count": 0, "last_signup_at": None,
                        "cooldown_until": None, "consecutive_failures": 0, "circuit_open_until": None,
                        "last_exit_ip": ""},
        },
        "account_map": {"acct-a": "ready01", "acct-b": "cool001"},
        "warmup_map": {},
    }), encoding="utf-8")

    monkeypatch.setenv("GENERATOR_BRIDGE_SECRET", SECRET)
    monkeypatch.setenv("GENERATOR_CONFIG", str(config_path))
    import src.proxy.manager as manager_module
    monkeypatch.setattr(manager_module, "STATE_FILE", state_file)

    from src.api import server
    with TestClient(server.app) as client:
        def call(method: str, path: str, payload=None):
            body = json.dumps(payload) if payload is not None else ""
            return client.request(method, path, content=body or None, headers=_sign(method, path, body))
        yield call, server, state_file


def _sessions(call):
    data = call("GET", "/api/generator/proxy").json()
    return {row["session_id"]: row for row in data["sessions"]}, data


def test_requests_must_be_signed(api):
    call, server, _ = api
    client = TestClient(server.app)
    for method, path in [
        ("GET", "/api/generator/proxy"), ("POST", "/api/generator/proxy/health-check"),
        ("POST", "/api/generator/proxy/sessions/ready01/reset"),
        ("DELETE", "/api/generator/proxy/sessions/free001"), ("GET", "/api/generator/server"),
    ]:
        assert client.request(method, path).status_code == 401, path


def test_proxy_overview_reports_each_session_state_without_credentials(api):
    call, _, _ = api
    by_id, data = _sessions(call)
    assert by_id["ready01"]["state"] == "ready"
    assert by_id["ready01"]["last_exit_ip"] == "203.0.113.5"
    assert by_id["ready01"]["assigned_accounts"] == 1
    assert by_id["cool001"]["state"] == "cooling"
    assert by_id["open001"]["state"] == "circuit_open"
    assert by_id["free001"]["state"] == "ready"
    assert data["summary"]["total_sessions"] == 4
    assert data["config"]["host"] == "proxy.test"
    assert data["health_check"]["running"] is False
    assert "secret-password" not in json.dumps(data)


def test_reset_circuit_keeps_cooldown_unless_asked(api):
    call, _, state_file = api
    assert call("POST", "/api/generator/proxy/sessions/open001/reset", {"circuit": True}).status_code == 200
    by_id, _ = _sessions(call)
    assert by_id["open001"]["state"] == "ready"
    assert by_id["open001"]["consecutive_failures"] == 0

    # Circuit reset alone must not cut the 3-week cooldown short.
    assert call("POST", "/api/generator/proxy/sessions/cool001/reset", {"circuit": True}).status_code == 200
    by_id, _ = _sessions(call)
    assert by_id["cool001"]["state"] == "cooling"
    assert by_id["cool001"]["account_count"] == 3

    assert call("POST", "/api/generator/proxy/sessions/cool001/reset", {"circuit": False, "cooldown": True}).status_code == 200
    by_id, _ = _sessions(call)
    assert by_id["cool001"]["state"] == "ready"
    assert by_id["cool001"]["account_count"] == 0
    assert json.loads(state_file.read_text())["sessions"]["cool001"]["cooldown_until"] is None


def test_reset_validation(api):
    call, _, _ = api
    assert call("POST", "/api/generator/proxy/sessions/missing1/reset", {"circuit": True}).status_code == 404
    assert call("POST", "/api/generator/proxy/sessions/ready01/reset", {"circuit": False, "cooldown": False}).status_code == 400
    assert call("POST", "/api/generator/proxy/sessions/bad.id/reset", {"circuit": True}).status_code == 400
    assert call("POST", "/api/generator/proxy/sessions/" + "a" * 65 + "/reset", {"circuit": True}).status_code == 400
    assert call("POST", "/api/generator/proxy/sessions/ready01/reset", {"circuit": "maybe"}).status_code == 400


def test_only_unused_sessions_can_be_deleted(api):
    call, _, state_file = api
    assert call("DELETE", "/api/generator/proxy/sessions/ready01").status_code == 409, "has an assigned account"
    assert call("DELETE", "/api/generator/proxy/sessions/free001").status_code == 200
    assert call("DELETE", "/api/generator/proxy/sessions/free001").status_code == 404
    assert "free001" not in json.loads(state_file.read_text())["sessions"]
    assert "ready01" in json.loads(state_file.read_text())["sessions"]


def test_health_check_runs_in_background_and_reports_counts(api, monkeypatch):
    call, server, _ = api
    release = asyncio.Event()
    loop_holder = {}

    async def fake_health_check_all(self, concurrency: int = 5):
        loop_holder["loop"] = asyncio.get_running_loop()
        await release.wait()
        return {"ready01": True, "cool001": False, "open001": False, "free001": True}

    monkeypatch.setattr(type(server._state.proxy_manager), "health_check_all", fake_health_check_all)

    assert call("POST", "/api/generator/proxy/health-check").status_code == 200
    # Wait for the background task to start inside the app's event loop.
    deadline = time.time() + 5
    while "loop" not in loop_holder and time.time() < deadline:
        time.sleep(0.01)
    assert _sessions(call)[1]["health_check"]["running"] is True
    assert call("POST", "/api/generator/proxy/health-check").status_code == 409

    loop_holder["loop"].call_soon_threadsafe(release.set)
    deadline = time.time() + 5
    while time.time() < deadline:
        health = _sessions(call)[1]["health_check"]
        if not health["running"]:
            break
        time.sleep(0.02)
    assert health["running"] is False
    assert health["result"] == {"checked": 4, "healthy": 2, "unhealthy": 2}
    assert health["finished_at"] is not None


def test_server_info_reports_host_and_data_stats(api):
    call, _, _ = api
    info = call("GET", "/api/generator/server").json()
    assert info["hostname"]
    assert info["python"].count(".") >= 1
    assert info["cpu_count"] >= 1
    assert info["api_uptime_seconds"] >= 0
    assert info["disk"]["total"] > 0 and info["disk"]["free"] >= 0
    assert info["orchestrator_running"] is False
    assert "memory" in info and "load_average" in info
    for key in ("db_size_bytes", "log_size_bytes", "host_uptime_seconds"):
        assert key in info
