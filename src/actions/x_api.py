"""
X 内部 API クライアント — curl_cffi ベースのブラウザ不要アクション。

auth_token + ct0 がある垢は Chromium を起動せずに
like / follow / tweet / タイムライン取得を API 一発で実行できる。

x-client-transaction-id ヘッダーを自動生成（x.com から鍵を取得、キャッシュ）。

メモリ: Chromium(150-400MB) → Python プロセス分のみ(〜20MB)
プロキシ帯域: ページロード(1-5MB) → API 呼び出し(5-50KB)
"""
from __future__ import annotations

import asyncio
import json
import random
import re
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

# curl_cffi の Chrome バージョン（api_flow.py と揃える）
_IMPERSONATE = "chrome136"

# chrome136 フィンガープリントに合わせた Chrome UA（Safari UA は JA3 不一致でボット検出される）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


# ── Typed exceptions ────────────────────────────────────────────────────────

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


class XAlreadyDone(XApiError):
    """既にいいね/フォロー済み（139, 160, 162）。失敗ではない。"""


class XAutomated(XApiError):
    """自動化の疑いで弾かれた（226）。続行すると凍結に近づくのでその日は止める。"""


class XActionLimit(XApiError):
    """日次上限（185, 344, 161）。"""


# ── Error code → exception ───────────────────────────────────────────────────

_ERROR_CODE_MAP: dict[int, type[XApiError]] = {
    187: XDuplicateTweet,
    64:  XAccountSuspended,
    63:  XAccountSuspended,
    326: XAccountLocked,
    139: XAlreadyDone,
    160: XAlreadyDone,
    162: XAlreadyDone,
    226: XAutomated,
    185: XActionLimit,
    344: XActionLimit,
    161: XActionLimit,
    32:  XUnauthorized,
    215: XUnauthorized,
}


def _raise_for_errors(body: dict) -> None:
    for err in body.get("errors", []):
        code = err.get("code", 0)
        exc_cls = _ERROR_CODE_MAP.get(code)
        if exc_cls:
            raise exc_cls(err.get("message", ""), code)


def _check_response(r) -> None:
    """403 等でも本文のエラーコードを先に見る（「既にいいね済み」と「ロック/自動化判定」を区別する）。"""
    if r.status_code != 200:
        try:
            body = r.json()
        except Exception:
            body = {}
        if isinstance(body, dict):
            _raise_for_errors(body)
    _raise_for_status(r)


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


# ── Header helpers ───────────────────────────────────────────────────────────

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


# ── ClientTransaction cache ──────────────────────────────────────────────────

# Global per-proxy cache: proxy_url → ClientTransaction
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


# ── XApiClient ───────────────────────────────────────────────────────────────

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
        # 実行中に観測した「止めるべきサイン」: suspended / locked / automated / limited
        self.flags: set[str] = set()

    def _note(self, exc: Exception) -> None:
        if isinstance(exc, XAccountSuspended):
            self.flags.add("suspended")
        elif isinstance(exc, XAccountLocked):
            self.flags.add("locked")
        elif isinstance(exc, XAutomated):
            self.flags.add("automated")
        elif isinstance(exc, (XActionLimit, XRateLimited)):
            self.flags.add("limited")

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
            _check_response(r)
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
            _check_response(r)
            body = r.json()
            _raise_for_errors(body if isinstance(body, dict) else {})
            return body

    # ────────────────────────────────────────────
    # タイムライン取得（like 用のツイート ID 収集）
    # ────────────────────────────────────────────

    async def home_timeline(self, count: int = 20) -> list[str]:
        """ホームタイムラインからツイート ID を返す。GraphQL(HomeTimeline)優先、旧1.1は予備。"""
        try:
            ids = await self.home_timeline_gql(count)
            if ids:
                return ids
        except XUnauthorized:
            logger.warning("[XApi] home_timeline: token invalid")
            return []
        except Exception as e:
            logger.debug(f"[XApi] HomeTimeline(gql) failed, legacy fallback: {e}")
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
            try:
                if await self.like_gql(tweet_id):
                    return True
            except (XAlreadyDone, XAccountSuspended, XAccountLocked, XAutomated,
                    XActionLimit, XRateLimited, XUnauthorized):
                raise
            except Exception as e:
                logger.debug(f"[XApi] FavoriteTweet(gql) failed, legacy fallback: {e}")
            await self._post(f"{_BASE}/1.1/favorites/create.json", data={"id": tweet_id})
            return True
        except (XAlreadyDone, XDuplicateTweet):
            return True
        except (XAccountSuspended, XAccountLocked, XAutomated, XActionLimit, XRateLimited) as e:
            self._note(e)
            logger.warning(f"[XApi] like: {type(e).__name__}")
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
            if self.flags:
                break
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
        except XAlreadyDone:
            return False
        except (XAccountSuspended, XAccountLocked, XAutomated, XActionLimit, XRateLimited) as e:
            self._note(e)
            logger.warning(f"[XApi] follow: {type(e).__name__}")
            return False
        except XForbidden:
            logger.debug(f"[XApi] follow @{screen_name}: forbidden")
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
            if self.flags:
                break
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
        return bool(await self.post_tweet(text))

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

    async def verify_state(self) -> str:
        """ok / unauthorized / suspended / locked / error。凍結・ロックとトークン失効を区別する。"""
        try:
            body = await self._get(
                f"{_BASE}/1.1/account/verify_credentials.json",
                params={"skip_status": "true", "include_entities": "false"},
            )
            if isinstance(body, dict) and body.get("suspended"):
                return "suspended"
            return "ok"
        except XUnauthorized:
            return "unauthorized"
        except XAccountSuspended:
            return "suspended"
        except XAccountLocked:
            return "locked"
        except XForbidden:
            # 403 はロック(326)/凍結(64)の本文コードが無いと区別できない → 不明扱い
            return "error"
        except Exception:
            return "error"

    async def verify(self) -> bool:
        """auth_token が有効かどうか。"""
        return await self.verify_state() == "ok"

    # ────────────────────────────────────────────
    # 出口 IP（プロキシとの一致確認用）
    # ────────────────────────────────────────────

    async def exit_ip(self) -> Optional[str]:
        """この接続が実際に使っている出口 IP を返す（取得できなければ None）。
        プロキシ経由の AsyncSession と同じ経路を使うので、X に見えている IP と一致する。"""
        s = self._open_session
        for url in ("https://api.ipify.org?format=json", "https://api.my-ip.io/ip.json"):
            try:
                async with (nullcontext(s) if s else self._session()) as _s:
                    r = await (s or _s).get(url)
                if r.status_code == 410:
                    raise XProxyIpLost("proxy killswitch (HTTP 410): sticky IP lost", 410)
                if r.status_code == 200:
                    return r.json().get("ip")
            except XProxyIpLost:
                raise
            except Exception as e:
                logger.debug(f"[XApi] exit_ip {url} failed: {e}")
        return None

    # ────────────────────────────────────────────
    # 検索（卒業テスト / シャドウバン判定用）
    # ────────────────────────────────────────────

    async def _query_id(self, operation: str) -> str:
        """GraphQL の queryId を x.com の main.js から取得（キャッシュ）。失敗時は既知の値。
        X-Utility/src/shadowban.ts が使っている ID を既定値にしている。"""
        if operation in _query_id_cache:
            return _query_id_cache[operation]
        if _query_id_cache.get("_scanned"):
            return _QUERY_ID_FALLBACK[operation]
        try:
            s = self._open_session
            async with (nullcontext(s) if s else self._session()) as _s:
                s = s or _s
                home = await s.get("https://x.com", headers={"User-Agent": _USER_AGENT})
                m = re.search(r'https://abs\.twimg\.com/responsive-web/client-web[^"\']*?/main\.[0-9a-z]+\.js', home.text)
                if m:
                    js = (await s.get(m.group(0), headers={"User-Agent": _USER_AGENT})).text
                    for op in _QUERY_ID_FALLBACK:
                        q = re.search(r'queryId:"([\w-]+)",operationName:"%s"' % op, js)
                        if q:
                            _query_id_cache[op] = q.group(1)
                    _query_id_cache["_scanned"] = "1"
        except Exception as e:
            logger.debug(f"[XApi] queryId lookup failed: {e}")
        return _query_id_cache.setdefault(operation, _QUERY_ID_FALLBACK[operation])

    async def _graphql(self, operation: str, variables: dict, features: dict, toggles: Optional[dict] = None) -> dict:
        qid = await self._query_id(operation)
        features = dict(features)
        for _ in range(3):  # 「features が足りない」エラーには不足分を False で足して再試行
            params = {"variables": json.dumps(variables, separators=(",", ":")),
                      "features": json.dumps(features, separators=(",", ":"))}
            if toggles:
                params["fieldToggles"] = json.dumps(toggles, separators=(",", ":"))
            body = await self._get(f"{_BASE}/graphql/{qid}/{operation}", params=params)
            missing = _missing_features(body)
            if not missing:
                return body or {}
            features.update({k: False for k in missing})
        return {}

    async def search_timeline(self, query: str, product: str = "Top", count: int = 20) -> list[dict]:
        """
        GraphQL SearchTimeline。product: Top / Latest / People / Media。
        戻り値（表示順）: People なら {"screen_name","name"}、それ以外は
        {"id","text","screen_name"}。
        """
        body = await self._graphql(
            "SearchTimeline",
            {"rawQuery": query, "count": count, "querySource": "typed_query", "product": product},
            _SEARCH_FEATURES, _SEARCH_FIELD_TOGGLES,
        )
        return parse_search_results(body, people=(product == "People"))

    async def typeahead_users(self, query: str) -> list[str]:
        """検索ボックスのサジェスト（typeahead）に出たユーザーの screen_name 一覧。"""
        body = await self._get(
            f"{_BASE}/1.1/search/typeahead.json",
            params={"include_ext_is_blue_verified": "1", "include_ext_verified_type": "1",
                    "include_ext_profile_image_shape": "1", "q": query,
                    "src": "search_box", "result_type": "events,users,topics,lists"},
        )
        users = body.get("users", []) if isinstance(body, dict) else []
        return [u.get("screen_name", "") for u in users]

    async def user_by_screen_name(self, screen_name: str) -> dict:
        """
        UserByScreenName。戻り値:
          {"state": "ok"|"not_found"|"suspended"|"protected",
           "screen_name","name","tweet_count"}
        """
        body = await self._graphql(
            "UserByScreenName",
            {"screen_name": screen_name, "withSafetyModeUserFields": True, "withGrokTranslatedBio": False},
            _USER_FEATURES, {"withAuxiliaryUserLabels": False, "withPayments": False},
        )
        user = (body.get("data") or {}).get("user")
        if not user:
            return {"state": "not_found", "screen_name": screen_name, "name": screen_name, "tweet_count": None}
        res = user.get("result") or {}
        if res.get("__typename") != "User":
            return {"state": "suspended", "screen_name": screen_name, "name": screen_name, "tweet_count": None}
        legacy = res.get("legacy") or {}
        core = res.get("core") or {}
        sn = core.get("screen_name") or legacy.get("screen_name") or screen_name
        name = core.get("name") or legacy.get("name") or sn
        try:
            cnt = int(legacy.get("statuses_count"))
        except (TypeError, ValueError):
            cnt = None
        prot = legacy.get("protected") or (res.get("privacy") or {}).get("protected")
        return {"state": "protected" if prot else "ok", "screen_name": sn, "name": name, "tweet_count": cnt}

    async def _graphql_post(self, operation: str, variables: dict, features: dict) -> dict:
        """GraphQL の書き込み系（POST JSON）。queryId 失効(404)時は取り直して1回だけ再試行。"""
        features = dict(features)
        for attempt in range(4):
            qid = await self._query_id(operation)
            url = f"{_BASE}/graphql/{qid}/{operation}"
            s = self._open_session
            async with (nullcontext(s) if s else self._session()) as _s:
                s = s or _s
                tid = await self._tid(s, "POST", url)
                r = await s.post(
                    url,
                    json={"variables": variables, "features": features, "queryId": qid},
                    headers={**_base_headers(self.ct0, tid), "Content-Type": "application/json"},
                    cookies=_cookies(self.auth_token, self.ct0),
                )
            if r.status_code == 404 and attempt == 0:
                _query_id_cache.pop(operation, None)  # 古い ID → 次回 main.js から取り直す
                _query_id_cache.pop("_scanned", None)
                continue
            if r.status_code == 403 and tid:
                _invalidate_transaction(self._proxy_key)
            _check_response(r)
            body = r.json()
            missing = _missing_features(body)
            if missing:
                features.update({k: False for k in missing})
                continue
            _raise_for_errors(body if isinstance(body, dict) else {})
            return body
        raise XApiError(f"{operation}: failed after retries")

    async def like_gql(self, tweet_id: str) -> bool:
        body = await self._graphql_post("FavoriteTweet", {"tweet_id": tweet_id}, _SEARCH_FEATURES)
        return (body.get("data") or {}).get("favorite_tweet") == "Done"

    async def retweet(self, tweet_id: str) -> bool:
        try:
            body = await self._graphql_post(
                "CreateRetweet", {"tweet_id": tweet_id, "dark_request": False}, _SEARCH_FEATURES)
            return bool(next(iter(_walk(body, "retweet_results")), None))
        except XAlreadyDone:
            return False
        except (XAccountSuspended, XAccountLocked, XAutomated, XActionLimit, XRateLimited) as e:
            self._note(e)
            logger.warning(f"[XApi] retweet: {type(e).__name__}")
        except Exception as e:
            logger.debug(f"[XApi] retweet error: {e}")
        return False

    async def home_timeline_gql(self, count: int = 20) -> list[str]:
        body = await self._graphql(
            "HomeTimeline",
            {"count": count, "includePromotedContent": False, "latestControlAvailable": True,
             "requestContext": "launch", "withCommunity": True},
            _SEARCH_FEATURES,
        )
        ids: list[str] = []
        for e in _walk(body, "entries"):
            if not isinstance(e, list):
                continue
            for ent in e:
                if str(ent.get("entryId", "")).startswith("tweet-"):
                    res = next(iter(_walk(ent, "tweet_results")), {}).get("result", {}) or {}
                    res = res.get("tweet", res)
                    if res.get("rest_id"):
                        ids.append(str(res["rest_id"]))
        return ids

    async def _create_tweet_gql(self, text: str) -> Optional[str]:
        body = await self._graphql_post(
            "CreateTweet",
            {"tweet_text": text, "dark_request": False,
             "media": {"media_entities": [], "possibly_sensitive": False},
             "semantic_annotation_ids": []},
            _SEARCH_FEATURES,
        )
        res = next(iter(_walk(body, "tweet_results")), {}).get("result", {}) or {}
        return str(res.get("rest_id") or "") or None

    async def post_tweet(self, text: str) -> Optional[str]:
        """ツイートして tweet id を返す（失敗は None）。GraphQL(CreateTweet)を優先、旧1.1は最後の手段。"""
        try:
            try:
                tid = await self._create_tweet_gql(text)
                if tid:
                    return tid
            except (XDuplicateTweet, XAccountSuspended, XAccountLocked, XAutomated,
                    XActionLimit, XRateLimited, XUnauthorized):
                raise
            except Exception as e:
                logger.debug(f"[XApi] CreateTweet(gql) failed, legacy fallback: {e}")
            body = await self._post(
                f"{_BASE}/1.1/statuses/update.json",
                data={"status": text, "tweet_mode": "extended"},
            )
            return str(body.get("id_str") or body.get("id") or "") or None
        except XDuplicateTweet:
            logger.warning("[XApi] duplicate tweet, skipping")
        except (XAccountSuspended, XAccountLocked, XAutomated, XActionLimit, XRateLimited) as e:
            self._note(e)
            logger.warning(f"[XApi] post_tweet: {type(e).__name__}")
        except Exception as e:
            logger.debug(f"[XApi] post_tweet error: {e}")
        return None


class XProxyIpLost(XApiError):
    """IPRoyal の killswitch（HTTP 410）: スティッキー IP が失われた。"""


# X-Utility/src/shadowban.ts が観測している現行のID（古くなったら main.js から自動取得）
_QUERY_ID_FALLBACK = {
    "SearchTimeline": "hyPfJYJ_XAtDYoslQc-Rgg",
    "UserByScreenName": "Gb-d6r0vxPOADdG62OEBpQ",
    # twikit (d60/twikit) の値。古くなり得るので main.js から取得できればそちらを使う
    "FavoriteTweet": "lI07N6Otwv1PhnEgXILM7A",
    "CreateTweet": "SiM_cAu83R0wnrpmKQQSEw",
    "CreateRetweet": "ojPdsZsimiJrUGLR1sjUtA",
    "HomeTimeline": "-X_hcgQzmHGl29-UXxz4sw",
}
_query_id_cache: dict[str, str] = {}

_USER_FEATURES = {
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "hidden_profile_subscriptions_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "subscriptions_feature_can_gift_premium": True,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
}

_SEARCH_FEATURES = {
    "articles_preview_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "post_ctas_fetch_enabled": False,
    "premium_content_api_read_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_annotations_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "rweb_cashtags_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "rweb_video_screen_enabled": False,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "verified_phone_label_enabled": False,
    "view_counts_everywhere_api_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
}

_SEARCH_FIELD_TOGGLES = {
    "withArticlePlainText": False,
    "withArticleRichContentState": True,
    "withArticleSummaryText": False,
    "withArticleVoiceOver": False,
    "withAuxiliaryUserLabels": False,
    "withDisallowedReplyControls": False,
    "withGrokAnalyze": False,
    "withPayments": False,
}


def _missing_features(body) -> list[str]:
    """`The following features cannot be null: a, b` 形式のエラーから不足 feature 名を抜く。"""
    out: list[str] = []
    for err in (body or {}).get("errors", []) if isinstance(body, dict) else []:
        m = re.search(r"features cannot be null: ([\w, ]+)", err.get("message", ""))
        if m:
            out += [x.strip() for x in m.group(1).split(",") if x.strip()]
    return out


def _walk(node, key: str):
    """ネストした dict/list から key を持つ値を全て順に返す。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key:
                yield v
            yield from _walk(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)


def parse_search_results(body: dict, people: bool = False) -> list[dict]:
    """SearchTimeline の応答を、画面に出る順の簡易リストにする。"""
    instructions = next(iter(_walk(body, "instructions")), None) or []
    entries: list = []
    for ins in instructions:
        if isinstance(ins, dict) and isinstance(ins.get("entries"), list):
            entries += ins["entries"]
    out: list[dict] = []
    for e in entries:
        eid = e.get("entryId", "")
        if people:
            if not eid.startswith("user-"):
                continue
            res = next(iter(_walk(e, "result")), None) or {}
            core = res.get("core", {}).get("user_results", {}).get("result", res)
            legacy = res.get("legacy", {}) or core.get("legacy", {})
            sn = (res.get("core", {}) or {}).get("screen_name") or legacy.get("screen_name")
            name = (res.get("core", {}) or {}).get("name") or legacy.get("name")
            if sn:
                out.append({"screen_name": sn, "name": name or ""})
        else:
            if not eid.startswith("tweet-"):
                continue
            res = next(iter(_walk(e, "tweet_results")), {}).get("result", {}) or {}
            res = res.get("tweet", res)  # TweetWithVisibilityResults
            legacy = res.get("legacy", {})
            if not legacy:
                continue
            user = res.get("core", {}).get("user_results", {}).get("result", {})
            sn = user.get("core", {}).get("screen_name") or user.get("legacy", {}).get("screen_name", "")
            out.append({"id": str(res.get("rest_id") or legacy.get("id_str", "")),
                        "text": legacy.get("full_text", ""), "screen_name": sn})
    return out
