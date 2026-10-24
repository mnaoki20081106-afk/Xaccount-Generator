from urllib.parse import unquote, urlparse

from src.config import ProxyConfig
from src.proxy.manager import ProxySession


def _url(**kw):
    cfg = ProxyConfig(provider="statesideip", host="gw.statesideip.com", port=7777,
                      username="USER", password="PW", **kw)
    return ProxySession({"session_id": "a1b2"}, cfg).url, cfg


def test_username_format_country_then_session_then_ttl():
    url, _ = _url(country="us", session_lifetime_minutes=30)
    assert unquote(urlparse(url).username) == "USER-country-us-session-a1b2-ttl-30"
    assert urlparse(url).hostname == "gw.statesideip.com" and urlparse(url).port == 7777


def test_ttl_is_clamped_to_30_minutes_in_config_and_url():
    url, cfg = _url()  # 既定 10080 分
    assert cfg.session_lifetime_minutes == 30
    assert unquote(urlparse(url).username) == "USER-session-a1b2-ttl-30"


def test_other_providers_keep_their_lifetime():
    cfg = ProxyConfig(provider="iproyal", host="h", port=1, username="u", password="p")
    assert cfg.session_lifetime_minutes == 10080
