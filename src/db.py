import aiosqlite
from pathlib import Path
from datetime import datetime
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

CREATE TABLE IF NOT EXISTS proxy_blacklist (
    ip TEXT PRIMARY KEY,
    reason TEXT,
    banned_at TEXT NOT NULL,
    expire_at TEXT
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
        await self.conn.commit()
        logger.info(f"DB connected: {self.path}")

    async def close(self):
        if hasattr(self, "conn"):
            await self.conn.close()

    async def add_account(self, account: Account) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO accounts (
                username, password, email, phone, twofa_secret, status,
                shadow_score, proxy_id, fingerprint_hash, cookies_path,
                created_at, last_action_at, sold_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account.username,
                account.password,
                account.email,
                account.phone,
                account.twofa_secret,
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
            (datetime.utcnow().isoformat(), account_id),
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
            (account_id, action, detail, datetime.utcnow().isoformat()),
        )
        await self.conn.commit()

    async def get_action_count_today(self, account_id: int, action: str) -> int:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cursor = await self.conn.execute(
            "SELECT COUNT(*) FROM action_log WHERE account_id = ? AND action = ? AND created_at LIKE ?",
            (account_id, action, f"{today}%"),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_account_by_id(self, account_id: int) -> Optional[Account]:
        cursor = await self.conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_account(row)

    def _row_to_account(self, row) -> Account:
        return Account(
            id=row["id"],
            username=row["username"],
            password=row["password"],
            email=row["email"],
            phone=row["phone"],
            twofa_secret=row["twofa_secret"],
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
