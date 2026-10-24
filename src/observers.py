"""
観測者（Observer）プール。

卒業テスト／シャドウバン検査は「別の垢から見えるか」で判定する（自分視点の検索は
BAN垢でも見えてしまう）。その"別の垢"の auth_token / ct0 を管理サイトから登録する。
X-Utility の search-credential と同じ考え方だが、複数登録してローテーションできる。

- トークンは twofa.encryption_key で暗号化して DB に保存。API では値を一切返さない。
- 観測者ごとに固定のプロキシセッション（observer:<id>）を使い、IP を安定させる。
- 登録時に verify_credentials で生存確認。失効/凍結/ロックは status に反映して使わない。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from loguru import logger

from .utils.crypto import decrypt_secret, encrypt_secret

USABLE = ("ok", "unverified")


@dataclass
class Observer:
    id: int
    label: str
    auth_token: str
    ct0: str
    proxy_url: Optional[str]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def public_view(row: dict) -> dict:
    """トークンを含まない表示用の dict。"""
    return {k: row.get(k) for k in
            ("id", "label", "status", "last_error", "last_checked_at", "last_used_at", "created_at")}


class ObserverPool:
    def __init__(self, db, proxy_manager, encryption_key: str, client_factory: Optional[Callable] = None):
        self.db = db
        self.proxy_manager = proxy_manager
        self._key = encryption_key
        if client_factory is None:
            from .actions.x_api import XApiClient

            def client_factory(acc, proxy_url):
                return XApiClient(acc.auth_token, acc.ct0, proxy_url=proxy_url)
        self._client_factory = client_factory

    def _proxy_url(self, observer_id: int) -> Optional[str]:
        if self.proxy_manager is None:
            return None
        return self.proxy_manager.get_warmup_proxy(f"observer:{observer_id}").url

    def _handle(self, row: dict) -> Observer:
        return Observer(
            id=row["id"], label=row["label"],
            auth_token=decrypt_secret(row["auth_token_enc"], self._key),
            ct0=decrypt_secret(row["ct0_enc"], self._key),
            proxy_url=self._proxy_url(row["id"]),
        )

    async def add(self, label: str, auth_token: str, ct0: str) -> dict:
        auth_token, ct0 = auth_token.strip(), ct0.strip()
        if not auth_token or not ct0:
            raise ValueError("OBSERVER_CREDENTIAL_INCOMPLETE")
        oid = await self.db.add_observer(
            label.strip()[:60], encrypt_secret(auth_token, self._key), encrypt_secret(ct0, self._key)
        )
        return await self.verify(oid)

    async def verify(self, observer_id: int) -> dict:
        row = await self.db.get_observer(observer_id)
        if not row:
            raise KeyError("OBSERVER_NOT_FOUND")
        obs = self._handle(row)
        state = "error"
        try:
            async with self._client_factory(obs, obs.proxy_url) as client:
                state = await client.verify_state()
        except Exception as e:
            logger.warning(f"[observer {observer_id}] verify failed: {e}")
        status = {"ok": "ok", "unauthorized": "invalid", "suspended": "suspended",
                  "locked": "locked"}.get(state, "unverified")
        await self.db.update_observer(
            observer_id, status=status, last_checked_at=_now(),
            last_error=None if state == "ok" else f"verify: {state}",
        )
        return public_view(await self.db.get_observer(observer_id))

    async def list(self) -> list[dict]:
        return [public_view(r) for r in await self.db.list_observers()]

    async def remove(self, observer_id: int) -> bool:
        return await self.db.delete_observer(observer_id)

    async def pick(self) -> Optional[Observer]:
        """使える観測者のうち、最後に使ってから一番時間が経っているものを返す。"""
        rows = [r for r in await self.db.list_observers() if r["status"] in USABLE]
        if not rows:
            return None
        rows.sort(key=lambda r: r["last_used_at"] or "")
        await self.db.update_observer(rows[0]["id"], last_used_at=_now())
        return self._handle(rows[0])

    async def report(self, observer: Observer, state: str) -> None:
        """観測に使った結果を反映（失効/凍結した観測者を以後使わない）。"""
        status = {"unauthorized": "invalid", "suspended": "suspended", "locked": "locked"}.get(state)
        if status:
            await self.db.update_observer(
                observer.id, status=status, last_error=f"search: {state}", last_checked_at=_now()
            )
            logger.warning(f"[observer {observer.id}] marked {status}")
