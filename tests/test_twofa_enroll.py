from src.auth.twofa_enroll import parse_totp_secret


def test_parse_secret_from_otpauth_uri():
    uri = "otpauth://totp/X:user?secret=JBSWY3DPEHPK3PXP&issuer=X"
    assert parse_totp_secret(uri) == "JBSWY3DPEHPK3PXP"


def test_parse_secret_when_secret_is_last_param():
    # twittergen の正規表現は末尾が "&" でないと失敗する。クエリとして解析するので影響しない。
    assert parse_totp_secret("otpauth://totp/X:u?issuer=X&secret=ABCDEFGHIJKLMNOP") == "ABCDEFGHIJKLMNOP"


def test_parse_secret_rejects_non_otpauth_or_missing_secret():
    assert parse_totp_secret("https://example.com/?secret=ABC") is None
    assert parse_totp_secret("otpauth://totp/X:u?issuer=X") is None
