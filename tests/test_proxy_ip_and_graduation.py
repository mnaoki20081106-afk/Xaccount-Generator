import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import pytest

from src.actions.x_api import _missing_features, parse_search_results
from src.config import ProxyConfig, WarmupConfig
from src.db import Database
from src.detector import BanStatus, ShadowbanDetector
from src.observers import ObserverPool
from src.graduation import GraduationExaminer
from src.models import Account, AccountStatus
from src.proxy import manager as pm
from src.proxy.manager import ProxyManager, ProxySession


def _cfg(**kw):
    base = dict(provider="iproyal", host="geo.iproyal.com", port=12321, username="u", password="p@ss")
    return ProxyConfig(**{**base, **kw})


def test_iproyal_url_puts_session_in_password():
    d = {"session_id": "abcd1234"}
    u = urlparse(ProxySession(d, _cfg(country="jp")).url)
    assert u.username == "u"
    pw = unquote(u.password)
    assert pw == "p@ss_country-jp_session-abcd1234_lifetime-10080m_killswitch-1"


def test_generic_provider_keeps_username_style():
    u = urlparse(ProxySession({"session_id": "s1"}, _cfg(provider="other")).url)
    assert u.username == "u-session-s1" and unquote(u.password) == "p@ss"


def test_observe_ip_baseline_same_unplanned_planned():
    d = {"session_id": "abcd1234"}
    s = ProxySession(d, _cfg())
    assert s.observe_ip("1.1.1.1") == "baseline"
    assert s.observe_ip("1.1.1.1") == "same"
    assert s.observe_ip("2.2.2.2") == "unplanned_change"
    assert s.unplanned_ip_changes == 1
    d["sticky_started_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    assert s.observe_ip("3.3.3.3") == "planned_change"
    assert s.unplanned_ip_changes == 1 and s.signup_ip == "1.1.1.1"


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "STATE_FILE", tmp_path / "proxy.json")
    return ProxyManager(_cfg())


def test_proxy_for_account_uses_proxy_id_not_username(mgr):
    sess = mgr.get_proxy_for_account("first_name")
    acc = Account(username="renamed_after_collision", password="x", proxy_id=sess.session_id)
    assert mgr.proxy_for_account(acc).session_id == sess.session_id


def test_verify_account_ip_detects_change(mgr, monkeypatch):
    sess = mgr.get_proxy_for_account("a")
    acc = Account(username="a", password="x", proxy_id=sess.session_id)
    ips = iter(["1.1.1.1", "1.1.1.1", "9.9.9.9"])

    async def fake_health(session):
        session.record_health_success(next(ips))
        kind = session.observe_ip(session._d["last_exit_ip"])
        session._d["last_ip_kind"] = kind
        return True

    monkeypatch.setattr(mgr, "health_check", fake_health)

    async def run():
        return [await mgr.verify_account_ip(acc) for _ in range(3)]

    r = asyncio.run(run())
    assert [x["kind"] for x in r] == ["baseline", "same", "unplanned_change"]
    assert [x["ok"] for x in r] == [True, True, False]
    assert mgr.ip_report(sess.session_id)["unplanned_changes"] == 1


def test_parse_search_results_and_missing_features():
    body = {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [
        {"type": "TimelineAddEntries", "entries": [
            {"entryId": "tweet-111", "content": {"itemContent": {"tweet_results": {"result": {
                "rest_id": "111",
                "core": {"user_results": {"result": {"core": {"screen_name": "Foo"}}}},
                "legacy": {"full_text": "hello kkabc"}}}}}},
            {"entryId": "cursor-bottom-1", "content": {}},
        ]}]}}}}}
    r = parse_search_results(body)
    assert r == [{"id": "111", "text": "hello kkabc", "screen_name": "Foo"}]
    assert _missing_features({"errors": [{"message": "The following features cannot be null: a_b, c"}]}) == ["a_b", "c"]


def test_detector_unknown_without_observers():
    st = asyncio.run(ShadowbanDetector(None).check("foo"))
    assert st.unknown and not st.is_clean


# ── 卒業テスト / 観測者（X-Utility 方式） ─────────────────────────

class FakeClient:
    users: dict = {}        # screen_name -> user_by_screen_name result
    top: dict = {}          # query -> results
    suggest: list = []
    state = "ok"

    def __init__(self, acc, proxy_url):
        self.acc = acc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def verify_state(self):
        return FakeClient.state

    async def user_by_screen_name(self, sn):
        return FakeClient.users.get(sn, {"state": "not_found", "screen_name": sn, "name": sn, "tweet_count": None})

    async def search_timeline(self, q, product="Top", count=20):
        return FakeClient.top.get((q, product), [])

    async def typeahead_users(self, q):
        return FakeClient.suggest


class FakeProxyMgr:
    def __init__(self, kind="same", unplanned=0):
        self.kind, self.unplanned = kind, unplanned

    async def verify_account_ip(self, acc):
        return {"ok": True, "kind": self.kind, "ip": "1.1.1.1", "signup_ip": "1.1.1.1",
                "unplanned_changes": self.unplanned}

    def proxy_for_account(self, acc):
        class S:
            url = "http://x"
            unplanned_ip_changes = self.unplanned
        return S()

    def get_warmup_proxy(self, key):
        class S:
            url = "http://obs"
        return S()


@pytest.fixture
def env(tmp_path):
    FakeClient.users = {"target": {"state": "ok", "screen_name": "target", "name": "Tgt", "tweet_count": 12}}
    FakeClient.top = {("from:target", "Top"): [{"id": "1", "text": "hi", "screen_name": "target"}]}
    FakeClient.suggest = ["target"]
    FakeClient.state = "ok"

    async def build(mgr=None, observer=True):
        db = Database(str(tmp_path / "a.db"))
        await db.connect()
        old = datetime.now(timezone.utc) - timedelta(days=30)
        aid = await db.add_account(Account(username="target", password="x", auth_token="t", ct0="c",
                                           status=AccountStatus.CLEAN, proxy_id="s1", created_at=old))
        for _ in range(12):
            await db.log_action(aid, "tweets", "t")
        for d in range(10):
            await db.conn.execute("INSERT INTO action_log (account_id, action, detail, created_at) VALUES (?,?,?,?)",
                                  (aid, "likes", "1", (old + timedelta(days=d)).isoformat()))
        await db.conn.commit()
        pm = mgr or FakeProxyMgr()
        pool = ObserverPool(db, pm, "k" * 32, client_factory=FakeClient)
        if observer:
            await pool.add("obs1", "AUTH", "CT0")
        ex = GraduationExaminer(db, pm, WarmupConfig(), pool, client_factory=FakeClient)
        return db, ex, pool, await db.get_account_by_id(aid)

    return build


def test_observer_tokens_encrypted_and_hidden(env):
    async def run():
        db, ex, pool, acc = await env()
        rows = await db.list_observers()
        assert "AUTH" not in rows[0]["auth_token_enc"] and rows[0]["status"] == "ok"
        listed = await pool.list()
        assert "auth_token_enc" not in listed[0] and "auth_token" not in listed[0]
        obs = await pool.pick()
        assert (obs.auth_token, obs.ct0) == ("AUTH", "CT0")
        await db.close()
    asyncio.run(run())


def test_invalid_observer_is_not_picked(env):
    async def run():
        FakeClient.state = "unauthorized"
        db, ex, pool, acc = await env()
        assert (await pool.list())[0]["status"] == "invalid"
        assert await pool.pick() is None
        await db.close()
    asyncio.run(run())


def test_graduation_pass(env):
    async def run():
        db, ex, pool, acc = await env()
        r = await ex.examine(acc)
        assert r.stage == "passed", r.to_json()
        assert acc.id in await db.graduated_account_ids()
        await db.close()
    asyncio.run(run())


def test_graduation_fails_when_missing_from_top_search(env):
    async def run():
        FakeClient.top = {}
        db, ex, pool, acc = await env()
        r = await ex.examine(acc)
        assert r.stage == "failed"
        assert (await db.get_graduation(acc.id))["attempts"] == 1
        # 直後の再判定は間隔待ち（一時不調で連続不合格にしない）
        assert (await ex.examine(acc)).stage == "pending"
        await db.close()
    asyncio.run(run())


def test_graduation_fails_when_missing_from_suggestion(env):
    async def run():
        FakeClient.suggest = ["someone_else"]
        db, ex, pool, acc = await env()
        r = await ex.examine(acc)
        assert r.stage == "failed" and any(c.name == "search_suggestion" and c.status == "fail" for c in r.checks)
        await db.close()
    asyncio.run(run())


def test_graduation_pending_without_observer(env):
    async def run():
        db, ex, pool, acc = await env(observer=False)
        r = await ex.examine(acc)
        assert r.stage == "pending" and any(c.status == "unknown" for c in r.checks)
        assert acc.id not in await db.graduated_account_ids()
        await db.close()
    asyncio.run(run())


def test_graduation_blocks_on_unplanned_ip_change(env):
    async def run():
        db, ex, pool, acc = await env(FakeProxyMgr(unplanned=1))
        assert (await ex.examine(acc)).stage == "failed"
        await db.close()
    asyncio.run(run())


def test_not_ready_when_young(env):
    async def run():
        db, ex, pool, acc = await env()
        acc.created_at = datetime.now(timezone.utc)
        assert (await ex.examine(acc)).stage == "not_ready"
        await db.close()
    asyncio.run(run())


def test_detector_via_observer(env):
    async def run():
        db, ex, pool, acc = await env()
        det = ShadowbanDetector(None, pool, client_factory=FakeClient)
        assert (await det.check("target")).is_clean
        FakeClient.top = {}
        assert (await det.check("target")).search_ban
        await db.close()
    asyncio.run(run())


def test_failed_signup_returns_ip_slot(mgr):
    mgr.config.max_accounts_per_ip = 1
    s = mgr.get_proxy_for_account("a")  # 上限1 → 即クーリングに入る
    assert mgr._sessions[s.session_id]["cooldown_until"]
    mgr.release_failed_signup("a")
    d = mgr._sessions[s.session_id]
    assert d["account_count"] == 0 and d["cooldown_until"] is None
    assert "a" not in mgr._account_map
