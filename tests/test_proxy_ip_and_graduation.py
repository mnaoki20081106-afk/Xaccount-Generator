import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import pytest

from src.actions.x_api import _missing_features, parse_search_results
from src.config import ProxyConfig, WarmupConfig
from src.db import Database
from src.detector import BanStatus, ShadowbanDetector
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


def test_detector_unknown_when_no_source(monkeypatch):
    det = ShadowbanDetector(None)

    async def none(handle):
        return None

    monkeypatch.setattr(det, "_check_hisubscriptions", none)
    monkeypatch.setattr(det, "_check_shadowban_eu", none)
    st = asyncio.run(det.check("foo"))
    assert st.unknown and not st.is_clean


# ── 卒業テスト ─────────────────────────────────────────────────

class FakeClient:
    """search_results: {(query, product): [..]}"""
    posted: list = []
    search_results: dict = {}
    alive = True

    def __init__(self, acc, proxy_url):
        self.acc = acc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def verify(self):
        return self.alive

    async def post_tweet(self, text):
        FakeClient.posted.append(text)
        return "999"

    async def search_timeline(self, q, product="Top", count=20):
        return FakeClient.search_results.get((q, product), [])


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


@pytest.fixture
def env(tmp_path):
    FakeClient.posted, FakeClient.search_results, FakeClient.alive = [], {}, True

    async def build(mgr=None):
        db = Database(str(tmp_path / "a.db"))
        await db.connect()
        old = datetime.now(timezone.utc) - timedelta(days=30)
        ids = []
        for n, pid in (("target", "s1"), ("observer", "s2")):
            a = Account(username=n, password="x", auth_token="t", ct0="c", status=AccountStatus.CLEAN,
                        proxy_id=pid, created_at=old)
            ids.append(await db.add_account(a))
        for _ in range(12):
            await db.log_action(ids[0], "tweets", "t")
        # 行動した日を10日分
        for d in range(10):
            ts = (old + timedelta(days=d)).isoformat()
            await db.conn.execute("INSERT INTO action_log (account_id, action, detail, created_at) VALUES (?,?,?,?)",
                                  (ids[0], "likes", "1", ts))
        await db.conn.commit()
        cfg = WarmupConfig()
        ex = GraduationExaminer(db, mgr or FakeProxyMgr(), cfg, client_factory=FakeClient)
        return db, ex, await db.get_account_by_id(ids[0]), ids

    return build


def test_graduation_full_flow_pass(env):
    async def run():
        db, ex, acc, ids = await env()
        r1 = await ex.examine(acc)
        assert r1.stage == "pending" and len(FakeClient.posted) == 1
        token = (await db.get_graduation(acc.id))["canary_token"]
        # インデックス待ちは pending のまま
        assert (await ex.examine(acc)).stage == "pending"
        await db.save_graduation(acc.id, canary_posted_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
        FakeClient.search_results = {
            (token, "Top"): [{"id": "999", "text": f"メモ {token}", "screen_name": "target"}],
            ("from:target", "Latest"): [{"id": "999", "text": f"メモ {token}", "screen_name": "target"}],
            ("target", "People"): [{"screen_name": "Target", "name": "t"}],
        }
        r = await ex.examine(acc)
        assert r.stage == "passed", r.to_json()
        assert acc.id in await db.graduated_account_ids()
        await db.close()
    asyncio.run(run())


def test_graduation_fails_when_not_in_search(env):
    async def run():
        db, ex, acc, ids = await env()
        await ex.examine(acc)
        await db.save_graduation(acc.id, canary_posted_at=(datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat())
        FakeClient.search_results = {("target", "People"): [{"screen_name": "someone", "name": ""}]}
        r = await ex.examine(acc)
        assert r.stage == "failed"
        assert (await db.get_graduation(acc.id))["attempts"] == 1
        assert acc.id not in await db.graduated_account_ids()
        await db.close()
    asyncio.run(run())


def test_graduation_blocks_on_unplanned_ip_change(env):
    async def run():
        db, ex, acc, ids = await env(FakeProxyMgr(kind="same", unplanned=1))
        r = await ex.examine(acc)
        assert r.stage == "failed" and FakeClient.posted == []
        await db.close()
    asyncio.run(run())


def test_graduation_unknown_without_observer(env):
    async def run():
        db, ex, acc, ids = await env()
        await db.update_status(ids[1], AccountStatus.WARMING)  # 観測者がいなくなる
        await ex.examine(acc)
        await db.save_graduation(acc.id, canary_posted_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat())
        r = await ex.examine(acc)
        assert r.stage == "pending" and any(c.status == "unknown" for c in r.checks)
        await db.close()
    asyncio.run(run())


def test_not_ready_when_young(env):
    async def run():
        db, ex, acc, ids = await env()
        acc.created_at = datetime.now(timezone.utc)
        assert (await ex.examine(acc)).stage == "not_ready"
        await db.close()
    asyncio.run(run())
