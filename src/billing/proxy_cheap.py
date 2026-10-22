"""
proxy-cheap.com REST API クライアント。
LTC でチャージ済みの残高を消費してプロキシを購入する。

API ドキュメント: https://docs.proxy-cheap.com/
認証: Authorization: Basic <api_key>

注意: エンドポイントは proxy-cheap.com の公式ドキュメントで確認すること。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from loguru import logger

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL = True
except ImportError:
    HAS_CURL = False


BASE_URL = "https://api.proxy-cheap.com"


@dataclass
class ProxyCheapBalance:
    balance_usd: float
    currency: str = "USD"


class ProxyCheapClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._headers = {
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _get(self, path: str) -> dict:
        if not HAS_CURL:
            import urllib.request
            req = urllib.request.Request(
                BASE_URL + path, headers=self._headers
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        resp = curl_requests.get(
            BASE_URL + path,
            headers=self._headers,
            impersonate="chrome136",
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        if not HAS_CURL:
            import urllib.request
            data = json.dumps(body).encode()
            req = urllib.request.Request(
                BASE_URL + path, data=data, headers=self._headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        resp = curl_requests.post(
            BASE_URL + path,
            headers=self._headers,
            json=body,
            impersonate="chrome136",
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_balance(self) -> Optional[ProxyCheapBalance]:
        """残高を取得する。API キーが未設定なら None を返す。"""
        if not self.api_key:
            return None
        try:
            data = self._get("/proxies/stats")
            # レスポンス形式は公式ドキュメントで確認。
            # 典型的なフィールド: {"balance": 12.34, ...} または {"data": {"balance": ...}}
            balance = (
                data.get("balance")
                or data.get("data", {}).get("balance")
                or 0.0
            )
            return ProxyCheapBalance(balance_usd=float(balance))
        except Exception as e:
            logger.warning(f"[proxy-cheap] 残高取得失敗: {e}")
            return None

    async def purchase_residential_gb(self, gb: float) -> bool:
        """
        住宅プロキシ帯域を購入する。
        成功 True / 失敗 False を返す。

        注意: エンドポイントと body フィールドは公式 API ドキュメントで確認すること。
        """
        if not self.api_key:
            logger.warning("[proxy-cheap] api_key 未設定 — 自動購入スキップ")
            return False
        try:
            result = self._post(
                "/proxies/residential",
                {"gb": gb, "type": "residential"},
            )
            logger.success(f"[proxy-cheap] {gb}GB 購入完了: {result}")
            return True
        except Exception as e:
            logger.error(f"[proxy-cheap] 購入失敗: {e}")
            return False
