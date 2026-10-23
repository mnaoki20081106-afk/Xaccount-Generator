"""
ウォームアップ卒業テスト。

アカウントが検索結果に表示されるかを確認する:
  1. ユニークなマーカーを含むテストツイートを投稿（アカウント自身の認証）
  2. X のインデックス反映を待機（最大 3 分、指数バックオフ）
  3. 同一セッションで GraphQL SearchTimeline を呼び検索
  4. ツイートの表示順位を計測 → GraduationResult を返す

検索エンドポイント: GraphQL SearchTimeline
  https://x.com/i/api/graphql/BYppgupFFS2yqVmmRNjlsw/SearchTimeline
  (参照: vladkens/twscrape, 2026-10-23 更新確認)

※ テストツイートは削除しない。削除はアルゴリズム評価に悪影響を及ぼす。

合格基準: ツイートが Live 検索結果の上位 10 件以内に表示されること。
  rank=1  → rank_score=1.0
  rank<=5 → rank_score=0.7
  rank<=10 → rank_score=0.4
  未表示   → rank_score=0.0（不合格）
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from ..models import Account

try:
    from ..actions.x_api import XApiClient, HAS_CURL_CFFI
except ImportError:
    XApiClient = None  # type: ignore
    HAS_CURL_CFFI = False

_MAX_WAIT_SECS = 180
# 指数バックオフ気味のポーリング間隔（秒）
_POLL_INTERVALS = [15, 20, 30, 45, 70]


@dataclass
class GraduationResult:
    passed: bool
    tweet_id: Optional[str] = None
    tweet_visible: bool = False
    rank: int = -1              # 1-indexed。-1 = 未表示
    rank_score: float = 0.0    # 1.0=1位, 0.7=2-5位, 0.4=6-10位, 0.0=圏外
    search_latency_secs: float = 0.0
    method: str = "unknown"
    details: dict = field(default_factory=dict)
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def fail(cls, reason: str) -> "GraduationResult":
        return cls(passed=False, details={"reason": reason})


class GraduationTest:
    """
    使い方:
        tester = GraduationTest()
        result = await tester.run(account, proxy_url=proxy.url)
        if result.passed:
            # mark account as GRADUATED
    """

    async def run(
        self,
        account: Account,
        proxy_url: Optional[str] = None,
    ) -> GraduationResult:
        if not HAS_CURL_CFFI or not XApiClient:
            return GraduationResult.fail("curl_cffi unavailable — API mode required")
        if not account.auth_token or not account.ct0:
            return GraduationResult.fail("auth_token / ct0 missing")

        marker = f"gtest_{uuid.uuid4().hex[:12]}"
        tweet_text = f"テスト投稿 {marker}"

        async with XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url) as client:
            if not await client.verify():
                return GraduationResult.fail("token invalid")

            tweet_id = await client.tweet_returning_id(tweet_text)
            if not tweet_id:
                return GraduationResult.fail("tweet post failed")

            logger.info(f"[@{account.username}] graduation tweet posted id={tweet_id} marker={marker}")

            result = await self._wait_and_search(client, account.username, marker, tweet_id)

        logger.info(
            f"[@{account.username}] graduation {'PASS' if result.passed else 'FAIL'} "
            f"rank={result.rank} score={result.rank_score:.1f} latency={result.search_latency_secs:.0f}s"
        )
        return result

    async def _wait_and_search(
        self,
        client: XApiClient,
        username: str,
        marker: str,
        tweet_id: str,
    ) -> GraduationResult:
        start = time.monotonic()

        for interval in _POLL_INTERVALS:
            await asyncio.sleep(interval)
            elapsed = time.monotonic() - start
            if elapsed > _MAX_WAIT_SECS:
                break

            result = await self._search_once(client, marker, tweet_id)
            if result is not None:
                result.search_latency_secs = elapsed
                return result

        elapsed = time.monotonic() - start
        return GraduationResult(
            passed=False,
            tweet_id=tweet_id,
            tweet_visible=False,
            rank=-1,
            rank_score=0.0,
            search_latency_secs=elapsed,
            method="timeout",
            details={"reason": "tweet not indexed within timeout", "marker": marker},
        )

    async def _search_once(
        self,
        client: XApiClient,
        marker: str,
        tweet_id: str,
    ) -> Optional[GraduationResult]:
        """
        GraphQL SearchTimeline で marker を Live 検索。
        インデックスされていれば GraduationResult を返す。
        まだ空なら None。
        """
        tweet_ids = await client.search_live_ids(marker)

        if not tweet_ids:
            return None  # 未インデックス — 次のポーリングへ

        try:
            rank = tweet_ids.index(tweet_id) + 1
        except ValueError:
            rank = -1

        visible = rank != -1
        rank_score = 0.0
        if visible:
            if rank == 1:
                rank_score = 1.0
            elif rank <= 5:
                rank_score = 0.7
            elif rank <= 10:
                rank_score = 0.4

        return GraduationResult(
            passed=rank_score >= 0.4,
            tweet_id=tweet_id,
            tweet_visible=visible,
            rank=rank,
            rank_score=rank_score,
            method="gql_search_timeline",
            details={"total_results": len(tweet_ids), "marker": marker},
        )
