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


class FactoryConfig(BaseModel):
    max_per_hour: int = 6
    use_phone: bool = False
    captcha_provider: str = "none"
    captcha_api_key: str = ""


class EmailConfig(BaseModel):
    provider: str = ""
    api_key: str = ""


class TwoFAConfig(BaseModel):
    enabled: bool = True
    encryption_key: str


class DBConfig(BaseModel):
    path: str = "data/accounts.db"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "data/bot.log"


class WarmupConfig(BaseModel):
    min_days_before_sale: int = 10
    actions_per_day_soft: int = 8


class AppConfig(BaseModel):
    proxy: ProxyConfig
    browser: BrowserConfig = BrowserConfig()
    factory: FactoryConfig = FactoryConfig()
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
