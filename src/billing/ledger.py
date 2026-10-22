"""
垢ごとの支出を記録・集計する。
DB テーブル account_spending を使う（db.py の SCHEMA に追加済み）。
"""
from __future__ import annotations

from loguru import logger


class SpendingLedger:
    def __init__(self, db):
        self.db = db

    async def record(
        self,
        account_id: int,
        sms_usd: float = 0.0,
        proxy_usd: float = 0.0,
        captcha_usd: float = 0.0,
    ):
        """支出を垢に加算記録（upsert）。"""
        await self.db.conn.execute(
            """
            INSERT INTO account_spending (account_id, sms_usd, proxy_usd, captcha_usd, updated_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(account_id) DO UPDATE SET
                sms_usd     = sms_usd     + excluded.sms_usd,
                proxy_usd   = proxy_usd   + excluded.proxy_usd,
                captcha_usd = captcha_usd + excluded.captcha_usd,
                updated_at  = excluded.updated_at
            """,
            (account_id, sms_usd, proxy_usd, captcha_usd),
        )
        await self.db.conn.commit()
        total = sms_usd + proxy_usd + captcha_usd
        logger.debug(
            f"[billing] account_id={account_id} +sms=${sms_usd:.4f} "
            f"+proxy=${proxy_usd:.4f} +captcha=${captcha_usd:.4f} total=${total:.4f}"
        )

    async def get(self, account_id: int) -> dict:
        """垢の支出内訳を返す。未記録なら全ゼロ。"""
        cur = await self.db.conn.execute(
            "SELECT sms_usd, proxy_usd, captcha_usd FROM account_spending WHERE account_id = ?",
            (account_id,),
        )
        row = await cur.fetchone()
        if not row:
            return {"sms_usd": 0.0, "proxy_usd": 0.0, "captcha_usd": 0.0, "total_usd": 0.0}
        s, p, c = row[0], row[1], row[2]
        return {"sms_usd": s, "proxy_usd": p, "captcha_usd": c, "total_usd": s + p + c}

    async def rolling_avg(self, n: int = 50) -> float:
        """直近 n 垢の平均単価（支出記録がある垢のみ対象）。"""
        cur = await self.db.conn.execute(
            """
            SELECT AVG(sms_usd + proxy_usd + captcha_usd)
            FROM (
                SELECT sms_usd, proxy_usd, captcha_usd
                FROM account_spending
                ORDER BY updated_at DESC
                LIMIT ?
            )
            """,
            (n,),
        )
        row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
