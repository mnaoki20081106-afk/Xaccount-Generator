from datetime import date, datetime, timedelta, timezone

import pytest

from src import costs

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def test_to_jpy_converts_foreign_currency_and_rejects_bad_input():
    assert costs.to_jpy(1500, "jpy", None) == 1500
    assert costs.to_jpy(10, "usd", 151.25) == 1512.5
    for args in [(0, "JPY", None), (-5, "JPY", None), (10, "USD", None), (10, "USD", 0), (10, "DOLLAR", 150), (10, "J1Y", 1)]:
        with pytest.raises(costs.CostError):
            costs.to_jpy(*args)


def test_next_due_date_clamps_to_month_end_and_rolls_over():
    assert costs.next_due_date(None, date(2026, 10, 1)) is None
    assert costs.next_due_date(25, date(2026, 10, 23)) == date(2026, 10, 25)
    assert costs.next_due_date(23, date(2026, 10, 23)) == date(2026, 10, 23), "due today still counts as upcoming"
    assert costs.next_due_date(5, date(2026, 10, 23)) == date(2026, 11, 5)
    assert costs.next_due_date(31, date(2026, 2, 10)) == date(2026, 2, 28)
    assert costs.next_due_date(15, date(2026, 12, 20)) == date(2027, 1, 15)
    assert costs.next_due_date(31, date(2026, 11, 30)) == date(2026, 11, 30)


def test_vps_cost_is_prorated_and_ignores_days_before_the_contract():
    month = costs.DAYS_PER_MONTH
    thirty = NOW - timedelta(days=30)
    assert costs.vps_cost(3000, None, thirty, NOW) == pytest.approx(3000 * 30 / month, abs=0.01)
    assert costs.vps_cost(0, None, thirty, NOW) == 0
    # Contract started 10 days ago: only those 10 days count.
    started = (NOW - timedelta(days=10)).date()
    start_dt = datetime(started.year, started.month, started.day, tzinfo=timezone.utc)
    expected = 3000 * ((NOW - start_dt).total_seconds() / 86400) / month
    assert costs.vps_cost(3000, started, thirty, NOW) == pytest.approx(expected, abs=0.01)
    # Contract starting in the future costs nothing yet.
    assert costs.vps_cost(3000, (NOW + timedelta(days=3)).date(), thirty, NOW) == 0


def _window(spec_key, expenses, counts, monthly=0, start=None, earliest=None):
    spec = next(w for w in costs.WINDOWS if w.key == spec_key)
    return costs.compute_window(
        spec, now=NOW, expenses=expenses, status_counts=counts,
        monthly_fee_jpy=monthly, contract_start=start, earliest=earliest,
    )


def test_unit_cost_uses_alive_and_ready_accounts():
    expenses = [
        {"category": "proxy", "amount_jpy": 3000},
        {"category": "sms", "amount_jpy": 1000},
        {"category": "weird-category", "amount_jpy": 500},  # unknown categories fall into "other"
    ]
    counts = {"created": 4, "warming": 3, "clean": 2, "sold": 1, "hard_shadow": 2, "locked": 1}
    result = _window("30d", expenses, counts)

    assert result["costs"]["proxy"] == 3000
    assert result["costs"]["sms"] == 1000
    assert result["costs"]["other"] == 500
    assert result["costs"]["total"] == 4500
    # 13 created, 3 failed → 10 alive; clean+sold = 3 ready
    assert result["accounts"] == {"created": 13, "alive": 10, "ready": 3, "failed": 3}
    assert result["unit_cost"]["per_alive"] == 450
    assert result["unit_cost"]["per_ready"] == 1500
    assert result["unit_cost"]["per_created"] == pytest.approx(346.15, abs=0.01)
    assert result["yield_rate"] == pytest.approx(10 / 13, abs=0.0001)
    assert result["unit_breakdown"]["proxy"] == 300


def test_unit_cost_is_none_not_zero_division_when_nothing_was_produced():
    result = _window("7d", [{"category": "proxy", "amount_jpy": 1000}], {})
    assert result["costs"]["total"] == 1000
    assert result["unit_cost"] == {"per_alive": None, "per_ready": None, "per_created": None}
    assert result["unit_breakdown"] is None
    assert result["yield_rate"] is None

    only_failed = _window("7d", [{"category": "proxy", "amount_jpy": 1000}], {"suspended": 2})
    assert only_failed["unit_cost"]["per_alive"] is None


def test_vps_share_is_part_of_the_total_and_all_time_starts_at_the_earliest_record():
    earliest = NOW - timedelta(days=61)
    result = _window("all", [], {"clean": 2}, monthly=3000, earliest=earliest)
    assert result["costs"]["vps"] == pytest.approx(3000 * 61 / costs.DAYS_PER_MONTH, abs=0.01)
    assert result["costs"]["total"] == result["costs"]["vps"]
    assert result["unit_cost"]["per_alive"] == pytest.approx(result["costs"]["vps"] / 2, abs=0.01)
