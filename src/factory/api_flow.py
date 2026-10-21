"""
X アカウント作成 — API 直叩きモード。
Playwright 不要。curl_cffi で Chrome TLS 指紋を偽装しつつ
onboarding/task.json API を順番に叩く。

参考: OkoyaUsman/twitter-account-creation-bot (Castle.io + CDS Solver)
      buggerlogger/arkose-solver Python ポート (ローカル FunCaptcha 解決)
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import string
import time
import urllib.parse
from datetime import datetime
from typing import Optional
import random

from loguru import logger

# curl_cffi は TLS 指紋 (JA3/JA4) を Chrome に偽装できる
try:
    import curl_cffi
    from curl_cffi import requests as curl_requests
    HAS_CURL = True
except ImportError:
    HAS_CURL = False
    logger.warning("curl_cffi 未インストール。API フローは無効 (pip install curl-cffi)")

# ローカル Arkose ソルバー
try:
    from ..arkose_solver import Solver as ArkoseSolver
    HAS_LOCAL_ARKOSE = True
except ImportError:
    HAS_LOCAL_ARKOSE = False

# X API 定数
X_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
X_TASK_URL = "https://api.x.com/1.1/onboarding/task.json"
X_GUEST_URL = "https://api.x.com/1.1/guest/activate.json"
X_JS_INST_URL = "https://x.com/i/js_inst?c_name=ui_metrics"
X_VIEWER_URL = (
    "https://api.x.com/graphql/RTSjxYbzVsRECfMtoB1Cqw/Viewer"
)

# Arkose surl / public key for X email signup
X_ARKOSE_SURL = "https://client-api.arkoselabs.com"
X_ARKOSE_KEY = "2CB16598-CB82-4CF7-B332-5990DB66F3AB"

# 指紋一貫性: curl_cffi の impersonate と BDA/UA の Chrome major を揃える
# chrome136 を使うなら UA も Chrome/136 で統一する
IMPERSONATE_VERSION = "chrome136"
CHROME_MAJOR = 136
UA_STR = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    f"(KHTML, like Gecko) Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
SEC_CH_UA = (
    f'"Not/A)Brand";v="99", "Google Chrome";v="{CHROME_MAJOR}", '
    f'"Chromium";v="{CHROME_MAJOR}"'
)

SUBTASK_VERSIONS: dict = {
    "action_list": 2, "alert_dialog": 1, "app_download_cta": 1,
    "check_logged_in_account": 1, "choice_selection": 3,
    "contacts_live_sync_permission_prompt": 0, "cta": 7,
    "email_verification": 2, "end_flow": 1, "enter_date": 1,
    "enter_email": 2, "enter_password": 5, "enter_phone": 2,
    "enter_recaptcha": 1, "enter_text": 5, "enter_username": 2,
    "generic_urt": 3, "in_app_notification": 1, "interest_picker": 3,
    "js_instrumentation": 1, "menu_dialog": 1,
    "notifications_permission_prompt": 2, "open_account": 2,
    "open_home_timeline": 1, "open_link": 1, "phone_verification": 4,
    "privacy_options": 1, "security_key": 3, "select_avatar": 4,
    "select_banner": 2, "settings_list": 7, "show_code": 1,
    "sign_up": 2, "sign_up_review": 4, "tweet_selection_urt": 1,
    "update_users": 1, "upload_media": 1,
    "user_recommendations_list": 4, "user_recommendations_urt": 1,
    "wait_spinner": 3, "web_modal": 1,
}


def _gen_password(n: int = 14) -> str:
    symbols = "!@#$%^&*"
    pool = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(pool) for _ in range(n))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in symbols for c in pw)):
            return pw


def _gen_username() -> str:
    base = "".join(secrets.choice(string.ascii_lowercase) for _ in range(6))
    return f"{base}{secrets.randbelow(10000):04d}"


def _gen_birthdate() -> tuple[int, int, int]:
    return (random.randint(1988, 2002), random.randint(1, 12), random.randint(1, 28))


class XApiFlow:
    """
    X サインアップを API のみで完遂するクラス。
    同期の curl_cffi を asyncio executor で wrap して非同期対応。
    """

    def __init__(
        self,
        email: str,
        proxy_url: Optional[str] = None,
        cds_api_key: Optional[str] = None,
    ):
        if not HAS_CURL:
            raise RuntimeError("curl_cffi が必要です: pip install curl-cffi")

        self.email = email
        self.proxy_url = proxy_url
        self.cds_api_key = cds_api_key

        # アカウントデータ
        self.full_name: str = ""
        self.password: str = _gen_password()
        self.username: Optional[str] = None
        self.user_id: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.ct0: Optional[str] = None
        bd = _gen_birthdate()
        self.bd_year, self.bd_month, self.bd_day = bd

        # フロー状態
        self.flow_token: Optional[str] = None
        self.js_instrumentation: Optional[str] = None
        self.arkose_token: Optional[str] = None

        # セッション: UA / sec-ch-ua / impersonate を全部 Chrome major で揃える
        self._session = curl_requests.Session(impersonate=IMPERSONATE_VERSION)
        self._session.headers.update({
            "accept-language": "en-US,en;q=0.9",
            "authorization": f"Bearer {X_BEARER}",
            "origin": "https://x.com",
            "referer": "https://x.com/",
            "user-agent": UA_STR,
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        })
        if proxy_url:
            self._session.proxies.update({"all": proxy_url})

    # ------------------------------------------------------------------
    # ヘルパー
    # ------------------------------------------------------------------

    def _post(self, url: str, **kwargs) -> dict:
        r = self._session.post(url, **kwargs)
        try:
            return r.json()
        except Exception:
            return {"_raw_status": r.status_code, "_raw_text": r.text[:200]}

    def _get(self, url: str, **kwargs):
        return self._session.get(url, **kwargs)

    # ------------------------------------------------------------------
    # Arkose FunCaptcha 解決
    # ------------------------------------------------------------------

    def _solve_arkose_local(self, blob: str) -> Optional[str]:
        """buggerlogger/arkose-solver Python ポートでローカル解決。
        suppressed=False (sup=1 in token) のときは拒否される可能性が高いので None 扱い。
        指紋一貫性のため UA は api_flow と同じものを渡す。
        """
        if not HAS_LOCAL_ARKOSE:
            return None
        try:
            solver = ArkoseSolver(
                surl=X_ARKOSE_SURL,
                public_key=X_ARKOSE_KEY,
                site="https://x.com",
                proxy=self.proxy_url,
                language="en-US",
                user_agent=UA_STR,
            )
            solver.data_blob = blob
            result = solver.solve()
            logger.info(
                f"Arkose ローカル解決 suppressed={result.suppressed} "
                f"timings={result.timings}"
            )
            # suppressed=False (sup=1) のトークンは X で弾かれやすい → フォールバック
            if result.suppressed:
                logger.warning("Arkose ローカルトークン suppressed=False — CDS にフォールバック")
                return None
            return result.token
        except Exception as e:
            logger.warning(f"Arkose ローカル解決失敗: {e}")
            return None

    def _solve_arkose_cds(self, blob: str) -> Optional[str]:
        """CDS Solver API (cds-solver.com) 経由で解決。"""
        if not self.cds_api_key:
            return None
        base_url = "https://cds-solver.com"
        payload = {
            "api_key": self.cds_api_key,
            "site_key": X_ARKOSE_KEY,
            "browser": "chrome",
            "browser_version": 136,
            "platform": "PC",
            "locale": "en-US",
            "proxy": self.proxy_url or "",
            "blob": blob,
            "site_referer": "https://iframe.arkoselabs.com/",
        }
        try:
            resp = curl_requests.post(f"{base_url}/createTask", json=payload).json()
        except Exception as e:
            logger.error(f"CDS createTask error: {e}")
            return None

        task_id = resp.get("task_id")
        if not task_id:
            logger.error(f"CDS task作成失敗: {resp}")
            return None

        logger.debug(f"CDS task_id={task_id}")
        for _ in range(120):
            time.sleep(0.5)
            try:
                resp = curl_requests.post(
                    f"{base_url}/getTask",
                    json={"api_key": self.cds_api_key, "task_id": task_id},
                ).json()
                status = resp.get("status")
                if status == "processing":
                    continue
                if status == "success":
                    logger.info("CDS Solver 解決成功")
                    return resp.get("token")
                logger.error(f"CDS 解決失敗: {status}")
                return None
            except Exception as e:
                logger.debug(f"CDS getTask error: {e}")
        logger.error("CDS タイムアウト")
        return None

    def _solve_arkose(self, blob: str) -> Optional[str]:
        """CDS → ローカルの順。CDS がキー設定済みなら優先 (成功率が高い)。
        ローカルは BDA 固定値が多く X に拒否されやすいので「無料試行」扱い。
        """
        if self.cds_api_key:
            token = self._solve_arkose_cds(blob)
            if token:
                return token
            logger.warning("CDS 失敗 — ローカル solver にフォールバック")
        token = self._solve_arkose_local(blob)
        if token:
            return token
        # CDS キーなし & ローカル失敗の場合、CDS を再試行 (キーが別経路で入っていれば)
        if not self.cds_api_key:
            logger.error("Arkose: CDS キー未設定かつローカル失敗。config.captcha.cds_api_key を設定推奨")
        return None

    # ------------------------------------------------------------------
    # サインアップステップ
    # ------------------------------------------------------------------

    def _step_guest_token(self) -> bool:
        data = self._post(X_GUEST_URL)
        token = data.get("guest_token")
        if not token:
            logger.error(f"guest_token 取得失敗: {data}")
            return False
        self._session.headers.update({"x-guest-token": token})
        logger.debug("guest_token OK")
        return True

    def _step_js_instrumentation(self) -> bool:
        try:
            text = self._get(X_JS_INST_URL).text
            m = re.search(r"return ({.*?});", text, re.DOTALL)
            if not m:
                logger.error("js_instrumentation 抽出失敗")
                return False
            self.js_instrumentation = m.group(1)
            logger.debug("js_instrumentation OK")
            return True
        except Exception as e:
            logger.error(f"js_instrumentation error: {e}")
            return False

    def _step_init_signup(self) -> bool:
        """flow_name=signup で最初のタスクを開始。ArkoseEmail が来たら解決。"""
        payload = {
            "input_flow_data": {
                "requested_variant": '{"signup_type":"phone_email"}',
                "flow_context": {
                    "debug_overrides": {},
                    "start_location": {"location": "splash_screen"},
                },
            },
            "subtask_versions": SUBTASK_VERSIONS,
        }
        data = self._post(f"{X_TASK_URL}?flow_name=signup", json=payload)
        self.flow_token = data.get("flow_token")

        subtasks = data.get("subtasks", [])
        for st in subtasks:
            if st.get("subtask_id") == "ArkoseEmail":
                web_modal = st.get("web_modal", {})
                blob = urllib.parse.parse_qs(
                    urllib.parse.urlparse(web_modal.get("url", "")).query
                ).get("data", [None])[0]
                if blob:
                    self.arkose_token = self._solve_arkose(blob)
                else:
                    logger.warning("ArkoseEmail blob なし — ブランクトークンで続行")
                    self.arkose_token = ""
                break

        if self.arkose_token is None and any(
            st.get("subtask_id") == "ArkoseEmail" for st in subtasks
        ):
            logger.error("Arkose 解決失敗")
            return False

        logger.debug(f"init_signup OK flow_token={self.flow_token}")
        return bool(self.flow_token)

    def _step_submit_profile(self, display_name: str) -> bool:
        payload = {
            "flow_token": self.flow_token,
            "display_name": display_name,
            "email": self.email,
        }
        r = self._session.post("https://api.x.com/1.1/onboarding/begin_verification.json", json=payload)
        ok = r.status_code == 200
        if not ok:
            logger.error(f"submit_profile HTTP {r.status_code}: {r.text[:200]}")
        else:
            logger.debug("submit_profile OK")
        return ok

    def _step_verify_email(self, email_code: str) -> bool:
        subtask_inputs = [
            {
                "subtask_id": "Signup",
                "sign_up": {
                    "js_instrumentation": {"response": self.js_instrumentation},
                    "link": "email_next_link",
                    "name": self.full_name,
                    "email": self.email,
                    "birthday": {
                        "year": self.bd_year,
                        "month": self.bd_month,
                        "day": self.bd_day,
                    },
                    "personalization_settings": {
                        "allow_cookie_use": True,
                        "allow_device_personalization": True,
                        "allow_partnerships": True,
                        "allow_ads_personalization": True,
                    },
                },
            },
        ]
        if self.arkose_token is not None:
            subtask_inputs.append({
                "subtask_id": "ArkoseEmail",
                "web_modal": {
                    "completion_deeplink": f"twitter://onboarding/web_modal/next_link?access_token={self.arkose_token}",
                    "link": "signup_with_email_next_link",
                },
            })
        subtask_inputs.append({
            "subtask_id": "EmailVerification",
            "email_verification": {
                "code": email_code,
                "email": self.email,
                "link": "next_link",
            },
        })

        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": subtask_inputs,
        })
        if "_raw_status" in data:
            logger.error(f"verify_email HTTP {data['_raw_status']}")
            return False
        self.flow_token = data.get("flow_token", self.flow_token)
        logger.debug("verify_email OK")
        return True

    def _step_submit_password(self) -> bool:
        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": [{
                "subtask_id": "EnterPassword",
                "enter_password": {
                    "password": self.password,
                    "link": "next_link",
                },
            }],
        })
        if "_raw_status" in data:
            logger.error(f"submit_password HTTP {data['_raw_status']}")
            return False
        ct0 = self._session.cookies.get("ct0")
        if ct0:
            self._session.headers.update({"x-csrf-token": ct0})
            self.ct0 = ct0
        self.flow_token = data.get("flow_token", self.flow_token)
        logger.debug("submit_password OK")
        return True

    def _step_get_viewer(self) -> bool:
        features = json.dumps({
            "subscriptions_upsells_api_enabled": True,
            "payments_enabled": False,
            "responsive_web_graphql_timeline_navigation_enabled": True,
        })
        variables = json.dumps({"withCommunitiesMemberships": True})
        field_toggles = json.dumps({"isDelegate": False, "withAuxiliaryUserLabels": True})
        r = self._get(
            f"{X_VIEWER_URL}?variables={urllib.parse.quote(variables)}"
            f"&features={urllib.parse.quote(features)}"
            f"&fieldToggles={urllib.parse.quote(field_toggles)}"
        )
        if r.status_code != 200:
            logger.warning(f"viewer HTTP {r.status_code} — 続行")
        ct0 = self._session.cookies.get("ct0")
        if ct0:
            self._session.headers.update({"x-csrf-token": ct0})
            self.ct0 = ct0
        logger.debug("viewer OK")
        return True

    def _step_skip_avatar(self) -> bool:
        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": [{"subtask_id": "SelectAvatar", "select_avatar": {"link": "skip_link"}}],
        })
        self.flow_token = data.get("flow_token", self.flow_token)
        logger.debug("skip_avatar OK")
        return True

    def _step_set_username(self) -> bool:
        username = self.username or _gen_username()
        for attempt in range(3):
            data = self._post(X_TASK_URL, json={
                "flow_token": self.flow_token,
                "subtask_inputs": [{
                    "subtask_id": "UsernameEntryBio",
                    "enter_username": {"username": username, "link": "next_link"},
                }],
            })
            if "_raw_status" not in data:
                self.flow_token = data.get("flow_token", self.flow_token)
                self.username = username
                logger.debug(f"username={username} OK")
                return True
            # 衝突 → 別の名前で再試行
            username = _gen_username()
            logger.debug(f"username 衝突 → {username} で再試行 ({attempt+1}/3)")
        logger.error("username 設定失敗")
        return False

    def _step_skip_notifications(self) -> bool:
        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": [{
                "subtask_id": "NotificationsPermissionPrompt",
                "notifications_permission_prompt": {"link": "skip_link"},
            }],
        })
        self.flow_token = data.get("flow_token", self.flow_token)
        return True

    def _step_skip_category(self) -> bool:
        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": [{
                "subtask_id": "StandAloneCategoryPickerBlockingNextURT",
                "generic_urt": {"link": "next_link"},
            }],
        })
        self.flow_token = data.get("flow_token", self.flow_token)
        return True

    def _step_follow_seed_user(self) -> bool:
        """@elonmusk をフォローして user_id / screen_name を取得。"""
        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": [{
                "subtask_id": "UserRecommendationsURTFollowGating",
                "user_recommendations_urt": {
                    "link": "next_link",
                    "selected_user_recommendations": ["44196397"],
                },
            }],
        })
        self.flow_token = data.get("flow_token", self.flow_token)
        try:
            users = data["subtasks"][1]["update_users"]["users"]
            self.username = users[0]["screen_name"]
            self.user_id = users[0]["id_str"]
            logger.debug(f"screen_name={self.username} user_id={self.user_id}")
        except (KeyError, IndexError, TypeError):
            logger.warning("user_id / screen_name 取得失敗 — 続行")
        return True

    def _step_finish(self) -> bool:
        data = self._post(X_TASK_URL, json={
            "flow_token": self.flow_token,
            "subtask_inputs": [],
        })
        self.auth_token = self._session.cookies.get("auth_token")
        if not self.auth_token:
            logger.error("auth_token が cookie にない")
            return False
        logger.info(f"サインアップ完了 @{self.username} auth_token={self.auth_token[:8]}...")
        return True

    # ------------------------------------------------------------------
    # メインフロー (同期)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # レスポンス駆動の動的サブタスクディスパッチ
    # ------------------------------------------------------------------

    def _build_subtask_input(self, subtask: dict, code: Optional[str]) -> Optional[dict]:
        """
        サーバーから返された subtask の種類に応じて、次に送る subtask_input を組み立てる。
        未知の subtask は None を返し、呼び出し側でスキップ or 失敗判定。
        """
        sid = subtask.get("subtask_id", "")

        # Arkose チャレンジ (Email / Register / Generic)
        if sid.startswith("Arkose") or sid.endswith("ArkoseChallenge"):
            if self.arkose_token is None:
                web_modal = subtask.get("web_modal", {})
                blob = urllib.parse.parse_qs(
                    urllib.parse.urlparse(web_modal.get("url", "")).query
                ).get("data", [None])[0]
                if blob:
                    self.arkose_token = self._solve_arkose(blob) or ""
                else:
                    self.arkose_token = ""
            return {
                "subtask_id": sid,
                "web_modal": {
                    "completion_deeplink": (
                        f"twitter://onboarding/web_modal/next_link"
                        f"?access_token={self.arkose_token}"
                    ),
                    "link": "signup_with_email_next_link",
                },
            }

        if sid == "Signup":
            return {
                "subtask_id": "Signup",
                "sign_up": {
                    "js_instrumentation": {"response": self.js_instrumentation or "{}"},
                    "link": "email_next_link",
                    "name": self.full_name,
                    "email": self.email,
                    "birthday": {
                        "year": self.bd_year,
                        "month": self.bd_month,
                        "day": self.bd_day,
                    },
                    "personalization_settings": {
                        "allow_cookie_use": True,
                        "allow_device_personalization": True,
                        "allow_partnerships": True,
                        "allow_ads_personalization": True,
                    },
                },
            }

        if sid in ("EmailVerification", "SignupEmailVerification"):
            if not code:
                return None
            return {
                "subtask_id": sid,
                "email_verification": {
                    "code": code,
                    "email": self.email,
                    "link": "next_link",
                },
            }

        if sid in ("EnterPassword", "SignupPassword"):
            return {
                "subtask_id": sid,
                "enter_password": {"password": self.password, "link": "next_link"},
            }

        if sid in ("UsernameEntryBio", "SignupUsername", "EnterUsername"):
            uname = self.username or _gen_username()
            self.username = uname
            return {
                "subtask_id": sid,
                "enter_username": {"username": uname, "link": "next_link"},
            }

        if sid in ("SelectAvatar", "UploadAvatar"):
            return {"subtask_id": sid, "select_avatar": {"link": "skip_link"}}

        if sid == "SelectBanner":
            return {"subtask_id": sid, "select_banner": {"link": "skip_link"}}

        if sid == "NotificationsPermissionPrompt":
            return {
                "subtask_id": sid,
                "notifications_permission_prompt": {"link": "skip_link"},
            }

        if sid in ("ContactsLiveSyncPermissionPrompt",):
            return {
                "subtask_id": sid,
                "contacts_live_sync_permission_prompt": {"link": "skip_link"},
            }

        if sid in ("StandAloneCategoryPickerBlockingNextURT", "InterestPicker"):
            return {"subtask_id": sid, "generic_urt": {"link": "next_link"}}

        if sid.startswith("UserRecommendations"):
            return {
                "subtask_id": sid,
                "user_recommendations_urt": {
                    "link": "next_link",
                    "selected_user_recommendations": ["44196397"],
                },
            }

        if sid in ("MenuDialog", "AlertDialog"):
            return {"subtask_id": sid, "menu_dialog": {"link": "next_link"}}

        if sid in ("OpenHomeTimeline", "OpenAccount"):
            # 完了シグナル — 空の input で確定
            return {"subtask_id": sid, "open_link": {"link": "next_link"}}

        logger.warning(f"未知の subtask_id={sid} — スキップ")
        return None

    def _dispatch_loop(self, email_code_fetcher) -> bool:
        """
        初回レスポンス取得後、subtasks を見て次の input を動的に組み立てて送る。
        EmailVerification が出た時点でメールコード取得を遅延実行する。
        """
        # 最初の signup 開始呼び出し
        payload = {
            "input_flow_data": {
                "requested_variant": '{"signup_type":"phone_email"}',
                "flow_context": {
                    "debug_overrides": {},
                    "start_location": {"location": "splash_screen"},
                },
            },
            "subtask_versions": SUBTASK_VERSIONS,
        }
        data = self._post(f"{X_TASK_URL}?flow_name=signup", json=payload)
        if "_raw_status" in data:
            logger.error(f"init_signup HTTP {data['_raw_status']}: {data.get('_raw_text')}")
            return False
        self.flow_token = data.get("flow_token")
        if not self.flow_token:
            logger.error(f"flow_token なし: {data}")
            return False

        max_iterations = 25
        code: Optional[str] = None

        for i in range(max_iterations):
            subtasks = data.get("subtasks", [])
            if not subtasks:
                logger.debug(f"iter={i}: subtasks 空 → 完了判定")
                break

            # 完了判定
            if any(st.get("subtask_id") == "SuccessExit" for st in subtasks):
                logger.debug("SuccessExit 検出")
                break

            # EmailVerification が出たらここでメールコードを取得
            needs_code = any(
                st.get("subtask_id") in ("EmailVerification", "SignupEmailVerification")
                for st in subtasks
            )
            if needs_code and code is None:
                code = email_code_fetcher()
                if not code:
                    logger.error("メール確認コード取得失敗")
                    return False

            # 全 subtask に対して input を組み立てる
            subtask_inputs = []
            for st in subtasks:
                inp = self._build_subtask_input(st, code)
                if inp:
                    subtask_inputs.append(inp)

            if not subtask_inputs:
                logger.warning(f"iter={i}: 処理可能な subtask なし — subtasks={[s.get('subtask_id') for s in subtasks]}")
                break

            # password 送信後は ct0 を取り込む
            data = self._post(X_TASK_URL, json={
                "flow_token": self.flow_token,
                "subtask_inputs": subtask_inputs,
            })

            if "_raw_status" in data:
                logger.error(f"task HTTP {data['_raw_status']}: {data.get('_raw_text')}")
                return False

            self.flow_token = data.get("flow_token", self.flow_token)

            # ct0 cookie を取り込み
            ct0 = self._session.cookies.get("ct0")
            if ct0 and ct0 != self.ct0:
                self.ct0 = ct0
                self._session.headers.update({"x-csrf-token": ct0})

            # user info 抽出 (follow seed 時など)
            for st in subtasks:
                if st.get("subtask_id") == "UpdateUsers":
                    try:
                        users = st["update_users"]["users"]
                        self.username = users[0].get("screen_name") or self.username
                        self.user_id = users[0].get("id_str")
                    except (KeyError, IndexError, TypeError):
                        pass

        # auth_token 取得確認
        self.auth_token = self._session.cookies.get("auth_token")
        if not self.auth_token:
            logger.error("auth_token が cookie に出ていない (サインアップ失敗)")
            return False

        # auth_token が立っただけでは「作成直後に即凍結」を見逃す。
        # verify_credentials で実際にログイン状態として読めるか確認する。
        if not self._verify_alive():
            logger.error(f"@{self.username} auth_token はあるが即時凍結/無効 — 失敗扱い")
            return False

        logger.info(
            f"API サインアップ完了 @{self.username} auth_token={self.auth_token[:8]}..."
        )
        return True

    def _verify_alive(self) -> bool:
        """verify_credentials.json で垢が実際に生きているか確認する生存チェック。"""
        try:
            r = self._session.get(
                "https://api.x.com/1.1/account/verify_credentials.json",
                params={"include_entities": "false", "skip_status": "true"},
            )
            if r.status_code != 200:
                logger.warning(f"verify_credentials HTTP {r.status_code}: {r.text[:200]}")
                return False
            data = r.json()
            suspended = bool(data.get("suspended"))
            if suspended:
                logger.warning(f"@{self.username} suspended=true")
                return False
            return True
        except Exception as e:
            logger.warning(f"verify_credentials error: {e}")
            return False

    def run(self, email_code_fetcher) -> bool:
        """
        email_code_fetcher: メール確認コードを返す callable。
        実際の呼び出しは async_run() 推奨。
        """
        from faker import Faker
        if not self.full_name:
            self.full_name = Faker("ja_JP").name()

        # 前段: guest_token, js_instrumentation 取得
        for name, fn in [
            ("guest_token", self._step_guest_token),
            ("js_instrumentation", self._step_js_instrumentation),
        ]:
            if not fn():
                logger.error(f"API フロー失敗: ステップ={name}")
                return False

        # 動的ディスパッチループ
        return self._dispatch_loop(email_code_fetcher)

    # ------------------------------------------------------------------
    # 非同期ラッパー
    # ------------------------------------------------------------------

    async def async_run(self, email_code_fetcher_async) -> bool:
        """
        email_code_fetcher_async: async callable -> str | None
        curl_cffi の同期コードを executor で wrap。
        """
        loop = asyncio.get_event_loop()

        async def _code_sync_wrapper():
            return await email_code_fetcher_async()

        def _sync_run():
            code_holder = []

            async def _fetch():
                code_holder.append(await _code_sync_wrapper())

            loop2 = asyncio.new_event_loop()
            loop2.run_until_complete(_fetch())
            loop2.close()

            def fetcher():
                return code_holder[0] if code_holder else None

            return self.run(fetcher)

        return await loop.run_in_executor(None, _sync_run)
