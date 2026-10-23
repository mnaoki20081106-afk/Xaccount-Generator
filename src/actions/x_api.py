"""
X 内部 API クライアント — curl_cffi ベースのブラウザ不要アクション。

auth_token + ct0 がある垃は Chromium を起動せずに
like / follow / tweet / タイムライン取得 / GraphQL 検索 を API 一発で実行できる。

x-client-transaction-id ヘッダーを自動生成（x.com から鍵を取得、キャッシュ）。

メモリ: Chromium(150-400MB) → Python プロセス分のみ(〜20MB)
プロキシ帯域: ページロード(1-5MB) → API 呼び出し(5-50KB)
"""
from __future__ import annotations

import asyncio
import json
import random
import re
from collections import defaultdict
from contextlib import nullcontext
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

try:
    from curl_cffi.requests import AsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False

try:
    from ..x_client_transaction import ClientTransaction
    HAS_CLIENT_TRANSACTION = True
except ImportError:
    HAS_CLIENT_TRANSACTION = False

# X ウェブクライアント用の公開 Bearer トークン（変更なし）
_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_BASE = "https://x.com/i/api"
_GQL_URL = f"{_BASE}/graphql"

# vladkens/twscrape 参照。SearchTimeline GraphQL operation ID。
# 変更された場合は scripts/update-gql-ops.py で更新が必要。
_GQL_SEARCH_OP = "BYppgupFFS2yqVmmRNjlsw/SearchTimeline"

# curl_cffi の Chrome バージョン（api_flow.py と揃える）
_IMPERSONATE = "chrome136"

# chrome136 フィンガープリントに合わせた Chrome UA
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# vladkens/twscrape GQL_FEATURES より引用。欠けると (336) エラーになる場合がある。
_GQL_FEATURES = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_grok_imagine_annotation_enabled": False,
    "responsive_web_media_download_video_enabled": False,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_awards_web_tipping_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "tweet_with_visibility_results_prefer_gql_media_interstitial_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_jetfuel_frame": False,
    "rweb_video_screen_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
}

_ENTRY_TWEET_RE = re.compile(r"(?:^|-)tweet-(\d+)(?:-|$)")


def _collect_entry_ids(obj: dict, out: list) -> None:
    """GraphQL レスポンスから entryId を表示順に収集。"""
    if not isinstance(obj, dict):
        return
    if isinstance(obj.get("entryId"), str):
        out.append(obj["entryId"])
    for v in obj.values():
        if isinstance(v, dict):
            _collect_entry_ids(v, out)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    _collect_entry_ids(item, out)


def _extract_tweet_ids_from_gql(data: dict) -> list[str]:
    """GraphQL SearchTimeline レスポンスからツイートIDを表示順で抽出。"""
    entry_ids: list[str] = []
    _collect_entry_ids(data, entry_ids)
    ids: list[str] = []
    seen: set[str] = set()
    for eid in entry_ids:
        m = _ENTRY_TWEET_RE.search(eid)
        if m:
            tid = m.group(1)
            if tid not in seen:
                seen.add(tid)
                ids.append(tid)
    return ids


# ── Typed exceptions ─────────────────────────────────────────────────────

class XApiError(Exception):
    """Base class for X API errors."""
    def __init__(self, message="", status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class XUnauthorized(XApiError):
    """auth_token / ct0 が無効（401）。"""


class XForbidden(XApiError):
    """アクセス拒否 / アカウントロック（403）。"""


class XRateLimited(XApiError):
    """レート制限（429）。"""
    def __init__(self, message="", reset_at: int = 0):
        super().__init__(message, 429)
        self.reset_at = reset_at


class XAccountSuspended(XApiError):
    """アカウント凍結。"""


class XAccountLocked(XApiError):
    """アカウントロック（Arkose チャレンジ）。"""


class XDuplicateTweet(XApiError):
    """重複ツイート（187）。"""


# ── Error code → exception ───────────────────────────────────────────────

_ERROR_CODE_MAP: dict[int, type[XApiError]] = {
    187: XDuplicateTweet,
    64:  XAccountSuspended,
    326: XAccountLocked,
}


def _raise_for_errors(body: dict) -> None:
    for err in body.get("errors", []):
        code = err.get("code", 0)
        exc_cls = _ERROR_CODE_MAP.get(code)
        if exc_cls:
            raise exc_cls(err.get("message", ""), code)


def _raise_for_status(r) -> None:
    sc = r.status_code
    if sc == 200:
        return
    if sc == 401:
        raise XUnauthorized(f"HTTP 401", sc)
    if sc == 403:
        raise XForbidden(f"HTTP 403", sc)
    if sc == 429:
        reset = int(r.headers.get("x-rate-limit-reset", 0))
        raise XRateLimited(f"HTTP 429", reset)
    if sc >= 500:
        raise XApiError(f"HTTP {sc} server error", sc)


# ── Header helpers ──────────────────────────────────────────────────

def _base_headers(ct0: str, transaction_id: Optional[str] = None) -> dict:
    h = {
        "Authorization": f"Bearer {_BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "ja",
        "User-Agent": _USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    }
    if transaction_id:
        h["x-client-transaction-id"] = transaction_id
    return h


def _cookies(auth_token: str, ct0: str) -> dict:
    return {"auth_token": auth_token, "ct0": ct0}


# ── ClientTransaction cache ─────────────────────────────────────────────

_transaction_cache: dict[str, ClientTransaction] = {}
_transaction_lock = asyncio.Lock()


async def _get_transaction(session, ct0: str, proxy_key: str) -> Optional[ClientTransaction]:
    """Lazily initialize and cache ClientTransaction by proxy key."""
    if not HAS_CLIENT_TRANSACTION:
        return None
    async with _transaction_lock:
        ct = _transaction_cache.get(proxy_key)
        if ct and ct.home_page_response:
            return ct
        ct = ClientTransaction()
        try:
            headers = {
                "Authorization": f"Bearer {_BEARER}",
                "x-csrf-token": ct0,
                "Accept-Language": "ja-JP,ja;q=0.9,en;q=0.8",
                "User-Agent": _USER_AGENT,
            }
            await ct.init(session, headers)
            _transaction_cache[proxy_key] = ct
            logger.debug(f"[XApi] ClientTransaction initialized proxy={proxy_key[:8]}")
        except Exception as e:
            logger.warning(f"[XApi] ClientTransaction init failed: {e} — sending without x-client-transaction-id")
            return None
    return ct


def _invalidate_transaction(proxy_key: str) -> None:
    _transaction_cache.pop(proxy_key, None)


# ── XApiClient ─────────────────────────────────────────────────────────

class XApiClient:
    """
    X の内部 REST v1.1 / GraphQL API を叩く軽量クライアント。
    一切ブラウザを使わない。x-client-transaction-id を自動付与。
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
        self._proxy_key = proxy_url or "direct"
        self._open_session: Optional[AsyncSession] = None

    def _session_kwargs(self) -> dict:
        kwargs: dict = {"impersonate": _IMPERSONATE, "timeout": 20}
        if self.proxy_url:
            kwargs["proxies"] = {"https": self.proxy_url, "http": self.proxy_url}
        return kwargs

    def _session(self) -> AsyncSession:
        return AsyncSession(**self._session_kwargs())

    async def __aenter__(self) -> "XApiClient":
        """Open a persistent session reused across all calls (avoids repeated TLS handshakes)."""
        self._open_session = AsyncSession(**self._session_kwargs())
        return self

    async def __aexit__(self, *_) -> None:
        if self._open_session:
            await self._open_session.close()
            self._open_session = None

    async def _tid(self, session, method: str, url: str) -> Optional[str]:
        """Generate x-client-transaction-id for the given request."""
        ct = await _get_transaction(session, self.ct0, self._proxy_key)
        if not ct:
            return None
        path = urlparse(url).path
        try:
            return ct.generate_transaction_id(method=method.upper(), path=path)
        except Exception as e:
            logger.debug(f"[XApi] generate_transaction_id error: {e}")
            return None

    async def _get(self, url: str, params: dict = None) -> Optional[dict]:
        s = self._open_session
        async with (nullcontext(s) if s else self._session()) as _s:
            s = s or _s
            tid = await self._tid(s, "GET", url)
            r = await s.get(
                url,
                params=params,
                headers=_base_headers(self.ct0, tid),
                cookies=_cookies(self.auth_token, self.ct0),
            )
            if r.status_code == 403 and tid:
                _invalidate_transaction(self._proxy_key)
            _raise_for_status(r)
            body = r.json()
            _raise_for_errors(body if isinstance(body, dict) else {})
            return body

    async def _post(self, url: str, data: dict = None) -> Optional[dict]:
        s = self._open_session
        async with (nullcontext(s) if s else self._session()) as _s:
            s = s or _s
            tid = await self._tid(s, "POST", url)
            h = {**_base_headers(self.ct0, tid), "Content-Type": "application/x-www-form-urlencoded"}
            r = await s.post(
                url,
                data=data,
                headers=h,
                cookies=_cookies(self.auth_token, self.ct0),
            )
            if r.status_code == 403 and tid:
                _invalidate_transaction(self._proxy_key)
            _raise_for_status(r)
            body = r.json()
            _raise_for_errors(body if isinstance(body, dict) else {})
            return body

    # ────────────────────────────────────────────
    # GraphQL 検索
    # ────────────────────────────────────────────

    async def search_live_ids(self, query: str, count: int = 20) -> list[str]:
        """GraphQL SearchTimeline でライブ検索。ツイートIDリストを表示順で返す。"""
        variables = {
            "rawQuery": query,
            "count": count,
            "product": "Latest",
            "querySource": "typed_query",
        }
        params = {
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(_GQL_FEATURES, separators=(",", ":")),
            "fieldToggles": json.dumps({"withArticleRichContentState": False}, separators=(",", ":")),
        }
        try:
            body = await self._get(f"{_GQL_URL}/{_GQL_SEARCH_OP}", params=params)
            return _extract_tweet_ids_from_gql(body or {})
        except XRateLimited as e:
            logger.warning(f"[XApi] search_live_ids: rate limited reset={e.reset_at}")
        except XUnauthorized:
            logger.warning("[XApi] search_live_ids: unauthorized")
        except Exception as e:
            logger.debug(f"[XApi] search_live_ids error: {e}")
        return []

    # ────────────────────────────────────────────
    # タイムライン取得（like 用のツイート ID 収集）
    # ────────────────────────────────────────────

    async def home_timeline(self, count: int = 20) -> list[str]:
        """ホームタイムラインからツイート ID を返す。"""
        try:
            tweets = await self._get(
                f"{_BASE}/1.1/statuses/home_timeline.json",
                params={"count": count, "include_entities": "false", "trim_user": "true"},
            )
            if isinstance(tweets, list):
                return [str(t["id"]) for t in tweets if "id" in t]
        except XUnauthorized:
            logger.warning("[XApi] home_timeline: token invalid")
        except XRateLimited as e:
            logger.warning(f"[XApi] home_timeline: rate limited, reset={e.reset_at}")
        except Exception as e:
            logger.debug(f"[XApi] home_timeline error: {e}")
        return []

    # ────────────────────────────────────────────
    # いいね
    # ────────────────────────────────────────────

    async def like(self, tweet_id: str) -> bool:
        try:
            await self._post(f"{_BASE}/1.1/favorites/create.json", data={"id": tweet_id})
            return True
        except XForbidden:
            return True  # already liked
        except XDuplicateTweet:
            return True
        except XAccountSuspended:
            logger.error("[XApi] account suspended")
            return False
        except XRateLimited as e:
            logger.warning(f"[XApi] like: rate limited, reset={e.reset_at}")
            return False
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
            await asyncio.sleep(random.uniform(2.0, 6.0))
        return liked

    # ────────────────────────────────────────────
    # フォロー
    # ────────────────────────────────────────────

    async def follow(self, screen_name: str) -> bool:
        try:
            await self._post(
                f"{_BASE}/1.1/friendships/create.json",
                data={"screen_name": screen_name.lstrip("@"), "skip_status": "true"},
            )
            return True
        except XForbidden:
            logger.debug(f"[XApi] follow @{screen_name}: forbidden (already following?)")
            return False
        except XAccountSuspended:
            logger.error("[XApi] account suspended")
            return False
        except XRateLimited as e:
            logger.warning(f"[XApi] follow: rate limited, reset={e.reset_at}")
            return False
        except Exception as e:
            logger.debug(f"[XApi] follow error: {e}")
            return False

    async def follow_suggestions(self, count: int = 2) -> int:
        """X のおすすめユーザーをフォロー。成功数を返す。"""
        try:
            body = await self._get(
                f"{_BASE}/1.1/users/recommendations.json",
                params={"count": 10, "include_entities": "false"},
            )
            users = body if isinstance(body, list) else body.get("users", [])
        except Exception as e:
            logger.debug(f"[XApi] recommendations error: {e}")
            return 0

        random.shuffle(users)
        followed = 0
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
        try:
            await self._post(
                f"{_BASE}/1.1/statuses/update.json",
                data={"status": text, "tweet_mode": "extended"},
            )
            return True
        except XDuplicateTweet:
            logger.warning("[XApi] duplicate tweet, skipping")
            return False
        except XAccountSuspended:
            logger.error("[XApi] account suspended")
            return False
        except XRateLimited as e:
            logger.warning(f"[XApi] tweet: rate limited, reset={e.reset_at}")
            return False
        except Exception as e:
            logger.debug(f"[XApi] tweet error: {e}")
            return False

    async def tweet_returning_id(self, text: str) -> Optional[str]:
        """ツイートを投稿し、ツイートIDを返す。失敗時はNone。"""
        try:
            body = await self._post(
                f"{_BASE}/1.1/statuses/update.json",
                data={"status": text, "tweet_mode": "extended"},
            )
            if isinstance(body, dict):
                tid = str(body.get("id_str") or body.get("id") or "")
                return tid if tid else None
        except XDuplicateTweet:
            logger.warning("[XApi] duplicate tweet")
        except XAccountSuspended:
            logger.error("[XApi] account suspended")
        except XRateLimited as e:
            logger.warning(f"[XApi] tweet: rate limited, reset={e.reset_at}")
        except Exception as e:
            logger.debug(f"[XApi] tweet_returning_id error: {e}")
        return None

    async def delete_tweet(self, tweet_id: str) -> bool:
        """ツイートを削除する。"""
        try:
            await self._post(
                f"{_BASE}/1.1/statuses/destroy/{tweet_id}.json",
                data={"id": tweet_id, "trim_user": "true"},
            )
            return True
        except Exception as e:
            logger.debug(f"[XApi] delete_tweet {tweet_id} error: {e}")
        return False

    # ────────────────────────────────────────────
    # ブラウズ（タイムライン取得のみ、何もしない）
    # ────────────────────────────────────────────

    async def browse(self, count: int = 10) -> bool:
        """OBSERVE フェーズ: タイムラインを取得するだけ（いいねなし）。"""
        ids = await self.home_timeline(count=count)
        if ids:
            await asyncio.sleep(random.uniform(5.0, 15.0))
        return bool(ids)

    # ────────────────────────────────────────────
    # トークン有効確認
    # ────────────────────────────────────────────

    async def verify(self) -> bool:
        """auth_token が有効かどうかを確認する。"""
        try:
            await self._get(
                f"{_BASE}/1.1/account/verify_credentials.json",
                params={"skip_status": "true", "include_entities": "false"},
            )
            return True
        except XUnauthorized:
            return False
        except XAccountSuspended:
            return False
        except XAccountLocked:
            return False
        except Exception:
            return False
