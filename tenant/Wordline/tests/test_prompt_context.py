"""Step 03 transforms for the 2026-11-10 fields (address, contact person)."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

SETUP = pathlib.Path(__file__).resolve().parent.parent / "setup" / "03_context_and_prompt.py"


@pytest.fixture(scope="module")
def s03():
    spec = importlib.util.spec_from_file_location("wordline_setup_03", SETUP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prompt(s03) -> str:
    return ("# ROLE\n\nYou are the voice agent.\n\n" + s03.RAISE_DETAILS_OLD
            + "\n\nQuantity is always between one and nine rolls per request.\n\n"
            + s03.CALLER_DATA_NEW + "\n\nBattery, battery looks fine (no heat).\n")


def test_visit_fields_prompt_is_exact_and_idempotent(s03):
    text = _prompt(s03)
    assert s03.upgrade_prompt(text) is None                  # v4 already applied
    new = s03.upgrade_prompt_visit_fields(text)
    assert "address (the registered\naddress) and contact_person" in new
    assert "say them exactly as written" in new
    assert "reads back the\nregistered address and contact person" in new
    assert new.count("# CALLER DATA") == 1
    assert new.endswith("Battery, battery looks fine (no heat).\n")   # rest untouched
    assert s03.upgrade_prompt_visit_fields(new) is None


def test_visit_fields_prompt_refuses_an_edited_section(s03):
    edited = _prompt(s03).replace("never invent a missing value", "never invent anything")
    with pytest.raises(SystemExit, match="CALLER DATA"):
        s03.upgrade_prompt_visit_fields(edited)


def test_runtime_context_declares_the_new_fields(s03):
    body = s03.upgrade_schema({"name": "Merchant terminal data", "testPayload": {
        "customer_name": "Rahul Sharma", "docket_number": "WL458921", "customer_verified": False}})
    keys = [f["key"] for f in body["fields"]]
    assert keys[-2:] == ["address", "contact_person"]
    assert "address" not in body["testPayload"] and "contact_person" not in body["testPayload"]
    assert "registered address and contact person" in body["missingValuePolicy"]
