"""
垢単価サーキットブレーカー。
rolling_avg > max_usd になったら生成を止める。
"""
from __future__ import annotations

from loguru import logger

from .ledger import SpendingLedger


class CostLimitExceeded(Exception):
    def __init__(self, avg: float, limit: float):
        super().__init__(
            f"垢単価 ${avg:.3f} が上限 ${limit:.2f} を超過 — 生成を一時停止します"
        )
        self.avg = avg
        self.limit = limit


class CostGuard:
    def __init__(
        self,
        ledger: SpendingLedger,
        target_usd: float = 0.46,
        max_usd: float = 0.80,
    ):
        self.ledger = ledger
        self.target_usd = target_usd
        self.max_usd = max_usd

    async def check(self):
        """avg > max_usd なら CostLimitExceeded を raise する。"""
        avg = await self.ledger.rolling_avg(50)
        if avg == 0.0:
            return  # まだ実績なし

        if avg > self.max_usd:
            raise CostLimitExceeded(avg, self.max_usd)

        if avg > self.target_usd:
            logger.warning(
                f"[billing] 垢単価 ${avg:.3f} > 目標 ${self.target_usd:.2f} "
                "(上限 ${:.2f} まで余裕あり)".format(self.max_usd)
            )

    async def report(self) -> str:
        avg = await self.ledger.rolling_avg(50)
        status = "OK" if avg <= self.target_usd else ("WARN" if avg <= self.max_usd else "STOP")
        return (
            f"[billing] avg=${avg:.3f} target=${self.target_usd:.2f} "
            f"max=${self.max_usd:.2f} [{status}]"
        )
