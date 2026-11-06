"""Wordline merchant API + converter tests (no network, no platform DB).

Run:  env/bin/pytest tenant/Wordline/tests -q
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from tenant.Wordline.api import main as api
from tenant.Wordline.api.store import MerchantStore, contact_key, terminal_keys
from tenant.Wordline.scripts import convert_csv

KEY = "test-key-123"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv(api.API_KEY_ENV, KEY)


@pytest.fixture()
def client():
    api.store.refresh(force=True)
    return TestClient(api.app)


# ── converter ───────────────────────────────────────────────────────────────


def test_converter_matches_committed_json():
    document = convert_csv.convert()
    committed = json.loads(convert_csv.JSON_PATH.read_text(encoding="utf-8"))
    assert document["records"] == committed["records"]
    assert document["_meta"]["record_count"] == 42
    assert document["_meta"]["fields"] == convert_csv.FIELDS


def test_converter_normalises_and_rejects_collisions(tmp_path):
    good = tmp_path / "ok.csv"
    good.write_text(
        "ANCHORAPPTID\tMERCHANT\tBank Name\tASSETMODEL\tContact No\tType\n"
        "1231410a\tShop A\tBank\tModel\t+91 98928 58883\tPaper Roll\n"
        "\t\t\t\t\t\n", encoding="utf-8")
    records = convert_csv.convert(good)["records"]
    assert records == [{
        "terminal_id": "1231410A", "outlet_name": "Shop A", "bank_name": "Bank",
        "asset_type": "Model", "contact_number": "9892858883",
        "issue_type": "Paper Roll", "issue_code": "paper_roll"}]
    dupe = tmp_path / "dupe.csv"
    dupe.write_text(
        "ANCHORAPPTID,MERCHANT,Bank Name,ASSETMODEL,Contact No,Type\n"
        "1231410A,Shop A,Bank,Model,9892858883,Paper Roll\n"
        "A1231410,Shop B,Bank,Model,9892858884,Paper Roll\n", encoding="utf-8")
    with pytest.raises(convert_csv.ConversionError, match="duplicate terminal digit key"):
        convert_csv.convert(dupe)
    short = tmp_path / "short.csv"
    short.write_text("ANCHORAPPTID,MERCHANT,Bank Name,ASSETMODEL,Contact No,Type\n"
                     "1231410A,Shop A,Bank,Model,98928,Paper Roll\n", encoding="utf-8")
    with pytest.raises(convert_csv.ConversionError, match="10 digits"):
        convert_csv.convert(short)


def test_converter_check_mode(tmp_path):
    assert convert_csv.main(["--check"]) == 0
    stale = tmp_path / "stale.json"
    stale.write_text('{"records": []}', encoding="utf-8")
    assert convert_csv.main(["--check", "--out", str(stale)]) == 1


# ── store keys ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("1231410A", ("1231410A", "1231410")),
    ("1 2 3 1 4 1 0", ("1231410", "1231410")),
    ("91-37-97-84", ("91379784", "91379784")),
    ("{{spoken_terminal_id_retry}}", ("", "")),
    ("{spoken_terminal_id}", ("", "")),
    (None, ("", "")),
    ("x" * 20, ("", "")),
])
def test_terminal_keys(raw, expected):
    assert terminal_keys(raw) == expected


def test_contact_key():
    assert contact_key("+91 98928 58883") == "9892858883"
    assert contact_key("9892858883") == "9892858883"
    assert contact_key("12345") == ""


def test_store_reloads_when_file_changes(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"records": [
        {"terminal_id": "1111A", "contact_number": "9000000001", "outlet_name": "One"}]}))
    store = MerchantStore(path)
    assert store.find_terminal("1111").record["outlet_name"] == "One"
    path.write_text(json.dumps({"records": [
        {"terminal_id": "2222A", "contact_number": "9000000002", "outlet_name": "Two"}]}))
    os.utime(path, (1, 1))  # force a distinct mtime
    assert store.find_terminal("1111") is None
    assert store.find_contact("9000000002").record["outlet_name"] == "Two"


# ── HTTP ────────────────────────────────────────────────────────────────────


def test_health_needs_no_key(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["records"] == 42
    assert r.json()["api_key_configured"] is True


def test_auth_fail_closed(client, monkeypatch):
    assert client.post("/api/v1/merchants/lookup", json={"terminal_id": "91379784"}).status_code == 401
    assert client.post("/api/v1/merchants/lookup", json={"terminal_id": "91379784"},
                       headers={"X-API-Key": "wrong"}).status_code == 401
    monkeypatch.setenv(api.API_KEY_ENV, "")
    monkeypatch.setattr(api, "_key_from_dotenv", lambda: "")
    r = client.post("/api/v1/merchants/lookup", json={"terminal_id": "91379784"}, headers=HEADERS)
    assert r.status_code == 503
    assert r.json()["lookup_status"] == "unauthorized"


def test_lookup_by_dictated_digits_and_exact_id(client):
    r = client.post("/api/v1/merchants/lookup", json={"spoken_terminal_id": "9 1 3 7 9 7 8 4"},
                    headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["lookup_status"] == "found"
    assert body["found"] is True and body["verified"] is True
    assert body["matched_by"] == "terminal_id"
    assert body["outlet_name"] == "AGARWAL PUBLIC SCHOOL"
    assert body["bank_name"] == "AXIS Bank"
    assert body["asset_type"] == "PAYTIVO-7210-2G+WIFI"
    assert body["contact_number"] == "9303333356"
    assert body["contact_number_masked"] == "XXXXXX3356"
    assert body["issue_code"] == "paper_roll"

    r = client.post("/api/v1/merchants/lookup", json={"spoken_terminal_id": "1231410"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["matched_by"] == "terminal_digits"
    assert r.json()["terminal_id"] == "1231410A"
    # leading zero preserved in the digit key
    r = client.post("/api/v1/merchants/lookup", json={"terminal_id": "0710141"}, headers=HEADERS)
    assert r.json()["outlet_name"] == "ADVANCE FERTILITY AND GYNAECOLOG"


def test_lookup_precedence_retry_and_placeholders(client):
    # The retry answer beats the failed first answer; unresolved templates are ignored.
    r = client.post("/api/v1/merchants/lookup", json={
        "spoken_terminal_id": "12345678", "spoken_terminal_id_retry": "7926 0453",
        "request_type": "paperroll", "ctx_customer_name": "Rahul Sharma",
    }, headers=HEADERS)
    assert r.status_code == 200 and r.json()["outlet_name"] == "KUNAL SWEETS"
    r = client.post("/api/v1/merchants/lookup", json={
        "spoken_terminal_id": "79260453", "spoken_terminal_id_retry": "{{spoken_terminal_id_retry}}",
    }, headers=HEADERS)
    assert r.json()["outlet_name"] == "KUNAL SWEETS"


def test_lookup_miss_omits_mapped_keys(client):
    r = client.post("/api/v1/merchants/lookup", json={"spoken_terminal_id": "12345678"}, headers=HEADERS)
    assert r.status_code == 404
    body = r.json()
    assert body["lookup_status"] == "not_found"
    for key in ("found", "verified", "outlet_name", "bank_name", "contact_number"):
        assert key not in body
    r = client.post("/api/v1/merchants/lookup", json={"request_type": "battery"}, headers=HEADERS)
    assert r.status_code == 422 and r.json()["lookup_status"] == "no_identifier"
    r = client.post("/api/v1/merchants/lookup", content=b"not json",
                    headers={**HEADERS, "content-type": "application/json"})
    assert r.status_code == 422


def test_lookup_by_contact_and_get_endpoints(client):
    r = client.post("/api/v1/merchants/lookup", json={"caller_phone": "+919303333356"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["matched_by"] == "contact_number"
    assert client.get("/api/v1/merchants/by-contact/9303333356", headers=HEADERS).json()["terminal_id"] == "91379784"
    assert client.get("/api/v1/merchants/by-contact/1234", headers=HEADERS).status_code == 404
    assert client.get("/api/v1/merchants/1231410A", headers=HEADERS).json()["outlet_name"] == "BHANU MEDICAL AND GENERAL STORE"
    assert client.get("/api/v1/merchants/4166725", headers=HEADERS).json()["terminal_id"] == "4166725M"
    assert client.get("/api/v1/merchants/0000000", headers=HEADERS).status_code == 404


def test_listing_is_masked_and_filtered(client):
    r = client.get("/api/v1/merchants", params={"issue": "battery_issue", "limit": 5}, headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 21 and len(body["records"]) == 5
    assert all("contact_number" not in rec for rec in body["records"])
    assert all(rec["contact_number_masked"].startswith("XXXXXX") for rec in body["records"])
    r = client.get("/api/v1/merchants", params={"bank": "kotak"}, headers=HEADERS)
    assert {rec["terminal_id"] for rec in r.json()["records"]} == {"79260453", "1974857A"}
    assert client.get("/api/v1/merchants", params={"limit": 500}, headers=HEADERS).status_code == 422
