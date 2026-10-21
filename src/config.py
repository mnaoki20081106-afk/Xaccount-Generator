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
    country: str = "jp"
    sticky_ttl_minutes: int = 30
    max_accounts_per_ip: int = 5


class BrowserConfig(BaseModel):
    headless: bool = True
    slow_mo: int = 50
    default_timeout: int = 30000


class SmsConfig(BaseModel):
    provider: str = "5sim"               # 5sim / smsactivate / auto / none
    api_key: str = ""                    # 5sim または primary プロバイダーのキー
    api_key2: str = ""                   # auto モード時の secondary (smsactivate)
    preferred_countries: list[str] = ["russia", "india", "indonesia", "philippines"]


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
