import asyncio
from datetime import date, datetime, timedelta, timezone

from src.actions.x_api import XAccountLocked, XApiClient, XAlreadyDone, XAutomated, _check_response
from src.db import Database
from src.models import Account
from src.warmup import (
    ACTIVE_HOURS_UTC, WarmupPhase, WarmupScheduler, day_limits, is_rest_day, session_hours,
)


def _created(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_limits_ramp_smoothly_and_no_early_tweets():
    d = date(2026, 10, 1)
    likes = [day_limits(1, _created(n), d)["likes"] for n in range(0, 21)]
    assert likes[-1] >= likes[0]
    assert max(abs(a - b) for a, b in zip(likes, likes[1:])) <= 8  # 階段状に倍々にならない
    assert all(day_limits(1, _created(n), d)["tweets"] == 0 for n in range(0, 5))
    assert day_limits(1, _created(30), d)["tweets"] > 0


def test_limits_are_deterministic_per_day_and_vary_per_account():
    d = date(2026, 10, 1)
    assert day_limits(5, _created(20), d) == day_limits(5, _created(20), d)
    assert len({tuple(day_limits(i, _created(20), d).values()) for i in range(30)}) > 5


def test_rest_days_only_after_first_week():
    ds = [date(2026, 10, 1) + timedelta(days=i) for i in range(80)]
    assert not any(is_rest_day(3, _created(3), d) for d in ds)
    rest = sum(is_rest_day(3, _created(20), d) for d in ds)
    assert 3 <= rest <= 20


def test_session_hours_in_active_window_and_stable():
    d = date(2026, 10, 1)
    for acc in range(40):
        for ph in WarmupPhase:
            h = session_hours(acc, ph, d)
            assert h == session_hours(acc, ph, d) and h and set(h) <= set(ACTIVE_HOURS_UTC)
    # 垢ごとに時刻が散る（全垢が同時刻に動かない）
    assert len({tuple(session_hours(a, WarmupPhase.ACTIVE, d)) for a in range(40)}) > 8


def test_share_splits_remaining_across_sessions():
    sh = WarmupScheduler._share
    assert sh(0, 3) == 0
    assert sh(7, 1) == 7
    assert all(1 <= sh(9, 3) <= 4 for _ in range(50))


def test_run_daily_skips_when_backoff(tmp_path, monkeypatch):
    async def run():
        db = Database(str(tmp_path / "a.db"))
        await db.connect()
        aid = await db.add_account(Account(username="u", password="x", created_at=_created(30)))
        acc = await db.get_account_by_id(aid)
        sch = WarmupScheduler(None, None, None, None, None, None, db=db)
        monkeypatch.setattr("src.warmup.is_active_hour", lambda: True)
        monkeypatch.setattr("src.warmup.is_rest_day", lambda *a, **k: False)
        await db.log_action(aid, "backoff", "automated")
        r = await sch.run_daily_actions(acc)
        assert r == {"skipped": True, "reason": "backoff"}
        assert await db.has_recent_action(aid, "backoff", 24)
        await db.close()
    asyncio.run(run())


class _R:
    def __init__(self, status, body):
        self.status_code, self._b, self.headers = status, body, {}

    def json(self):
        return self._b


def test_error_codes_distinguish_already_liked_from_automation():
    import pytest
    with pytest.raises(XAlreadyDone):
        _check_response(_R(403, {"errors": [{"code": 139, "message": "already favorited"}]}))
    with pytest.raises(XAutomated):
        _check_response(_R(403, {"errors": [{"code": 226, "message": "automated"}]}))
    with pytest.raises(XAccountLocked):
        _check_response(_R(403, {"errors": [{"code": 326, "message": "locked"}]}))


def test_client_flags_set_by_note():
    c = XApiClient("a", "b")
    c._note(XAutomated("x", 226))
    c._note(XAccountLocked("x", 326))
    assert c.flags == {"automated", "locked"}
