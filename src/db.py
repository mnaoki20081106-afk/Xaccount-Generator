import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from loguru import logger

from .models import Account, AccountStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    twofa_secret TEXT,
    auth_token TEXT,
    ct0 TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    shadow_score REAL DEFAULT 0.0,
    proxy_id TEXT,
    fingerprint_hash TEXT,
    cookies_path TEXT,
    created_at TEXT NOT NULL,
    last_action_at TEXT,
    sold_at TEXT,
    notes TEXT
);

-- auth_token / ct0 カラムをマイグレーション（既存DBへの追加）
CREATE TABLE IF NOT EXISTS _migrations (key TEXT PRIMARY KEY);


CREATE TABLE IF NOT EXISTS proxy_blacklist (
    ip TEXT PRIMARY KEY,
    reason TEXT,
    banned_at TEXT NOT NULL,
    expire_at TEXT
);

-- 支払い台帳（プロキシ通信量のチャージ・SMS・Captcha等）。金額は円換算して保存する。
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    amount_jpy REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'JPY',
    amount_original REAL NOT NULL,
    rate_to_jpy REAL NOT NULL DEFAULT 1,
    quantity REAL,
    unit TEXT,
    paid_at TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_paid_at ON expenses(paid_at);

-- VPS契約（サーバーは1つなので1行だけ）
CREATE TABLE IF NOT EXISTS vps_contract (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    label TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    monthly_fee_jpy REAL NOT NULL DEFAULT 0,
    billing_day INTEGER,
    started_at TEXT,
    note TEXT,
    updated_at TEXT NOT NULL
);

-- 観測者（検索を代行する"別の垢"）。トークンは暗号化して保存する。
CREATE TABLE IF NOT EXISTS observers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL DEFAULT '',
    auth_token_enc TEXT NOT NULL,
    ct0_enc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unverified',  -- unverified / ok / invalid / suspended / locked
    last_error TEXT,
    last_checked_at TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL
);

-- 卒業テスト（ウォームアップ完了→検索TOP掲載の確認）の状態と結果
CREATE TABLE IF NOT EXISTS graduation (
    account_id INTEGER PRIMARY KEY,
    passed INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    canary_token TEXT,
    canary_tweet_id TEXT,
    canary_posted_at TEXT,
    report TEXT,
    checked_at TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()
        logger.info(f"DB connected: {self.path}")

    async def _migrate(self):
        """既存DBへの非破壊カラム追加マイグレーション。"""
        migrations = [
            ("add_auth_token", "ALTER TABLE accounts ADD COLUMN auth_token TEXT"),
            ("add_ct0",        "ALTER TABLE accounts ADD COLUMN ct0 TEXT"),
        ]
        for key, sql in migrations:
            row = await (await self.conn.execute(
                "SELECT key FROM _migrations WHERE key = ?", (key,)
            )).fetchone()
            if row:
                continue
            try:
                await self.conn.execute(sql)
                await self.conn.execute(
                    "INSERT INTO _migrations (key) VALUES (?)", (key,)
                )
                logger.debug(f"Migration applied: {key}")
            except Exception:
                # カラムが既に存在する場合は無視してマーク
                try:
                    await self.conn.execute(
                        "INSERT OR IGNORE INTO _migrations (key) VALUES (?)", (key,)
                    )
                except Exception:
                    pass

    async def close(self):
        if hasattr(self, "conn"):
            await self.conn.close()

    async def add_account(self, account: Account) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO accounts (
                username, password, email, phone, twofa_secret,
                auth_token, ct0, status, shadow_score, proxy_id,
                fingerprint_hash, cookies_path, created_at, last_action_at,
                sold_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account.username,
                account.password,
                account.email,
                account.phone,
                account.twofa_secret,
                account.auth_token,
                account.ct0,
                account.status.value,
                account.shadow_score,
                account.proxy_id,
                account.fingerprint_hash,
                account.cookies_path,
                account.created_at.isoformat(),
                account.last_action_at.isoformat() if account.last_action_at else None,
                account.sold_at.isoformat() if account.sold_at else None,
                account.notes,
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def update_tokens(self, account_id: int, auth_token: str, ct0: str):
        await self.conn.execute(
            "UPDATE accounts SET auth_token = ?, ct0 = ? WHERE id = ?",
            (auth_token, ct0, account_id),
        )
        await self.conn.commit()

    async def get_account_by_username(self, username: str) -> Optional[Account]:
        cursor = await self.conn.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_account(row)

    async def list_accounts(
        self, status: Optional[AccountStatus] = None, limit: int = 100
    ) -> List[Account]:
        if status:
            cursor = await self.conn.execute(
                "SELECT * FROM accounts WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status.value, limit),
            )
        else:
            cursor = await self.conn.execute(
                "SELECT * FROM accounts ORDER BY id DESC LIMIT ?", (limit,)
            )
        rows = await cursor.fetchall()
        return [self._row_to_account(r) for r in rows]

    async def update_status(self, account_id: int, status: AccountStatus):
        await self.conn.execute(
            "UPDATE accounts SET status = ? WHERE id = ?",
            (status.value, account_id),
        )
        await self.conn.commit()

    async def update_shadow_score(self, account_id: int, score: float):
        await self.conn.execute(
            "UPDATE accounts SET shadow_score = ? WHERE id = ?",
            (score, account_id),
        )
        await self.conn.commit()

    async def update_last_action(self, account_id: int):
        await self.conn.execute(
            "UPDATE accounts SET last_action_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), account_id),
        )
        await self.conn.commit()

    async def update_proxy(self, account_id: int, proxy_id: str):
        await self.conn.execute(
            "UPDATE accounts SET proxy_id = ? WHERE id = ?",
            (proxy_id, account_id),
        )
        await self.conn.commit()

    async def log_action(self, account_id: int, action: str, detail: str = ""):
        await self.conn.execute(
            "INSERT INTO action_log (account_id, action, detail, created_at) VALUES (?, ?, ?, ?)",
            (account_id, action, detail, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def recent_action_details(self, account_id: int, action: str, limit: int = 30) -> list[str]:
        rows = await (await self.conn.execute(
            "SELECT detail FROM action_log WHERE account_id = ? AND action = ? ORDER BY id DESC LIMIT ?",
            (account_id, action, limit),
        )).fetchall()
        return [r["detail"] or "" for r in rows]

    async def has_recent_action(self, account_id: int, action: str, hours: float) -> bool:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        row = await (await self.conn.execute(
            "SELECT 1 FROM action_log WHERE account_id = ? AND action = ? AND created_at >= ? LIMIT 1",
            (account_id, action, since),
        )).fetchone()
        return row is not None

    async def get_action_count_today(self, account_id: int, action: str) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE account_id = ? AND action = ? AND created_at LIKE ?",
            (account_id, action, f"{today}%"),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── 観測者 ──

    async def add_observer(self, label: str, auth_token_enc: str, ct0_enc: str) -> int:
        cur = await self.conn.execute(
            "INSERT INTO observers (label, auth_token_enc, ct0_enc, created_at) VALUES (?,?,?,?)",
            (label, auth_token_enc, ct0_enc, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_observers(self) -> List[dict]:
        rows = await (await self.conn.execute("SELECT * FROM observers ORDER BY id")).fetchall()
        return [dict(r) for r in rows]

    async def get_observer(self, observer_id: int) -> Optional[dict]:
        row = await (await self.conn.execute("SELECT * FROM observers WHERE id = ?", (observer_id,))).fetchone()
        return dict(row) if row else None

    async def update_observer(self, observer_id: int, **fields) -> None:
        allowed = {"label", "auth_token_enc", "ct0_enc", "status", "last_error",
                   "last_checked_at", "last_used_at"}
        if set(fields) - allowed:
            raise ValueError("unknown observer fields")
        sets = ", ".join(f"{k} = ?" for k in fields)
        await self.conn.execute(f"UPDATE observers SET {sets} WHERE id = ?", (*fields.values(), observer_id))
        await self.conn.commit()

    async def delete_observer(self, observer_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM observers WHERE id = ?", (observer_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    # ── 卒業テスト ──

    async def get_graduation(self, account_id: int) -> Optional[dict]:
        row = await (await self.conn.execute(
            "SELECT * FROM graduation WHERE account_id = ?", (account_id,)
        )).fetchone()
        return dict(row) if row else None

    async def save_graduation(self, account_id: int, **fields) -> None:
        allowed = {"passed", "attempts", "canary_token", "canary_tweet_id",
                   "canary_posted_at", "report", "checked_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"unknown graduation fields: {bad}")
        await self.conn.execute(
            "INSERT OR IGNORE INTO graduation (account_id) VALUES (?)", (account_id,)
        )
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            await self.conn.execute(
                f"UPDATE graduation SET {sets} WHERE account_id = ?",
                (*fields.values(), account_id),
            )
        await self.conn.commit()

    async def graduated_account_ids(self) -> set[int]:
        rows = await (await self.conn.execute(
            "SELECT account_id FROM graduation WHERE passed = 1"
        )).fetchall()
        return {r["account_id"] for r in rows}

    async def action_stats(self, account_id: int) -> dict:
        """行動ログの集計: 種別ごとの件数と、何かしら行動した日数。"""
        rows = await (await self.conn.execute(
            "SELECT action, COUNT(*) AS n FROM action_log WHERE account_id = ? GROUP BY action",
            (account_id,),
        )).fetchall()
        days = await (await self.conn.execute(
            "SELECT COUNT(DISTINCT substr(created_at, 1, 10)) AS d FROM action_log "
            "WHERE account_id = ? AND action IN ('likes','follows','tweets','browse')",
            (account_id,),
        )).fetchone()
        return {"counts": {r["action"]: r["n"] for r in rows}, "active_days": days["d"] if days else 0}

    async def last_action_detail(self, account_id: int, action: str) -> Optional[str]:
        row = await (await self.conn.execute(
            "SELECT detail FROM action_log WHERE account_id = ? AND action = ? ORDER BY id DESC LIMIT 1",
            (account_id, action),
        )).fetchone()
        return row["detail"] if row else None

    # ── コスト（支払い台帳・VPS契約） ──

    async def add_expense(
        self, *, category: str, description: str, amount_jpy: float, currency: str,
        amount_original: float, rate_to_jpy: float, quantity: Optional[float],
        unit: Optional[str], paid_at: str, note: Optional[str],
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO expenses (category, description, amount_jpy, currency, amount_original,
                                  rate_to_jpy, quantity, unit, paid_at, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (category, description, amount_jpy, currency, amount_original, rate_to_jpy,
             quantity, unit, paid_at, note, datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def list_expenses(self, since: Optional[str] = None, limit: int = 500) -> List[dict]:
        sql = "SELECT * FROM expenses"
        params: list = []
        if since:
            sql += " WHERE paid_at >= ?"
            params.append(since)
        sql += " ORDER BY paid_at DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = await (await self.conn.execute(sql, params)).fetchall()
        return [dict(r) for r in rows]

    async def delete_expense(self, expense_id: int) -> bool:
        cursor = await self.conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        await self.conn.commit()
        return cursor.rowcount > 0

    async def account_status_counts(self, since_iso: Optional[str] = None) -> dict:
        """作成日時が since 以降の垢のステータス別件数（None なら全期間）。"""
        sql = "SELECT status, COUNT(*) AS n FROM accounts"
        params: list = []
        if since_iso:
            sql += " WHERE created_at >= ?"
            params.append(since_iso)
        sql += " GROUP BY status"
        rows = await (await self.conn.execute(sql, params)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    async def first_account_created_at(self) -> Optional[str]:
        row = await (await self.conn.execute("SELECT MIN(created_at) AS t FROM accounts")).fetchone()
        return row["t"] if row else None

    async def first_expense_paid_at(self) -> Optional[str]:
        row = await (await self.conn.execute("SELECT MIN(paid_at) AS t FROM expenses")).fetchone()
        return row["t"] if row else None

    async def get_vps_contract(self) -> Optional[dict]:
        row = await (await self.conn.execute("SELECT * FROM vps_contract WHERE id = 1")).fetchone()
        return dict(row) if row else None

    async def save_vps_contract(
        self, *, label: str, provider: str, monthly_fee_jpy: float,
        billing_day: Optional[int], started_at: Optional[str], note: Optional[str],
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO vps_contract (id, label, provider, monthly_fee_jpy, billing_day, started_at, note, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label = excluded.label, provider = excluded.provider,
                monthly_fee_jpy = excluded.monthly_fee_jpy, billing_day = excluded.billing_day,
                started_at = excluded.started_at, note = excluded.note, updated_at = excluded.updated_at
            """,
            (label, provider, monthly_fee_jpy, billing_day, started_at, note,
             datetime.now(timezone.utc).isoformat()),
        )
        await self.conn.commit()

    async def get_account_by_id(self, account_id: int) -> Optional[Account]:
        cursor = await self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_account(row)

    def _row_to_account(self, row) -> Account:
        keys = row.keys() if hasattr(row, "keys") else []
        return Account(
            id=row["id"],
            username=row["username"],
            password=row["password"],
            email=row["email"],
            phone=row["phone"],
            twofa_secret=row["twofa_secret"],
            auth_token=row["auth_token"] if "auth_token" in keys else None,
            ct0=row["ct0"] if "ct0" in keys else None,
            status=AccountStatus(row["status"]),
            shadow_score=row["shadow_score"] or 0.0,
            proxy_id=row["proxy_id"],
            fingerprint_hash=row["fingerprint_hash"],
            cookies_path=row["cookies_path"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_action_at=datetime.fromisoformat(row["last_action_at"])
            if row["last_action_at"]
            else None,
            sold_at=datetime.fromisoformat(row["sold_at"]) if row["sold_at"] else None,
            notes=row["notes"],
        )
