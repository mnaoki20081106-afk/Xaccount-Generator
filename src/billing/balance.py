"""
各サービスの残高を監視し、不足時に Exodus から LTC をチャージするよう警告する。

5sim:          API で残高確認 → 不足でアラート
proxy-cheap:   ProxyCheapClient で残高確認 → 不足でアラート
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from .proxy_cheap import ProxyCheapClient

try:
    from curl_cffi import requests as curl_requests
    HAS_CURL = True
except ImportError:
    HAS_CURL = False


@dataclass
class ServiceBalance:
    name: str
    balance_usd: float
    ltc_deposit_address: str
    low_threshold_usd: float
    reachable: bool = True


class BalanceMonitor:
    def __init__(self, config):
        self._cfg = config.billing
        self._sms_cfg = config.sms
        self._proxy_client = ProxyCheapClient(self._cfg.proxy_cheap_api_key)

    async def check_all(self) -> list[ServiceBalance]:
        """全サービスの残高をチェックしてリスト返却。不足があればログに警告。"""
        results = []

        sms_bal = await self._check_5sim()
        if sms_bal:
            results.append(sms_bal)
            self._alert_if_low(sms_bal)

        proxy_bal = await self._check_proxy_cheap()
        if proxy_bal:
            results.append(proxy_bal)
            self._alert_if_low(proxy_bal)

        return results

    async def _check_5sim(self) -> Optional[ServiceBalance]:
        key = self._sms_cfg.api_key
        if not key:
            return None
        try:
            if HAS_CURL:
                resp = curl_requests.get(
                    "https://5sim.net/v1/user/profile",
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    impersonate="chrome136",
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
            else:
                import urllib.request
                req = urllib.request.Request(
                    "https://5sim.net/v1/user/profile",
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = json.loads(r.read())

            balance = float(data.get("balance", 0))
            return ServiceBalance(
                name="5sim",
                balance_usd=balance,
                ltc_deposit_address=self._cfg.sms_ltc_address,
                low_threshold_usd=self._cfg.low_balance_alert_usd,
            )
        except Exception as e:
            logger.warning(f"[balance] 5sim 残高取得失敗: {e}")
            return ServiceBalance(
                name="5sim",
                balance_usd=0.0,
                ltc_deposit_address=self._cfg.sms_ltc_address,
                low_threshold_usd=self._cfg.low_balance_alert_usd,
                reachable=False,
            )

    async def _check_proxy_cheap(self) -> Optional[ServiceBalance]:
        if not self._cfg.proxy_cheap_api_key:
            return None
        result = await self._proxy_client.get_balance()
        balance = result.balance_usd if result else 0.0
        return ServiceBalance(
            name="proxy-cheap",
            balance_usd=balance,
            ltc_deposit_address=self._cfg.proxy_cheap_ltc_address,
            low_threshold_usd=self._cfg.low_balance_alert_usd,
            reachable=result is not None,
        )

    def _alert_if_low(self, svc: ServiceBalance):
        if not svc.reachable:
            logger.warning(f"[balance] {svc.name}: 接続不可 — API キーを確認してください")
            return
        if svc.balance_usd < svc.low_threshold_usd:
            addr = svc.ltc_deposit_address or "(config.yaml に ltc_address を設定)"
            logger.warning(
                f"[balance] ⚠ {svc.name} 残高不足: ${svc.balance_usd:.2f} "
                f"(閾値 ${svc.low_threshold_usd:.2f}) — "
                f"Exodus から LTC を送金してください → {addr}"
            )
        else:
            logger.info(f"[balance] {svc.name}: ${svc.balance_usd:.2f} (OK)")
