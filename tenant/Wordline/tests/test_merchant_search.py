"""Terminal ID / Merchant ID / merchant name search (2026-11-26 export
``Wordline_with_distance.csv``: + ``ANCHORAPPMID``, + ``distance``).

Every assertion runs on the real converted records (``data/merchants.json``)
through the real FastAPI app; the connection contract part uses the platform
executor's own success / schema / mapping functions with the setup/01 row.

Run:  env/bin/pytest tenant/Wordline/tests/test_merchant_search.py -q
"""

from __future__ import annotations

import csv
import importlib.util
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from shared.orchestration.tool_executor import ToolExecutor, validate_response
from tenant.Wordline.api import main as api
from tenant.Wordline.api.store import identifier_in_text, phonetic_key
from tenant.Wordline.scripts import convert_csv

KEY = "search-key"
HEADERS = {"X-API-Key": KEY}
TENANT = pathlib.Path(__file__).resolve().parent.parent
SETUP = TENANT / "setup" / "01_api_connection.py"
RECORDS = json.loads(convert_csv.JSON_PATH.read_text(encoding="utf-8"))["records"]
BY_TID = {r["terminal_id"]: r for r in RECORDS}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv(api.API_KEY_ENV, KEY)


@pytest.fixture()
def client():
    api.store.refresh(force=True)
    return TestClient(api.app)


def lookup(client, body: dict):
    return client.post("/api/v1/merchants/lookup", json=body, headers=HEADERS)


def _connection() -> dict:
    spec = importlib.util.spec_from_file_location("wordline_setup_01_search", SETUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    c = module.CONNECTION
    return {"success_condition": c["successCondition"], "response_schema": c["responseSchema"],
            "response_mapping": c["responseMapping"]}


def decide(status: int, body) -> tuple[bool, dict]:
    connection = _connection()
    ok = ToolExecutor._success(connection, status)
    if ok and validate_response(connection["response_schema"], body):
        ok = False
    return ok, (ToolExecutor._apply_mapping(connection, body) if ok else {})


# ── converter: the two new columns ───────────────────────────────────────────


def test_json_carries_merchant_id_and_distance_from_the_csv():
    """Cell by cell against the export: ANCHORAPPMID and distance per TID."""
    rows = list(csv.DictReader(
        convert_csv.CSV_PATH.read_text(encoding="utf-8-sig").splitlines(True), delimiter="\t"))
    assert len(rows) == len(RECORDS) == 42
    for row in rows:
        record = BY_TID[row["ANCHORAPPTID"].strip().upper()]
        assert record["merchant_id"] == row["ANCHORAPPMID"].strip()
        assert record["distance"] == int(row["distance"])
    assert convert_csv.main(["--check"]) == 0


def test_converter_rejects_duplicate_or_malformed_merchant_ids(tmp_path):
    head = ("ANCHORAPPTID\tANCHORAPPMID\tMERCHANT\tBank Name\tASSETMODEL\tContact No\tType"
            "\tdistance\n")
    dupe = tmp_path / "dupe.csv"
    dupe.write_text(head + "1231410A\t0110656A0007856\tA\tB\tM\t9892858883\tPaper Roll\t3\n"
                    "2231410A\t01106560007856\tC\tB\tM\t9892858884\tPaper Roll\t4\n",
                    encoding="utf-8")
    with pytest.raises(convert_csv.ConversionError, match="duplicate merchant id digit key"):
        convert_csv.convert(dupe)
    bad = tmp_path / "bad.csv"
    bad.write_text(head + "1231410A\t01AB56\tA\tB\tM\t9892858883\tPaper Roll\t3\n",
                   encoding="utf-8")
    with pytest.raises(convert_csv.ConversionError, match="merchant id"):
        convert_csv.convert(bad)
    far = tmp_path / "far.csv"
    far.write_text(head + "1231410A\t0110656A0007856\tA\tB\tM\t9892858883\tPaper Roll\tfar\n",
                   encoding="utf-8")
    with pytest.raises(convert_csv.ConversionError, match="distance"):
        convert_csv.convert(far)


def test_converter_parses_decimal_and_empty_distance():
    assert convert_csv.parse_distance("12.5") == 12.5
    assert convert_csv.parse_distance("65") == 65
    assert convert_csv.parse_distance("") is None


# ── the three searches, every real record ────────────────────────────────────


@pytest.mark.parametrize("record", RECORDS, ids=[r["terminal_id"] for r in RECORDS])
def test_every_record_resolves_by_each_of_the_three_fields(client, record):
    """Each search alone (explicit ``search_by``) finds the same merchant and
    returns its details incl. merchant_id and distance."""
    expected = {"terminal_id": record["terminal_id"], "merchant_id": record["merchant_id"],
                "distance": record["distance"], "bank_name": record["bank_name"]}
    for field, query, matched_by in (
        ("terminal_id", record["terminal_id"], "terminal_id"),
        ("merchant_id", record["merchant_id"], "merchant_id"),
        ("merchant_name", record["outlet_name"].lower(), "merchant_name"),
    ):
        r = lookup(client, {"search_by": field, "query": query})
        body = r.json()
        assert r.status_code == 200 and body["lookup_status"] == "found", (field, body)
        assert body["matched_by"] == matched_by
        assert {k: body[k] for k in expected} == expected


def test_csv_column_names_work_as_keys_and_search_by(client):
    for body in ({"ANCHORAPPTID": "1231410A"}, {"ANCHORAPPMID": "0110656A0007856"},
                 {"MERCHANT": "Bhanu Medical"},
                 {"search_by": "ANCHORAPPMID", "query": "0110656A0007856"},
                 {"search_by": "MERCHANT", "query": "bhanu"}):
        r = lookup(client, body).json()
        assert r["lookup_status"] == "found" and r["terminal_id"] == "1231410A", body


# ── Terminal ID: unchanged ───────────────────────────────────────────────────


def test_terminal_id_lookup_is_backward_compatible(client):
    """The workflow's existing bodies: same answers, same mapped fields, and
    two new fields (merchant_id, distance) on a hit."""
    hit = lookup(client, {"spoken_terminal_id": "9 1 3 7 9 7 8 4", "tid": "{{customer_phone}}"})
    body = hit.json()
    assert hit.status_code == 200 and body["outlet_name"] == "Agarwal Public School"
    assert body["matched_by"] == "terminal_id"            # all digits: the exact key
    assert (body["merchant_id"], body["distance"]) == ("037122019340169", 65)
    letter_lost = lookup(client, {"spoken_terminal_id": "1231410"}).json()
    assert letter_lost["terminal_id"] == "1231410A"
    retry_first = lookup(client, {"spoken_terminal_id": "12345678",
                                  "spoken_terminal_id_retry": "3137502A"}).json()
    assert retry_first["outlet_name"] == "Hotel The Rostral"
    miss = lookup(client, {"spoken_terminal_id": "12345678"})
    assert miss.status_code == 200
    assert miss.json() == {"lookup_status": "not_found", "found_count": 0,
                           "message": "No merchant found for the shared details.",
                           "hint": "Terminal ids are 7-8 characters; check the digits dictated."}
    assert lookup(client, {"spoken_terminal_id": "no_tid"}).json()["lookup_status"] == "not_found"
    none = lookup(client, {"customer_name": "Rahul"})
    assert none.status_code == 422 and none.json()["lookup_status"] == "no_identifier"


# ── Merchant ID: exact ───────────────────────────────────────────────────────


def test_merchant_id_is_exact(client):
    def by_mid(query):
        return lookup(client, {"search_by": "merchant_id", "query": query})

    assert by_mid("0110656a0007856").json()["matched_by"] == "merchant_id"     # case-insensitive
    assert by_mid("0110656 A 0007856").json()["terminal_id"] == "1231410A"    # dictated spacing
    lost = by_mid("01106560007856").json()                                     # STT lost the A
    assert (lost["matched_by"], lost["terminal_id"]) == ("merchant_id_digits", "1231410A")
    assert by_mid("0110656B0007856").json()["lookup_status"] == "not_found"   # other letter
    assert by_mid("37122019340169").json()["lookup_status"] == "not_found"    # leading 0 dropped
    assert by_mid("0371220193401690").json()["lookup_status"] == "not_found"  # one digit extra
    assert by_mid("466821290007").json()["outlet_name"] == "Bhagwati Enterprises"  # 12 digits


def test_free_text_mid_and_outlet_answer(client):
    """The bot's free-text "MID and outlet name" answer (``pr_merchant_outlet``
    / ``bat_merchant_outlet``): the id inside it first (10+ digits = merchant
    id, 4-9 = terminal id), then the rest of the answer as an outlet name."""
    assert identifier_in_text("mera MID hai 0 1 1 0 6 5 6 A for Apple 0 0 0 7 8 5 6") == (
        "merchant_id", "0110656A0007856")
    assert identifier_in_text("TID 1 2 3 1 4 1 0 A hai") == ("terminal_id", "1231410A")
    assert identifier_in_text("A H Arts") == ("", "")
    mid = lookup(client, {"pr_merchant_outlet": "MID hai 0063260 A 0001914"}).json()
    assert (mid["outlet_name"], mid["matched_by"]) == ("Hotel The Rostral", "merchant_id")
    tid = lookup(client, {"bat_merchant_outlet": "terminal 3 3 1 8 2 5 8 A"}).json()
    assert tid["outlet_name"] == "Westend Entertainment"
    both = lookup(client, {"pr_merchant_outlet":
                           "MID 0 1 1 0 6 5 6 A 0 0 0 7 8 5 6, outlet Bhanu Medical"}).json()
    assert (both["outlet_name"], both["matched_by"]) == ("Bhanu Medical and General Store",
                                                         "merchant_id")
    # a misheard MID does not hide the outlet name said with it
    misheard = lookup(client, {"pr_merchant_outlet":
                               "MID 0 1 1 0 6 5 6 A 0 0 0 7 8 5 9 aur outlet Kunal Sweets"}).json()
    assert (misheard["outlet_name"], misheard["matched_by"]) == ("Kunal Sweets", "merchant_name")
    no_mid = lookup(client, {"bat_merchant_outlet":
                             "MID yaad nahi hai, dukaan ka naam Eye World Optics"}).json()
    assert no_mid["outlet_name"] == "Eye World Optics"


@pytest.mark.parametrize("answer, outlets", [
    ("mera MID yaad nahi, dukaan ka naam Kunal Sweets", ["Kunal Sweets"]),
    ("outlet ka naam Snacks Centre hai, MID pata nahi", ["Snacks Centre"]),
    ("Sagar footwear", ["Sagar Shoes Showroom"]),
    ("मेरा आउटलेट शाजी मेडिकल्स है, एमआईडी नहीं पता", ["Shaji Medicals"]),
    ("medical store", ["Bhanu Medical and General Store", "Shankar Medical and General Stores"]),
    ("xyz traders", []),                         # only a generic word in common
    ("Rajesh Kirana", []),
])
def test_free_text_answers_use_the_shared_words_tier(client, answer, outlets):
    body = lookup(client, {"pr_merchant_outlet": answer}).json()
    if not outlets:
        assert body["lookup_status"] == "not_found"
    elif len(outlets) == 1:
        assert body["lookup_status"] == "found" and body["outlet_name"] == outlets[0]
    else:
        assert body["lookup_status"] == "multiple"
        assert [c["outlet_name"] for c in body["candidates"]] == outlets
    # an explicit API name query never uses the loose tier
    assert lookup(client, {"MERCHANT": "xyz traders"}).json()["lookup_status"] == "not_found"


# ── Merchant name: case-insensitive, partial, never arbitrary ────────────────


@pytest.mark.parametrize("query, outlet, tier", [
    ("WESTEND ENTERTAINMENT", "Westend Entertainment", "exact"),
    ("tristar cars private limited", "Tristar Cars Private Limited", "exact"),   # PVT LTD
    ("mera shop ka naam Kunal Sweets hai", "Kunal Sweets", "exact"),             # fillers
    ("bhanu", "Bhanu Medical and General Store", "partial"),
    ("agarwal school", "Agarwal Public School", "partial"),
    ("jaya lakshmi", "Jayalakshmi Medicals", "partial"),
    ("la mode", "La Mode Fashions Private Limited", "partial"),
    ("Jayalaxmi", "Jayalakshmi Medicals", "phonetic"),
    ("भानु मेडिकल", "Bhanu Medical and General Store", "phonetic"),
    ("मेरी दुकान का नाम अग्रवाल पब्लिक स्कूल है", "Agarwal Public School", "phonetic"),
    ("डेक्कन क्रॉनिकल", "Deccan Chronicle Holdings Limited", "phonetic"),
    ("पेलिकन ऑफिस", "Pelikan Office Automation Private Limited", "phonetic"),
])
def test_name_search_single_match(client, query, outlet, tier):
    body = lookup(client, {"search_by": "merchant_name", "query": query}).json()
    assert body["lookup_status"] == "found", body
    assert body["outlet_name"] == outlet
    assert body["matched_by"] == "merchant_name"


def test_exact_name_wins_over_names_it_is_part_of(client):
    # "hotel" alone matches two hotels; the full name is one merchant.
    assert lookup(client, {"MERCHANT": "hotel ida"}).json()["outlet_name"] == "Hotel Ida"


@pytest.mark.parametrize("query, count", [("medical", 5), ("hotel", 2), ("store", 4),
                                          ("MEDICALS", 3)])
def test_name_matching_several_merchants_returns_all_and_picks_none(client, query, count):
    r = lookup(client, {"search_by": "merchant_name", "query": query})
    body = r.json()
    assert r.status_code == 200 and body["lookup_status"] == "multiple"
    assert body["ambiguous"] is True and body["found_count"] == body["match_count"] == count
    assert len(body["candidates"]) == count and body["truncated"] is False
    for forbidden in ("found", "verified", "matched_by", "terminal_id", "outlet_name",
                      "contact_number", "address", "contact_person"):
        assert forbidden not in body                   # nothing a workflow could map
    for candidate in body["candidates"]:
        assert set(candidate) == {"terminal_id", "merchant_id", "outlet_name", "bank_name",
                                  "issue_type", "distance"}            # no contact details
    names = [c["outlet_name"] for c in body["candidates"]]
    assert names == sorted(names, key=str.upper)                       # stable order
    assert body["candidates_spoken"] == ", ".join(names[:3])


def test_name_misses_and_invalid_names(client):
    miss = lookup(client, {"search_by": "merchant_name", "query": "xyz traders"})
    assert miss.status_code == 200 and miss.json()["lookup_status"] == "not_found"
    short = lookup(client, {"search_by": "merchant_name", "query": "ab"})
    assert short.status_code == 422 and short.json()["lookup_status"] == "invalid_query"
    fillers = lookup(client, {"search_by": "merchant_name", "query": "mera shop ka naam"})
    assert fillers.status_code == 422


# ── invalid input ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body, status", [
    ({"search_by": "pincode", "query": "452016"}, 422),
    ({"search_by": "merchant_id", "query": ""}, 422),
    ({"search_by": "merchant_id", "query": "12345"}, 422),
    ({"search_by": "merchant_id", "query": "01AB0656A0007856"}, 422),
    ({"search_by": "terminal_id", "query": "!!!"}, 422),
    ({"search_by": "terminal_id", "query": "1234567890123"}, 422),
])
def test_explicit_invalid_queries_are_422(client, body, status):
    r = lookup(client, body)
    assert r.status_code == status and r.json()["lookup_status"] == "invalid_query"


def test_workflow_style_invalid_values_are_a_graceful_miss(client):
    """The workflow posts whatever the caller said; an unusable value is a 200
    miss that names the problem (never 422, which would be the "service
    unavailable" edge)."""
    r = lookup(client, {"spoken_terminal_id": "no_tid", "spoken_terminal_id_retry": "m_tid",
                        "pr_merchant_outlet": "haan ji", "merchant_id": "12"})
    body = r.json()
    assert r.status_code == 200 and body["lookup_status"] == "not_found"
    assert {i["field"] for i in body["invalid"]} == {"pr_merchant_outlet", "merchant_id"}


def test_search_order_lets_a_later_answer_decide_after_misses(client):
    """Slots from earlier questions are posted too: misses do not block."""
    body = lookup(client, {"spoken_terminal_id": "12345678", "spoken_terminal_id_retry": "87654321",
                           "pr_merchant_outlet": "Shaji Medicals"}).json()
    assert body["outlet_name"] == "Shaji Medicals" and body["searched_field"] == "pr_merchant_outlet"
    # the retry answer to "several merchants" is consulted before the first answer
    narrowed = lookup(client, {"pr_merchant_outlet": "medical",
                               "pr_merchant_outlet_retry": "sri ram medicals"}).json()
    assert narrowed["outlet_name"] == "Sri Ram Medicals"


# ── GET routes ───────────────────────────────────────────────────────────────


def test_get_routes_for_merchant_id_and_name(client):
    r = client.get("/api/v1/merchants/by-merchant-id/0141093A0018926", headers=HEADERS)
    assert r.status_code == 200 and r.json()["outlet_name"] == "Sri Ram Medicals"
    assert client.get("/api/v1/merchants/by-merchant-id/0141093A0018927",
                      headers=HEADERS).status_code == 404
    assert client.get("/api/v1/merchants/by-merchant-id/123", headers=HEADERS).status_code == 422
    one = client.get("/api/v1/merchants/search", params={"name": "snacks"}, headers=HEADERS)
    assert one.json()["outlet_name"] == "Snacks Centre"
    several = client.get("/api/v1/merchants/search", params={"name": "hotel"}, headers=HEADERS)
    assert several.json()["lookup_status"] == "multiple"
    assert client.get("/api/v1/merchants/search", params={"name": "zzz"},
                      headers=HEADERS).status_code == 404
    assert client.get("/api/v1/merchants/search", headers=HEADERS).status_code == 422
    # the terminal route is not shadowed by the new ones
    assert client.get("/api/v1/merchants/1231410A", headers=HEADERS).json()["merchant_id"] == \
        "0110656A0007856"
    assert client.get("/api/v1/merchants/search?name=hotel").status_code == 401   # key required


def test_listing_carries_merchant_id_and_distance_but_no_personal_fields(client):
    body = client.get("/api/v1/merchants", params={"limit": 100}, headers=HEADERS).json()
    assert body["total"] == 42
    for row in body["records"]:
        assert "merchant_id" in row and "distance" in row
        assert not {"contact_number", "address", "contact_person"} & set(row)


# ── platform connection contract ─────────────────────────────────────────────


def test_connection_routes_found_multiple_and_miss(client):
    found = lookup(client, {"pr_merchant_outlet": "0110656A0007856"})
    ok, mapped = decide(found.status_code, found.json())
    assert ok and mapped["merchant_found"] is True and mapped["customer_verified"] is True
    assert mapped["merchant_id"] == "0110656A0007856"
    assert mapped["ctx_distance"] == 33           # the workflow's visit-timing slot (lte 50)
    assert "merchant_ambiguous" not in mapped

    several = lookup(client, {"pr_merchant_outlet": "hotel"})
    ok, mapped = decide(several.status_code, several.json())
    assert ok                                                   # success edge, not "unavailable"
    assert mapped == {"merchant_ambiguous": True, "merchant_match_count": 2,
                      "merchant_candidates": "Hotel Ida, Hotel The Rostral"}

    miss = lookup(client, {"pr_merchant_outlet": "xyz traders"})
    ok, mapped = decide(miss.status_code, miss.json())
    assert ok and mapped == {}                                  # nothing pinned by a miss

    explicit_bad = lookup(client, {"search_by": "merchant_id", "query": "1"})
    assert decide(explicit_bad.status_code, explicit_bad.json())[0] is False


def test_phonetic_key_examples():
    assert phonetic_key("MEDICAL") == phonetic_key("medikl") == "MDKL"
    assert phonetic_key("SCHOOL") == phonetic_key("skul") == "SKL"
    assert phonetic_key("OFFICE") == phonetic_key("ophis") == "FS"
