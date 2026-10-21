"""
X 内部 API クライアント — curl_cffi ベースのブラウザ不要アクション。

auth_token + ct0 がある垢は Chromium を起動せずに
like / follow / tweet / タイムライン取得を API 一発で実行できる。

メモリ: Chromium(150-400MB) → Python プロセス分のみ(〜20MB)
プロキシ帯域: ページロード(1-5MB) → API 呼び出し(5-50KB)
"""
from __future__ import annotations

import random
import re
from typing import Optional

from loguru import logger

try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

# X ウェブクライアント用の公開 Bearer トークン（変更なし）
_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_BASE = "https://x.com/i/api"

# curl_cffi の Chrome バージョン（api_flow.py と揃える）
_IMPERSONATE = "chrome136"


def _headers(ct0: str) -> dict:
    return {
        "Authorization": f"Bearer {_BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "ja",
        "Accept": "*/*",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    }


def _cookies(auth_token: str, ct0: str) -> dict:
    return {"auth_token": auth_token, "ct0": ct0}


class XApiClient:
    """
    X の内部 REST v1.1 / GraphQL API を叩く軽量クライアント。
    一切ブラウザを使わない。
    """

    def __init__(
        self,
        auth_token: str,
        ct0: str,
        proxy_url: Optional[str] = None,
    ):
        if not HAS_CURL_CFFI:
            raise RuntimeError("curl_cffi が必要です: pip install curl-cffi")
        self.auth_token = auth_token
        self.ct0 = ct0
        self.proxy_url = proxy_url

    def _session(self) -> AsyncSession:
        kwargs: dict = {"impersonate": _IMPERSONATE, "timeout": 20}
        if self.proxy_url:
            kwargs["proxies"] = {"https": self.proxy_url, "http": self.proxy_url}
        return AsyncSession(**kwargs)

    # ────────────────────────────────────────────
    # タイムライン取得（like 用のツイート ID 収集）
    # ────────────────────────────────────────────

    async def home_timeline(self, count: int = 20) -> list[str]:
        """ホームタイムラインからツイート ID を返す。"""
        async with self._session() as s:
            try:
                r = await s.get(
                    f"{_BASE}/1.1/statuses/home_timeline.json",
                    params={"count": count, "include_entities": "false", "trim_user": "true"},
                    headers=_headers(self.ct0),
                    cookies=_cookies(self.auth_token, self.ct0),
                )
                if r.status_code == 200:
                    tweets = r.json()
                    return [str(t["id"]) for t in tweets if "id" in t]
                logger.debug(f"[XApi] timeline status={r.status_code}")
            except Exception as e:
                logger.debug(f"[XApi] home_timeline error: {e}")
        return []

    # ────────────────────────────────────────────
    # いいね
    # ────────────────────────────────────────────

    async def like(self, tweet_id: str) -> bool:
        async with self._session() as s:
            try:
                r = await s.post(
                    f"{_BASE}/1.1/favorites/create.json",
                    data={"id": tweet_id},
                    headers={**_headers(self.ct0), "Content-Type": "application/x-www-form-urlencoded"},
                    cookies=_cookies(self.auth_token, self.ct0),
                )
                ok = r.status_code in (200, 403)  # 403 = already liked
                if not ok:
                    logger.debug(f"[XApi] like tweet={tweet_id} status={r.status_code}")
                return ok
            except Exception as e:
                logger.debug(f"[XApi] like error: {e}")
                return False

    async def like_random_from_timeline(self, count: int = 3) -> int:
        """タイムラインからランダムに count 件いいね。成功数を返す。"""
        tweet_ids = await self.home_timeline(count=min(count * 4, 40))
        if not tweet_ids:
            return 0
        random.shuffle(tweet_ids)
        liked = 0
        for tid in tweet_ids[:count]:
            if await self.like(tid):
                liked += 1
            # API レート制限回避のための人間らしい間隔
            import asyncio
            await asyncio.sleep(random.uniform(2.0, 6.0))
        return liked

    # ────────────────────────────────────────────
    # フォロー
    # ────────────────────────────────────────────

    async def follow(self, screen_name: str) -> bool:
        async with self._session() as s:
            try:
                r = await s.post(
                    f"{_BASE}/1.1/friendships/create.json",
                    data={"screen_name": screen_name.lstrip("@"), "skip_status": "true"},
                    headers={**_headers(self.ct0), "Content-Type": "application/x-www-form-urlencoded"},
                    cookies=_cookies(self.auth_token, self.ct0),
                )
                ok = r.status_code == 200
                if not ok:
                    logger.debug(f"[XApi] follow @{screen_name} status={r.status_code}")
                return ok
            except Exception as e:
                logger.debug(f"[XApi] follow error: {e}")
                return False

    async def follow_suggestions(self, count: int = 2) -> int:
        """X のおすすめユーザーをフォロー。成功数を返す。"""
        async with self._session() as s:
            try:
                r = await s.get(
                    f"{_BASE}/1.1/users/recommendations.json",
                    params={"count": 10, "include_entities": "false"},
                    headers=_headers(self.ct0),
                    cookies=_cookies(self.auth_token, self.ct0),
                )
                if r.status_code != 200:
                    return 0
                users = r.json()
                if not isinstance(users, list):
                    users = users.get("users", [])
            except Exception as e:
                logger.debug(f"[XApi] recommendations error: {e}")
                return 0

        random.shuffle(users)
        followed = 0
        import asyncio
        for user in users[:count]:
            sn = user.get("screen_name") or user.get("legacy", {}).get("screen_name", "")
            if not sn:
                continue
            if await self.follow(sn):
                followed += 1
                await asyncio.sleep(random.uniform(3.0, 8.0))
            if followed >= count:
                break
        return followed

    # ────────────────────────────────────────────
    # ツイート
    # ────────────────────────────────────────────

    async def tweet(self, text: str) -> bool:
        async with self._session() as s:
            try:
                r = await s.post(
                    f"{_BASE}/1.1/statuses/update.json",
                    data={"status": text, "tweet_mode": "extended"},
                    headers={**_headers(self.ct0), "Content-Type": "application/x-www-form-urlencoded"},
                    cookies=_cookies(self.auth_token, self.ct0),
                )
                ok = r.status_code == 200
                if not ok:
                    logger.debug(f"[XApi] tweet status={r.status_code} body={r.text[:80]}")
                return ok
            except Exception as e:
                logger.debug(f"[XApi] tweet error: {e}")
                return False

    # ────────────────────────────────────────────
    # ブラウズ（タイムライン取得のみ、何もしない）
    # ────────────────────────────────────────────

    async def browse(self, count: int = 10) -> bool:
        """OBSERVE フェーズ: タイムラインを取得するだけ（いいねなし）。"""
        import asyncio
        ids = await self.home_timeline(count=count)
        if ids:
            # 読んでいる風に間隔を置く
            await asyncio.sleep(random.uniform(5.0, 15.0))
        return bool(ids)

    # ────────────────────────────────────────────
    # トークン有効確認
    # ────────────────────────────────────────────

    async def verify(self) -> bool:
        """auth_token が有効かどうかを確認する。"""
        async with self._session() as s:
            try:
                r = await s.get(
                    f"{_BASE}/1.1/account/verify_credentials.json",
                    params={"skip_status": "true", "include_entities": "false"},
                    headers=_headers(self.ct0),
                    cookies=_cookies(self.auth_token, self.ct0),
                )
                return r.status_code == 200
            except Exception:
                return False
