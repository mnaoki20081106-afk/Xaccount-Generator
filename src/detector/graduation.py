"""
ウォームアップ卒業テスト。

アカウントが検索結果に表示されるかを確認する:
  1. ユニークなマーカーを含むテストツイートを投稿（XApiClient）
  2. X のインデックス反映を待機（最大 3 分、指数バックオフ）
  3. ゲスト検索 API でそのマーカーを検索（未認証 = 一般ユーザー視点）
  4. ツイートの表示順位を計測 → GraduationResult を返す
  5. テストツイートを削除（cleanup=True のとき）

合格基準: ツイートが検索結果の上位10件以内に表示されること。
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

import httpx
from loguru import logger

from ..models import Account

try:
    from ..actions.x_api import XApiClient, HAS_CURL_CFFI
except ImportError:
    XApiClient = None  # type: ignore
    HAS_CURL_CFFI = False

_GUEST_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "=1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
_GUEST_TOKEN_URL = "https://api.twitter.com/1.1/guest/activate.json"
_ADAPTIVE_SEARCH_URL = (
    "https://twitter.com/i/api/2/search/adaptive.json"
    "?q={query}&count=20&result_filter=live&tweet_search_mode=live"
)

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

    def __init__(self):
        self._guest_token_cache: tuple[str, float] | None = None

    async def run(
        self,
        account: Account,
        proxy_url: Optional[str] = None,
        cleanup: bool = True,
    ) -> GraduationResult:
        if not HAS_CURL_CFFI or not XApiClient:
            return GraduationResult.fail("curl_cffi unavailable — API mode required")
        if not account.auth_token or not account.ct0:
            return GraduationResult.fail("auth_token / ct0 missing")

        marker = f"gtest_{uuid.uuid4().hex[:12]}"
        tweet_text = f"テスト投稿 {marker}"

        tweet_id: Optional[str] = None
        async with XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url) as client:
            if not await client.verify():
                return GraduationResult.fail("token invalid")
            tweet_id = await client.tweet_returning_id(tweet_text)

        if not tweet_id:
            return GraduationResult.fail("tweet post failed")

        logger.info(f"[@{account.username}] graduation tweet posted id={tweet_id} marker={marker}")

        result = await self._wait_and_search(account.username, marker, tweet_id)

        if cleanup and tweet_id:
            try:
                async with XApiClient(account.auth_token, account.ct0, proxy_url=proxy_url) as client:
                    await client.delete_tweet(tweet_id)
            except Exception as e:
                logger.debug(f"[@{account.username}] graduation cleanup error: {e}")

        logger.info(
            f"[@{account.username}] graduation {'PASS' if result.passed else 'FAIL'} "
            f"rank={result.rank} score={result.rank_score:.1f} latency={result.search_latency_secs:.0f}s"
        )
        return result

    async def _wait_and_search(
        self, username: str, marker: str, tweet_id: str
    ) -> GraduationResult:
        start = time.monotonic()

        for interval in _POLL_INTERVALS:
            await asyncio.sleep(interval)
            elapsed = time.monotonic() - start
            if elapsed > _MAX_WAIT_SECS:
                break

            result = await self._search_once(marker, tweet_id)
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
        self, marker: str, tweet_id: str
    ) -> Optional[GraduationResult]:
        """
        ゲスト API で marker を検索。
        インデックスされていれば GraduationResult を返す。
        まだ空なら None。
        """
        token = await self._get_guest_token()
        if not token:
            return None

        try:
            url = _ADAPTIVE_SEARCH_URL.format(query=marker)
            headers = {
                "Authorization": f"Bearer {_GUEST_BEARER}",
                "x-guest-token": token,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "ja",
                "Referer": "https://twitter.com/search",
                "Accept": "application/json",
            }
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.get(url, headers=headers)

            if r.status_code != 200:
                logger.debug(f"[graduation] search HTTP {r.status_code}")
                return None

            data = r.json()
            tweet_ids = self._extract_ordered_ids(data)

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
                method="guest_adaptive",
                details={"total_results": len(tweet_ids), "marker": marker},
            )

        except Exception as e:
            logger.debug(f"[graduation] search error: {e}")
            return None

    def _extract_ordered_ids(self, data: dict) -> list[str]:
        """
        adaptive.json からツイートIDを出現順で抽出。
        v1 (globalObjects + timeline.instructions) と v2 (data.search_by_raw_query) に対応。
        """
        ids: list[str] = []

        # v1 形式
        if "globalObjects" in data:
            tweets_map: dict = data["globalObjects"].get("tweets", {})
            # timeline.instructions の addEntries で順序を取得
            for inst in data.get("timeline", {}).get("instructions", []):
                for entry in inst.get("addEntries", {}).get("entries", []):
                    entry_id: str = entry.get("entryId", "")
                    if entry_id.startswith("tweet-"):
                        tid = entry_id.split("-", 1)[1]
                        if tid in tweets_map:
                            ids.append(tid)
            if not ids:
                ids = list(tweets_map.keys())
            return ids

        # v2 形式
        try:
            instructions = (
                data["data"]["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"]
            )
            for inst in instructions:
                for entry in inst.get("entries", []):
                    item = entry.get("content", {}).get("itemContent", {})
                    result = item.get("tweet_results", {}).get("result", {})
                    tid = (
                        result.get("rest_id")
                        or result.get("legacy", {}).get("id_str")
                    )
                    if tid:
                        ids.append(str(tid))
        except (KeyError, TypeError):
            pass

        return ids

    async def _get_guest_token(self) -> Optional[str]:
        import time as _t
        if self._guest_token_cache:
            token, ts = self._guest_token_cache
            if _t.time() - ts < 3600:
                return token
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    _GUEST_TOKEN_URL,
                    headers={
                        "Authorization": f"Bearer {_GUEST_BEARER}",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                if r.status_code == 200:
                    token = r.json().get("guest_token")
                    if token:
                        self._guest_token_cache = (token, _t.time())
                        return token
        except Exception as e:
            logger.debug(f"[graduation] guest token error: {e}")
        return None
