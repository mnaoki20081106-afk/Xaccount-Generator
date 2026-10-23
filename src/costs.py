"""
垢1つあたりのコスト（単価）計算。

コストは次の2種類を合算する:
  - 支払い台帳 (expenses): プロキシの通信量チャージ・SMS・Captcha・メール等。
    支払った日 (paid_at) の期間に計上する（現金主義）。
  - VPS契約: 月額を日割りで按分する（発生主義）。台帳には入れない（二重計上防止）。

単価の分母は2通り出す:
  - 見込み単価: 生きている垢（BAN・凍結されていない垢）あたり。ウォームアップ中の
    垢も含むので、生成直後から数字が出る。
  - 完成垢単価: 販売可能になった垢（clean・sold）あたり。確定した実績だが、
    ウォームアップが終わるまでの間は分母が少ない。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

CATEGORIES = ("proxy", "sms", "captcha", "email", "other")

# 月額を日割りにするときの1か月の平均日数
DAYS_PER_MONTH = 30.4375

# 失敗扱いにするステータス（これ以外は「生きている垢」）
FAILED_STATUSES = ("soft_shadow", "hard_shadow", "locked", "suspended")
READY_STATUSES = ("clean", "sold")


class CostError(ValueError):
    """入力値が不正なとき。メッセージは画面にそのまま出せる日本語。"""


def to_jpy(amount: float, currency: str, rate_to_jpy: Optional[float]) -> float:
    currency = (currency or "JPY").strip().upper()
    if not (len(currency) == 3 and currency.isalpha()):
        raise CostError("通貨は3文字の通貨コード（JPY, USDなど）で指定してください")
    if not (amount > 0) or amount > 100_000_000:
        raise CostError("金額は0より大きい値で指定してください")
    if currency == "JPY":
        return round(float(amount), 2)
    if rate_to_jpy is None or not (rate_to_jpy > 0) or rate_to_jpy > 100_000:
        raise CostError("円以外の通貨は、円への換算レートを指定してください")
    return round(float(amount) * float(rate_to_jpy), 2)


def parse_date(value: str, field: str = "日付") -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        raise CostError(f"{field}はYYYY-MM-DDの形式で指定してください")


def _clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_due_date(billing_day: Optional[int], today: date) -> Optional[date]:
    """次の支払日。月末より大きい日は月末に丸める（例: 31日払い→2月は28日）。"""
    if not billing_day:
        return None
    this_month = _clamp_day(today.year, today.month, billing_day)
    if this_month >= today:
        return this_month
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return _clamp_day(year, month, billing_day)


def vps_cost(
    monthly_fee_jpy: float,
    contract_start: Optional[date],
    window_start: datetime,
    window_end: datetime,
) -> float:
    """窓の期間に発生したVPS料金（日割り）。契約開始前の日は含めない。"""
    if monthly_fee_jpy <= 0:
        return 0.0
    start = window_start
    if contract_start is not None:
        contract_dt = datetime(contract_start.year, contract_start.month, contract_start.day, tzinfo=timezone.utc)
        start = max(start, contract_dt)
    seconds = (window_end - start).total_seconds()
    if seconds <= 0:
        return 0.0
    return round(monthly_fee_jpy * (seconds / 86400) / DAYS_PER_MONTH, 2)


@dataclass
class WindowSpec:
    key: str
    label: str
    days: Optional[int]  # None = 累計


WINDOWS = (
    WindowSpec("7d", "直近7日", 7),
    WindowSpec("30d", "直近30日", 30),
    WindowSpec("all", "累計", None),
)


def _ratio(total: float, count: int) -> Optional[float]:
    return round(total / count, 2) if count > 0 else None


def compute_window(
    spec: WindowSpec,
    *,
    now: datetime,
    expenses: list[dict],
    status_counts: dict[str, int],
    monthly_fee_jpy: float,
    contract_start: Optional[date],
    earliest: Optional[datetime],
) -> dict:
    """
    expenses: この窓に含まれる支払い台帳の行 {category, amount_jpy}
    status_counts: この窓に作成された垢のステータス別件数
    earliest: 累計の起点（最初の垢・支払い・契約開始のうち一番古い日時）
    """
    if spec.days is not None:
        window_start = now - timedelta(days=spec.days)
    else:
        window_start = earliest or now

    by_category = {c: 0.0 for c in CATEGORIES}
    for row in expenses:
        category = row["category"] if row["category"] in by_category else "other"
        by_category[category] += float(row["amount_jpy"])
    by_category = {k: round(v, 2) for k, v in by_category.items()}

    vps = vps_cost(monthly_fee_jpy, contract_start, window_start, now)
    total = round(sum(by_category.values()) + vps, 2)

    created = sum(status_counts.values())
    failed = sum(status_counts.get(s, 0) for s in FAILED_STATUSES)
    alive = created - failed
    ready = sum(status_counts.get(s, 0) for s in READY_STATUSES)

    breakdown = {**by_category, "vps": vps}
    return {
        "key": spec.key,
        "label": spec.label,
        "start": window_start.isoformat(),
        "costs": {**breakdown, "total": total},
        "accounts": {"created": created, "alive": alive, "ready": ready, "failed": failed},
        "yield_rate": round(alive / created, 4) if created > 0 else None,
        "unit_cost": {
            "per_alive": _ratio(total, alive),
            "per_ready": _ratio(total, ready),
            "per_created": _ratio(total, created),
        },
        "unit_breakdown": (
            {k: _ratio(v, alive) for k, v in breakdown.items()} if alive > 0 else None
        ),
    }
