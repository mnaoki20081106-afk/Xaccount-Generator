import asyncio
from types import SimpleNamespace

from src.factory.account_factory import AccountFactory
from src.auth import twofa_enroll
from tests.test_twofa_flow import SECRET, _Page, _qr_png


class _Context:
    def __init__(self):
        self.closed = False

    async def add_cookies(self, cookies):
        self.cookies = cookies

    async def new_cdp_session(self, page):
        raise RuntimeError("no cdp in test")

    async def close(self):
        self.closed = True


class _Browser:
    def __init__(self, pages_by_block):
        self.pages_by_block = pages_by_block
        self.blocks = []
        self.ctx = _Context()

    async def new_context(self, **kw):
        self.locale = kw.get("locale")
        return self.ctx

    async def new_page(self, context, block_resources=True):
        self.blocks.append(block_resources)
        page = self.pages_by_block[block_resources]
        page.close = _noop
        return page


async def _noop():
    pass


def _factory(browser, enroll=True):
    f = AccountFactory.__new__(AccountFactory)
    f.config = SimpleNamespace(
        twofa=SimpleNamespace(enabled=True),
        factory=SimpleNamespace(enroll_2fa=enroll),
    )
    f.browser = browser
    f.twofa_manager = SimpleNamespace(encrypt=lambda s: "enc:" + s)
    return f


def run(c):
    return asyncio.run(c)


def test_blocked_page_succeeds_without_retry():
    b = _Browser({True: _Page([_qr_png()]), False: _Page([_qr_png()])})
    assert run(_factory(b)._enroll_2fa("u", "tok", "ct0", "http://p")) == "enc:" + SECRET
    assert b.blocks == [True] and b.locale == "en-US" and b.ctx.closed


def test_retries_without_blocking_only_when_qr_missing():
    b = _Browser({True: _Page([b"nope"]), False: _Page([_qr_png()])})
    assert run(_factory(b)._enroll_2fa("u", "tok", "ct0", "http://p")) == "enc:" + SECRET
    assert b.blocks == [True, False]


def test_no_retry_when_code_rejected():
    b = _Browser({True: _Page([_qr_png()], code_rejected=True), False: _Page([_qr_png()])})
    assert run(_factory(b)._enroll_2fa("u", "tok", "ct0", "http://p")) is None
    assert b.blocks == [True] and b.ctx.closed


def test_disabled_or_missing_tokens_skip_browser():
    b = _Browser({True: _Page([_qr_png()]), False: _Page([_qr_png()])})
    assert run(_factory(b, enroll=False)._enroll_2fa("u", "tok", "ct0", "p")) is None
    assert run(_factory(b)._enroll_2fa("u", None, "ct0", "p")) is None
    assert b.blocks == []
