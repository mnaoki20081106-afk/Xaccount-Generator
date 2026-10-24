from pathlib import Path
from typing import Optional
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class ProxyConfig(BaseModel):
    provider: str = "iproyal"
    host: str
    port: int
    username: str
    password: str
    country: str = ""                # 空文字 = 混合IP（国指定なし、最安）。"jp" で日本IP固定
    sticky_ttl_minutes: int = 30     # 互換用（未使用）。実際の維持時間は session_lifetime_minutes
    # スティッキーセッションの維持時間。垢は数週間ウォームアップするので IPRoyal の最大(7日)を既定に。
    session_lifetime_minutes: int = 10080
    killswitch: bool = True          # IP を失ったら黙って別 IP に変わらず 410 を返す（IPRoyal）
    max_accounts_per_ip: int = 5


class BrowserConfig(BaseModel):
    headless: bool = True
    slow_mo: int = 50
    default_timeout: int = 30000
    max_concurrent_browsers: int = 2


class SmsConfig(BaseModel):
    provider: str = "5sim"               # 5sim / smsactivate / auto / none
    api_key: str = ""                    # 5sim または primary プロバイダーのキー
    api_key2: str = ""                   # auto モード時の secondary (smsactivate)
    preferred_countries: list[str] = ["usa", "england", "germany", "france", "netherlands"]


class CaptchaConfig(BaseModel):
    provider: str = "none"               # capsolver / 2captcha / anticaptcha / local / none
    api_key: str = ""
    provider_fallback: str = ""          # プライマリが失敗した際のフォールバック
    api_key_fallback: str = ""
    # CDS Solver (cds-solver.com) APIキー — Arkose 解決のフォールバック
    # ローカル解決 (arkose_solver) が失敗した場合のみ使用
    cds_api_key: str = ""


class FactoryConfig(BaseModel):
    max_per_hour: int = 6
    use_phone: bool = True
    setup_profile: bool = True           # サインアップ後にプロフィール完成


class EmailConfig(BaseModel):
    provider: str = ""
    api_key: str = ""
    outlook_domain: str = "outlook.com"
    outlook_client_id: str = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"


class TwoFAConfig(BaseModel):
    enabled: bool = True
    encryption_key: str


class DBConfig(BaseModel):
    path: str = "data/accounts.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/bot.log"


class WarmupConfig(BaseModel):
    min_days_before_sale: int = 21       # HStoraレベル: 21日以上
    actions_per_day_soft: int = 8
    require_graduation: bool = False     # True: /pop は卒業テスト合格済みの垢だけ払い出す
    graduation_min_days: int = 21
    graduation_min_active_days: int = 10 # 実際に行動した日数の下限
    graduation_min_tweets: int = 8
    graduation_max_unplanned_ip_changes: int = 0
    graduation_max_attempts: int = 5     # 卒業テスト不合格の上限回数


class AppConfig(BaseModel):
    proxy: ProxyConfig
    browser: BrowserConfig = BrowserConfig()
    factory: FactoryConfig = FactoryConfig()
    sms: SmsConfig = SmsConfig()
    captcha: CaptchaConfig = CaptchaConfig()
    email: EmailConfig = EmailConfig()
    twofa: TwoFAConfig
    db: DBConfig = DBConfig()
    logging: LoggingConfig = LoggingConfig()
    warmup: WarmupConfig = WarmupConfig()


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} が見つかりません。config.example.yaml をコピーして config.yaml を作成してください。"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return AppConfig(**data)
