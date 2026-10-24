"""
卒業テスト — ウォームアップ完了後に「売れる垢か」を実測で判定する。

静的な条件（経過日数・行動量）だけでなく、以下を実際に確認する:
  1. プロキシ: 生成時と同じ出口 IP で運用できている（想定外の IP 変化なし）
  2. 生存:   本人のトークンで verify_credentials が通る
  3. 検索:   別の垢（観測者）から見て
       a) 一意トークン入りの canary ツイートが「話題 (Top)」検索に出る
       b) from:ユーザー名 の「最新」検索に canary が出る
       c) 「ユーザー」検索でユーザー名が上位に出る
     自分視点の検索は BAN 垢でも見えてしまうため、必ず別垢で見る。

canary は投稿→数分待ってから検索（インデックス待ち）の2段階。状態は DB の
graduation テーブルに持つので、プロセスが落ちても続きから再開できる。
観測者がいない／検索が失敗した場合は "unknown" とし、合格にはしない（甘く通さない）。
"""
from __future__ import annotations

import json
import random
import secrets
import string
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from loguru import logger

from .config import WarmupConfig
from .models import Account, AccountStatus

CANARY_INDEX_WAIT = timedelta(minutes=4)    # 投稿からこの時間は検索しない（インデックス待ち）
CANARY_GIVE_UP = timedelta(minutes=30)      # これを過ぎても出なければ不合格として扱う
CANARY_RETRY_BACKOFF = timedelta(hours=6)   # 不合格後に次の canary を投げるまでの間隔
PEOPLE_RANK_LIMIT = 3                       # ユーザー検索でこの順位以内なら合格

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


def new_canary_token() -> str:
    # 英小文字のみ・先頭固定語: 検索で他のツイートと衝突せず、スパム判定もされにくい形
    return "kk" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(9))


def canary_text(token: str) -> str:
    return random.choice([
        "メモ {t}", "今日の記録 {t}", "ふと思ったこと {t}", "{t} これ覚えておこう",
    ]).format(t=token)


class GraduationExaminer:
    def __init__(
        self,
        db,
        proxy_manager,
        config: WarmupConfig,
        client_factory: Optional[Callable] = None,
    ):
        self.db = db
        self.proxy_manager = proxy_manager
        self.config = config
        if client_factory is None:
            from .actions.x_api import XApiClient

            def client_factory(acc: Account, proxy_url: str):
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

    # ── 観測者（別の垢から検索する） ──────────────────────────────

    async def pick_observer(self, target: Account) -> Optional[Account]:
        pool = await self.db.list_accounts(status=AccountStatus.CLEAN, limit=1000)
        graduated = await self.db.graduated_account_ids()
        cands = [
            a for a in pool
            if a.id != target.id and a.auth_token and a.ct0
            and a.proxy_id != target.proxy_id  # 同一 IP だと関連垢として同一視され得る
            and (a.id in graduated or (_now() - _aware(a.created_at)).days >= 14)
        ]
        if not cands:
            return None
        cands.sort(key=lambda a: a.id not in graduated)  # 卒業済み（実績あり）を優先
        top = [a for a in cands if (a.id in graduated) == (cands[0].id in graduated)]
        return random.choice(top)

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

        # 1. プロキシ（IP 一致）
        ip = await self.proxy_manager.verify_account_ip(account)
        sess = self.proxy_manager.proxy_for_account(account)
        unplanned = ip.get("unplanned_changes", sess.unplanned_ip_changes)
        if ip["kind"] == "unreachable":
            checks.append(Check("proxy_ip", "unknown", "プロキシに接続できない"))
        elif unplanned > self.config.graduation_max_unplanned_ip_changes:
            checks.append(Check("proxy_ip", "fail", f"想定外のIP変化 {unplanned}回 (許容 {self.config.graduation_max_unplanned_ip_changes})"))
        else:
            checks.append(Check("proxy_ip", "pass", f"signup={ip.get('signup_ip')} now={ip.get('ip')}"))
        if checks[-1].status != "pass":
            return GraduationReport("pending" if checks[-1].status == "unknown" else "failed", checks)

        proxy_url = sess.url
        async with self._client_factory(account, proxy_url) as me:
            # 2. 生存
            if not await me.verify():
                checks.append(Check("alive", "fail", "verify_credentials が通らない（凍結/ロック/失効）"))
                return GraduationReport("failed", checks)
            checks.append(Check("alive", "pass"))

            # 3. canary 投稿（まだなら投稿して次回へ）
            token = grad.get("canary_token")
            posted_at = _aware(datetime.fromisoformat(grad["canary_posted_at"])) if grad.get("canary_posted_at") else None
            need_new = (
                not token or not posted_at
                or (_now() - posted_at > CANARY_GIVE_UP + CANARY_RETRY_BACKOFF)
            )
            if need_new:
                token = new_canary_token()
                tid = await me.post_tweet(canary_text(token))
                if not tid:
                    checks.append(Check("canary_post", "unknown", "canary を投稿できなかった"))
                    return GraduationReport("pending", checks)
                await self.db.save_graduation(
                    account.id, canary_token=token, canary_tweet_id=tid,
                    canary_posted_at=_now().isoformat(),
                )
                await self.db.log_action(account.id, "canary", f"{tid}|{token}")
                checks.append(Check("canary_post", "pass", f"id={tid}"))
                return GraduationReport("pending", checks + [Check("search", "unknown", "インデックス待ち")])

            if _now() - posted_at < CANARY_INDEX_WAIT:
                return GraduationReport("pending", checks + [Check("search", "unknown", "インデックス待ち")])
            canary_id = grad.get("canary_tweet_id")

        # 4. 別垢から検索
        observer = await self.pick_observer(account)
        if not observer:
            checks.append(Check("search", "unknown", "観測者にできる垢がない（CLEAN で別IPの垢が必要）"))
            return GraduationReport("pending", checks)
        obs_proxy = self.proxy_manager.proxy_for_account(observer)
        search_checks = await self._search_checks(observer, obs_proxy.url, account.username, token, canary_id)
        checks += search_checks

        if any(c.status == "unknown" for c in search_checks):
            return GraduationReport("pending", checks)
        if all(c.status == "pass" for c in search_checks):
            await self.db.save_graduation(
                account.id, passed=1, report=GraduationReport("passed", checks).to_json(),
                checked_at=_now().isoformat(),
            )
            logger.success(f"[@{account.username}] 卒業テスト合格")
            return GraduationReport("passed", checks)

        # 不合格: インデックスの遅れかもしれないので猶予内は pending のまま
        if _now() - posted_at < CANARY_GIVE_UP:
            return GraduationReport("pending", checks)
        attempts += 1
        await self.db.save_graduation(
            account.id, attempts=attempts, canary_posted_at=(_now() - CANARY_GIVE_UP).isoformat(),
            report=GraduationReport("failed", checks).to_json(), checked_at=_now().isoformat(),
        )
        logger.warning(f"[@{account.username}] 卒業テスト不合格 ({attempts}/{self.config.graduation_max_attempts})")
        if attempts >= self.config.graduation_max_attempts:
            await self.db.update_status(account.id, AccountStatus.SOFT_SHADOW)
        return GraduationReport("failed", checks)

    async def _search_checks(
        self, observer: Account, proxy_url: str, handle: str, token: str, canary_id: Optional[str]
    ) -> list[Check]:
        handle_l = handle.lstrip("@").lower()
        out: list[Check] = []
        try:
            async with self._client_factory(observer, proxy_url) as obs:
                top = await obs.search_timeline(token, "Top")
                found = any(t["id"] == canary_id or token in t["text"] for t in top)
                out.append(Check("search_top", "pass" if found else "fail",
                                 "canary が話題検索に表示" if found else f"話題検索に出ない（{len(top)}件中）"))

                latest = await obs.search_timeline(f"from:{handle_l}", "Latest")
                found = any(t["id"] == canary_id or token in t["text"] for t in latest)
                out.append(Check("search_from_latest", "pass" if found else "fail",
                                 "from: 最新に表示" if found else f"from: 最新に出ない（{len(latest)}件中）"))

                people = await obs.search_timeline(handle_l, "People")
                rank = next((i + 1 for i, u in enumerate(people)
                             if u["screen_name"].lower() == handle_l), None)
                ok = rank is not None and rank <= PEOPLE_RANK_LIMIT
                out.append(Check("search_people", "pass" if ok else "fail",
                                 f"ユーザー検索 {rank}位" if rank else "ユーザー検索に出ない"))
        except Exception as e:
            logger.warning(f"[@{handle_l}] 検索チェック失敗: {e}")
            out.append(Check("search", "unknown", f"検索API失敗: {type(e).__name__}"))
        return out
