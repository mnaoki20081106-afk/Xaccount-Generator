"""
卒業テスト — ウォームアップ完了後に「売れる垢か」を実測で判定する。

検索の判定は X-Utility（shadowban.ts）の確認方式を採用:
  - searchBan:        観測者で `from:ユーザー名` を「話題(Top)」検索 → 本人のポストが出るか
  - searchSuggestion: 観測者で検索候補(typeahead) `@ユーザー名 表示名` → 本人が出るか
観測者は管理サイトから登録した別の垢（src/observers.py）。自分視点の検索は
BAN垢でも見えてしまうため、必ず別垢で見る。

静的な条件（経過日数・行動日数・ツイート数）に加え、
  - プロキシ: 生成時と同じ出口 IP で運用できている（想定外の IP 変化なし）
  - 生存:     本人のトークンで verify_credentials が通る（凍結/ロックは状態に反映）
  - ユーザー: 存在し、非凍結・非鍵・投稿が1件以上
も確認する。観測者がいない／検索が失敗した場合は "unknown" とし、合格にしない。
結果は DB の graduation テーブルに保存。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from loguru import logger

from .config import WarmupConfig
from .models import Account, AccountStatus

RETRY_BACKOFF = timedelta(hours=6)   # 不合格後、次の判定までの間隔（一時的な不調で連続不合格にしない）

_NOT_GRADUATABLE = {
    AccountStatus.SOLD, AccountStatus.SUSPENDED, AccountStatus.LOCKED,
    AccountStatus.HARD_SHADOW,
}


@dataclass
class Check:
    name: str
    status: str            # pass / fail / unknown
    detail: str = ""


@dataclass
class GraduationReport:
    stage: str             # not_ready / pending / failed / passed
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.stage == "passed"

    def to_json(self) -> str:
        return json.dumps(
            {"stage": self.stage, "checks": [asdict(c) for c in self.checks]},
            ensure_ascii=False,
        )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class GraduationExaminer:
    def __init__(self, db, proxy_manager, config: WarmupConfig, observers, client_factory: Optional[Callable] = None):
        self.db = db
        self.proxy_manager = proxy_manager
        self.config = config
        self.observers = observers
        if client_factory is None:
            from .actions.x_api import XApiClient

            def client_factory(acc, proxy_url: str):
                return XApiClient(acc.auth_token, acc.ct0, proxy_url=proxy_url)
        self._client_factory = client_factory

    # ── 静的条件（ネットワーク不要） ─────────────────────────────

    async def static_checks(self, account: Account) -> list[Check]:
        cfg = self.config
        age_days = (_now() - _aware(account.created_at)).days
        stats = await self.db.action_stats(account.id)
        counts = stats["counts"]
        return [
            Check("age", "pass" if age_days >= cfg.graduation_min_days else "fail",
                  f"{age_days}日 / 必要 {cfg.graduation_min_days}日"),
            Check("active_days", "pass" if stats["active_days"] >= cfg.graduation_min_active_days else "fail",
                  f"行動した日 {stats['active_days']} / 必要 {cfg.graduation_min_active_days}"),
            Check("tweets", "pass" if counts.get("tweets", 0) >= cfg.graduation_min_tweets else "fail",
                  f"ツイート {counts.get('tweets', 0)} / 必要 {cfg.graduation_min_tweets}"),
            Check("tokens", "pass" if account.auth_token and account.ct0 else "fail",
                  "auth_token/ct0 " + ("あり" if account.auth_token and account.ct0 else "なし")),
        ]

    # ── 本体 ──────────────────────────────────────────────────

    async def examine(self, account: Account) -> GraduationReport:
        if account.status in _NOT_GRADUATABLE:
            return GraduationReport("not_ready", [Check("status", "fail", account.status.value)])

        grad = await self.db.get_graduation(account.id) or {}
        if grad.get("passed"):
            return GraduationReport("passed", [Check("already", "pass", "合格済み")])

        checks = await self.static_checks(account)
        if any(c.status == "fail" for c in checks):
            return GraduationReport("not_ready", checks)

        attempts = grad.get("attempts", 0)
        if attempts >= self.config.graduation_max_attempts:
            return GraduationReport("failed", checks + [Check("attempts", "fail", f"{attempts}回不合格")])
        if grad.get("checked_at") and attempts and _now() - _aware(datetime.fromisoformat(grad["checked_at"])) < RETRY_BACKOFF:
            return GraduationReport("pending", checks + [Check("retry", "unknown", "前回不合格から間隔待ち")])

        # 1. プロキシ（IP 一致）
        ip = await self.proxy_manager.verify_account_ip(account)
        sess = self.proxy_manager.proxy_for_account(account)
        unplanned = ip.get("unplanned_changes", sess.unplanned_ip_changes)
        if ip["kind"] == "unreachable":
            return GraduationReport("pending", checks + [Check("proxy_ip", "unknown", "プロキシに接続できない")])
        if unplanned > self.config.graduation_max_unplanned_ip_changes:
            checks.append(Check("proxy_ip", "fail", f"想定外のIP変化 {unplanned}回 (許容 {self.config.graduation_max_unplanned_ip_changes})"))
            return GraduationReport("failed", checks)
        checks.append(Check("proxy_ip", "pass", f"signup={ip.get('signup_ip')} now={ip.get('ip')}"))

        # 2. 生存（本人のトークン）
        async with self._client_factory(account, sess.url) as me:
            state = await me.verify_state()
        if state in ("suspended", "locked"):
            await self.db.update_status(
                account.id, AccountStatus.SUSPENDED if state == "suspended" else AccountStatus.LOCKED
            )
            checks.append(Check("alive", "fail", state))
            return GraduationReport("failed", checks)
        if state != "ok":
            checks.append(Check("alive", "fail" if state == "unauthorized" else "unknown", state))
            return GraduationReport("failed" if state == "unauthorized" else "pending", checks)
        checks.append(Check("alive", "pass"))

        # 3. 観測者から検索（X-Utility と同じ判定）
        observer = await self.observers.pick()
        if not observer:
            checks.append(Check("search", "unknown", "観測者が未登録（管理サイトで観測者のauth_token/ct0を登録）"))
            return GraduationReport("pending", checks)
        search = await self._search_checks(observer, account.username)
        checks += search

        if any(c.status == "unknown" for c in search):
            return GraduationReport("pending", checks)
        if all(c.status == "pass" for c in search):
            await self.db.save_graduation(
                account.id, passed=1, report=GraduationReport("passed", checks).to_json(),
                checked_at=_now().isoformat(),
            )
            logger.success(f"[@{account.username}] 卒業テスト合格")
            return GraduationReport("passed", checks)

        attempts += 1
        await self.db.save_graduation(
            account.id, attempts=attempts,
            report=GraduationReport("failed", checks).to_json(), checked_at=_now().isoformat(),
        )
        logger.warning(f"[@{account.username}] 卒業テスト不合格 ({attempts}/{self.config.graduation_max_attempts})")
        if attempts >= self.config.graduation_max_attempts:
            await self.db.update_status(account.id, AccountStatus.SOFT_SHADOW)
        return GraduationReport("failed", checks)

    async def _search_checks(self, observer, username: str) -> list[Check]:
        handle = username.lstrip("@")
        try:
            async with self._client_factory(observer, observer.proxy_url) as obs:
                state = await obs.verify_state()
                if state != "ok":
                    await self.observers.report(observer, state)
                    return [Check("search", "unknown", f"観測者 #{observer.id} が使えない: {state}")]

                user = await obs.user_by_screen_name(handle)
                if user["state"] != "ok":
                    return [Check("user", "fail", {"not_found": "ユーザーが見つからない",
                                                   "suspended": "凍結されている",
                                                   "protected": "鍵アカウント"}[user["state"]])]
                if not user["tweet_count"]:
                    return [Check("user", "fail", "投稿が0件のため検索確認ができない")]
                out = [Check("user", "pass", f"投稿 {user['tweet_count']}件")]
                real = user["screen_name"]

                top = await obs.search_timeline(f"from:{real}", "Top")
                own = any(t["screen_name"].lower() == real.lower() for t in top)
                out.append(Check("search_top", "pass" if own else "fail",
                                 "Top検索に本人のポストを確認" if own else "Top検索に本人のポストが見つからない"))

                sugg = await obs.typeahead_users(f"@{real} {user['name']}")
                found = any(u.lower() == real.lower() for u in sugg)
                out.append(Check("search_suggestion", "pass" if found else "fail",
                                 "検索候補に本人を確認" if found else "検索候補に本人が見つからない"))
                return out
        except Exception as e:
            logger.warning(f"[@{handle}] 検索チェック失敗: {e}")
            return [Check("search", "unknown", f"検索API失敗: {type(e).__name__}")]
