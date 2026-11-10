"""Error-routing contract between the merchant API and the platform executor.

Runs the REAL executor decision functions (``ToolExecutor._success`` and
``validate_response`` from shared/orchestration/tool_executor.py — nothing
mocked, nothing patched) over REAL responses of the merchant API (FastAPI
TestClient) with the exact connection settings of setup/01, and asserts which
api-node edge each HTTP outcome takes:

    200 hit            → success edge, merchant fields mapped
    200 miss           → success edge, nothing mapped (→ merchant_found false → retry)
    401 / 403 / 404 / 422 / 429 / non-JSON / foreign 200 / 5xx / timeout / connect error → failure edge
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from fastapi.testclient import TestClient

from shared.orchestration.tool_executor import ToolExecutor, validate_response
from tenant.Wordline.api import main as api

SETUP = pathlib.Path(__file__).resolve().parent.parent / "setup" / "01_api_connection.py"
KEY = "contract-key"


def _load_connection() -> dict:
    spec = importlib.util.spec_from_file_location("wordline_setup_01", SETUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CONNECTION


@pytest.fixture(scope="module")
def connection() -> dict:
    c = _load_connection()
    return {"success_condition": c["successCondition"], "response_schema": c["responseSchema"],
            "response_mapping": c["responseMapping"]}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv(api.API_KEY_ENV, KEY)
    api.store.refresh(force=True)
    return TestClient(api.app)


def decide(connection: dict, status: int, body, error: str | None = None) -> tuple[bool, dict]:
    """Mirror of ToolExecutor.execute's outcome logic for one HTTP exchange."""
    if status == 0:
        return False, {}
    ok = ToolExecutor._success(connection, status) and not error
    if ok and validate_response(connection["response_schema"], body):
        ok = False
    return ok, (ToolExecutor._apply_mapping(connection, body) if ok else {})


def test_hit_takes_success_edge_with_mapped_fields(client, connection):
    r = client.post("/api/v1/merchants/lookup", json={"spoken_terminal_id": "9 1 3 7 9 7 8 4"},
                    headers={"X-API-Key": KEY})
    ok, mapped = decide(connection, r.status_code, r.json())
    assert (r.status_code, ok) == (200, True)
    assert mapped["merchant_found"] is True and mapped["customer_verified"] is True
    assert mapped["outlet_name"] == "Agarwal Public School" and mapped["bank_name"] == "AXIS Bank"
    assert mapped["asset_type"] == "Paytivo 7210 2G plus WiFi"
    assert mapped["address"] == "Agarwal Public School, Bicholi Mardana Road, Indore, Madhya Pradesh 452016"
    assert mapped["contact_person"] == "K C Tiwari"


def test_miss_takes_success_edge_with_nothing_mapped(client, connection):
    r = client.post("/api/v1/merchants/lookup", json={"spoken_terminal_id": "12345678"},
                    headers={"X-API-Key": KEY})
    ok, mapped = decide(connection, r.status_code, r.json())
    assert (r.status_code, ok, mapped) == (200, True, {})
    assert r.json()["lookup_status"] == "not_found"


def test_studio_test_sample_value_is_a_miss_that_passes(client, connection):
    """Studio's "Send test request" fills {{customer_phone}} with +10000000000 and
    treats any non-2xx as a failing endpoint — the probe must be a 200 miss."""
    r = client.post("/api/v1/merchants/lookup", json={"tid": "+10000000000"}, headers={"X-API-Key": KEY})
    assert r.status_code == 200 and r.json()["lookup_status"] == "not_found"
    assert decide(connection, r.status_code, r.json()) == (True, {})


@pytest.mark.parametrize("headers,body,expected_status", [
    ({}, {"spoken_terminal_id": "91379784"}, 401),                       # no key
    ({"X-API-Key": "wrong"}, {"spoken_terminal_id": "91379784"}, 401),   # wrong key
    ({"X-API-Key": KEY}, {"request_type": "battery"}, 422),              # no identifier
])
def test_auth_and_validation_errors_take_failure_edge(client, connection, headers, body, expected_status):
    r = client.post("/api/v1/merchants/lookup", json=body, headers=headers)
    assert r.status_code == expected_status
    ok, mapped = decide(connection, r.status_code, r.json())
    assert (ok, mapped) == (False, {})


def test_unconfigured_service_takes_failure_edge(client, connection, monkeypatch):
    monkeypatch.setenv(api.API_KEY_ENV, "")
    monkeypatch.setattr(api, "_key_from_dotenv", lambda: "")
    r = client.post("/api/v1/merchants/lookup", json={"spoken_terminal_id": "91379784"},
                    headers={"X-API-Key": KEY})
    assert r.status_code == 503
    assert decide(connection, r.status_code, r.json()) == (False, {})


@pytest.mark.parametrize("status,body,error", [
    (403, {"error": "forbidden"}, None),                     # gateway / WAF
    (429, {"error": "rate limited"}, None),                  # proxy throttling
    (404, None, "Response was not valid JSON."),             # HTML 404 page from a proxy
    (404, {"detail": "Not Found"}, None),                    # wrong path on the service itself
    (200, "<html>login</html>", None),                       # non-object body
    (200, {"found": True, "outlet_name": "X"}, None),        # foreign service without the contract
    (500, {"lookup_status": "found"}, None),
    (502, None, "Connection failed (ConnectError)."),
    (504, None, "Upstream timeout after 2500ms."),
    (0, None, "The URL resolves to a private or internal address, which is not allowed."),
])
def test_other_outcomes_take_failure_edge(connection, status, body, error):
    assert decide(connection, status, body, error) == (False, {})


def test_new_mapping_against_a_service_without_the_new_fields(connection):
    """DEMO runs the previous service until its code is deployed: the new
    address / contact_person mappings must then map nothing (the workflow's
    ``address exists`` gate stays false and the steps keep asking)."""
    old_hit = {"lookup_status": "found", "found": True, "verified": True,
               "matched_by": "terminal_id", "terminal_id": "91379784",
               "outlet_name": "AGARWAL PUBLIC SCHOOL", "bank_name": "AXIS Bank",
               "asset_type": "PAYTIVO-7210-2G+WIFI", "contact_number": "9303333356",
               "contact_number_masked": "XXXXXX3356", "issue_type": "Paper Roll",
               "issue_code": "paper_roll"}
    ok, mapped = decide(connection, 200, old_hit)
    assert ok and mapped["outlet_name"] == "AGARWAL PUBLIC SCHOOL"
    assert "address" not in mapped and "contact_person" not in mapped
    assert {m["target"] for m in connection["response_mapping"]} >= {"address", "contact_person"}
