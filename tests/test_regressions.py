import asyncio

import pytest

from src.arkose_solver.bda import generate_bda
from src.arkose_solver.crypto import marshal_bda
from src.arkose_solver.devices import generate_device
from src.factory.api_flow import XApiFlow
from src.x_client_transaction.utils import float_to_hex


class _Flow(XApiFlow):
    def __init__(self):
        self.email = "t@example.com"
        self.password = "pw"
        self.username = "testuser"
        self.full_name = "T U"
        self.bd_year, self.bd_month, self.bd_day = 1990, 1, 1
        self.js_instrumentation = "{}"
        self.arkose_token = None
        self._seen_subtasks = {}
        self.cds_api_key = None


ARKOSE = {"subtask_id": "ArkoseEmail", "web_modal": {"url": "https://x.com?data=B"}}


def test_username_retry_changes_name():
    f = _Flow()
    first = f._build_subtask_input({"subtask_id": "UsernameEntryBio"}, None)
    assert first["enter_username"]["username"] == "testuser"
    f._seen_subtasks["UsernameEntryBio"] = 1
    second = f._build_subtask_input({"subtask_id": "UsernameEntryBio"}, None)
    assert second["enter_username"]["username"] != "testuser"


def test_arkose_rechallenge_resolves_again():
    f = _Flow()
    f._solve_arkose = lambda blob: "T1"
    assert "T1" in f._build_subtask_input(ARKOSE, None)["web_modal"]["completion_deeplink"]
    f._seen_subtasks["ArkoseEmail"] = 1
    f._solve_arkose = lambda blob: "T2"
    assert "T2" in f._build_subtask_input(ARKOSE, None)["web_modal"]["completion_deeplink"]


def test_unknown_subtask_returns_none():
    assert _Flow()._build_subtask_input({"subtask_id": "Nope"}, None) is None


@pytest.mark.parametrize("val,exp", [
    (0.0, "0"), (15.0, "f"), (16.0, "10"), (255.0, "ff"),
    (3735928559.0, "deadbeef"), (0.5, "0.8"), (255.5, "ff.8"),
])
def test_float_to_hex(val, exp):
    assert float_to_hex(val) == exp


def test_bda_pipeline():
    cfg = {"surl": "https://client-api.arkoselabs.com", "language": "ja",
           "title": "", "window_location_href": "https://x.com",
           "sitedata_location_href": "https://x.com",
           "document_referrer": "", "enforcement_hash": ""}
    assert len(marshal_bda(generate_bda(cfg))) > 1000


def test_device_has_snake_and_camel_keys():
    d = generate_device()
    for k in ("color_depth", "device_memory", "gl_vendor", "innerWidth", "glVendor"):
        assert k in d
