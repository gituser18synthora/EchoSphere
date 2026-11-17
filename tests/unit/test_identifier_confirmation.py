"""Identifiers cannot reach workflow actions before caller confirmation."""
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we
from shared.orchestration.entity_extractor import extract_entity
from shared.orchestration.identifier_confirmation import identifier_kind, spoken_identifier_text
from voice_runtime.identifier_capture import IdentifierCapture


TID_ENTITY = {"dataType": "identifier", "preserveAlphanumeric": True,
              "regexPattern": r"(?<![A-Za-z0-9])([A-Za-z]?\d{7,8}[A-Za-z]?)(?![A-Za-z0-9])"}


def definition(variable="terminal_id", entity=None):
    return {"id": "wf_confirm", "version": 1, "name": "confirm", "nodes": [
        {"id": "start", "kind": "start", "config": {}},
        {"id": "collect", "kind": "ask", "config": {
            "question": "Please give your ID.", "variable": variable,
            "entity": entity or TID_ENTITY}},
        {"id": "lookup", "kind": "api", "config": {"connection": "lookup"}},
        {"id": "end", "kind": "end", "config": {"text": "Done"}},
    ], "edges": [{"from": "start", "to": "collect"}, {"from": "collect", "to": "lookup"},
                  {"from": "lookup", "to": "end", "label": "success"}]}


def engine(monkeypatch, doc=None):
    monkeypatch.setattr(we, "load_workflow_definition", lambda *a: doc or definition())
    result = we.WorkflowEngine()

    async def memory():
        if result._checkpointer is None:
            result._checkpointer = MemorySaver()
        return result._checkpointer

    monkeypatch.setattr(result, "_get_checkpointer", memory)
    return result


async def turn(wf, text, *, reset=False, session="one", signal=None, tenant="tenant", initial=None):
    return await wf.handle_turn_detailed(
        session_id=session, tenant_id=tenant, bot_id="bot", workflow_name="confirm",
        user_text=text, reset_state=reset, language="en-IN", signal=signal, initial_slots=initial,
        mock_tool_results={"lookup": {"ok": True}})


@pytest.mark.parametrize("text,expected", [
    ("1132986A", "1132986A"), ("A for apple two one two nine zero zero seven", "A2129007"),
    ("1132. 986 ए।", "1132986A"), ("4166725 M", "4166725M"),
    ("zero zero one two three four five A", "0012345A"), ("11329868", "11329868"),
])
def test_alphanumeric_value_survives_normalization(text, expected):
    assert extract_entity(text, TID_ENTITY)["value"] == expected


async def test_candidate_is_private_and_no_action_runs_until_yes(monkeypatch):
    wf = engine(monkeypatch)
    await turn(wf, "hello", reset=True)
    pending = await turn(wf, "1132986A")
    assert pending["awaitingIdentifierConfirmation"]
    assert "letter A" in pending["reply"] and "one, one, three" in pending["reply"]
    assert pending["slots"] == {} and "lookup" not in pending["trace"]
    assert pending["awaitingIdentifier"] is None
    assert pending["awaitingAsk"]["kind"] == "yes_no"
    final = await turn(wf, "yes, correct")
    assert final["slots"]["terminal_id"] == "1132986A"
    assert final["trace"].count("lookup") == 1 and final["done"]


async def test_no_then_replacement_requires_new_confirmation(monkeypatch):
    wf = engine(monkeypatch)
    await turn(wf, "11329868", reset=True)  # entry capture is gated too
    rejected = await turn(wf, "no")
    assert not rejected["slots"] and "lookup" not in rejected["trace"]
    pending = await turn(wf, "1132986A")
    assert "letter A" in pending["reply"] and not pending["slots"]
    final = await turn(wf, "haan sahi hai")
    assert final["slots"]["terminal_id"] == "1132986A" and final["done"]


@pytest.mark.parametrize("correction", ["no, 1132986A", "yes, actually 1132986A", "1132986A"])
async def test_inline_correction_never_approves_old_candidate(monkeypatch, correction):
    wf = engine(monkeypatch)
    await turn(wf, "11329868", reset=True)
    updated = await turn(wf, correction, signal="affirm")
    assert updated["slots"] == {} and "letter A" in updated["reply"]
    assert "lookup" not in updated["trace"]
    final = await turn(wf, "yes")
    assert final["slots"]["terminal_id"] == "1132986A"


@pytest.mark.parametrize("reply", ["yes but the last letter is A", "not correct", "haan nahi", "why do you need it?", "okay", "sir", ""])
async def test_uncertain_or_partial_correction_never_advances(monkeypatch, reply):
    wf = engine(monkeypatch)
    await turn(wf, "11329868", reset=True)
    result = await turn(wf, reply, signal="affirm")
    assert not result["done"] and not result["slots"] and "lookup" not in result["trace"]


async def test_masked_phone_readback_does_not_export_raw_value(monkeypatch):
    wf = engine(monkeypatch, definition("alternate_phone", {"dataType": "phone", "pii": True}))
    pending = await turn(wf, "9876543210", reset=True)
    assert "nine, eight, seven" in pending["reply"] and not pending["slots"]
    final = await turn(wf, "yes")
    assert "•" in final["slots"]["alternate_phone"]
    assert "9876543210" not in json.dumps(final)


async def test_unknown_confirmation_is_bounded_and_session_isolated(monkeypatch):
    wf = engine(monkeypatch)
    await turn(wf, "1132986A", reset=True)
    other = await turn(wf, "yes", reset=True, session="two")
    assert not other["done"] and not other["slots"]
    for _ in range(3):
        result = await turn(wf, "maybe")
    assert result["status"] == "handoff" and "lookup" not in result["trace"]


def test_amounts_dates_and_authentication_secrets_are_not_identifier_readbacks():
    for var, dtype in [("amount", "number"), ("visit_date", "date"), ("otp", "integer"), ("account_pin", "integer")]:
        assert identifier_kind({"config": {"entity": {"dataType": dtype}}}, var) is None


def test_suffix_fragment_is_preserved_and_numeric_prefix_gets_a_grace_window():
    capture = IdentifierCapture("workflow", "collect", "terminal_id", TID_ENTITY)
    assert capture.candidate("1132986 ए") == "1132986A"
    assert capture.is_dictation("ए")
    assert capture.hold_delay("1132986") > 0
    assert capture.hold_delay("1132986A") == 0


async def test_secondary_phone_capture_cannot_skip_confirmation(monkeypatch):
    doc = definition("phone", {"dataType": "phone"})
    doc["nodes"].insert(1, {"id": "story", "kind": "ask", "config": {
        "variable": "note", "question": "Tell me the issue and your phone number.",
        "alsoCapture": [{"variable": "phone", "entity": {"dataType": "phone"}}]}})
    doc["edges"][0]["to"] = "story"
    doc["edges"].append({"from": "story", "to": "collect"})
    wf = engine(monkeypatch, doc)
    await turn(wf, "hello", reset=True)
    result = await turn(wf, "My terminal is broken, my phone is 9876543210")
    assert result["awaitingIdentifierConfirmation"] and "phone" not in result["slots"]
    assert "lookup" not in result["trace"]
    final = await turn(wf, "yes")
    assert final["slots"]["phone"] == "9876543210" and final["done"]


async def test_rollback_restores_pending_candidate_and_reset_discards_it(monkeypatch):
    wf = engine(monkeypatch)
    await turn(wf, "1132986A", reset=True)
    await turn(wf, "3139511A")
    assert await wf.rollback_last_turn(session_id="one", workflow_name="confirm", user_text="3139511A")
    result = await turn(wf, "yes")
    assert result["slots"]["terminal_id"] == "1132986A"
    await turn(wf, "hello", reset=True)
    result = await turn(wf, "yes")
    assert "terminal_id" not in result["slots"] and "lookup" not in result["trace"]


@pytest.mark.parametrize("text", ["1234567890123456", "123 456 789 012 3456"])
def test_phone_extractor_never_truncates_an_overlong_number(text):
    assert not extract_entity(text, {"dataType": "phone"})["matched"]


@pytest.mark.parametrize("variable,metadata", [
    ("bookingId", {}), ("complaintID", {}), ("customerReferenceNumber", {}),
    ("ticketNo", {}), ("shipment-number", {}), ("shipping_id", {}),
    ("order_ref2", {}), ("registrationReference", {}), ("serialNumber", {}),
    ("customValue", {"dataType": "identifier"}),
    ("opaque", {"semanticType": "identifier"}),
    ("opaque", {"requireConfirmation": True}),
])
async def test_any_tenant_reference_uses_the_common_gate(monkeypatch, variable, metadata):
    entity = {"dataType": "text", "regexPattern": r"\b([A-Z]{2}-[A-Z]{3})\b", **metadata}
    wf = engine(monkeypatch, definition(variable, entity))
    for tenant in ("booking_tenant", "complaint_tenant"):
        pending = await turn(wf, "AB-XYZ", reset=True, session=tenant, tenant=tenant)
        assert pending["awaitingIdentifierConfirmation"] and not pending["slots"]
        assert "lookup" not in pending["trace"]
        assert "letter A, letter B, hyphen, letter X, letter Y, letter Z" in pending["reply"]
        final = await turn(wf, "yes", session=tenant, tenant=tenant)
        assert final["slots"][variable] == "AB-XYZ"
        assert final["trace"].count("lookup") == 1


@pytest.mark.parametrize("variable,dtype", [
    ("shipping_id", "text"), ("bookingId", "text"), ("complaintID", "text"),
    ("ticketNo", "text"), ("opaque", "identifier"),
])
def test_generic_names_and_schema_work_without_a_custom_matcher(variable, dtype):
    from shared.orchestration.ask_resolution import _extract_ask_value
    node = {"config": {"entityType": dtype}}
    assert identifier_kind(node, variable) == "identifier"
    assert _extract_ask_value(node, variable, "000123A") == "000123A"


@pytest.mark.parametrize("variable,dtype", [
    ("issue_type", "text"), ("language_code", "text"), ("guests_number", "number"),
    ("floor_number", "integer"), ("amount_number", "number"), ("visitDate", "date"),
    ("number_of_rooms", "integer"), ("number_of_tickets", "integer"), ("identifierCount", "integer"),
    ("otpCode", "identifier"), ("accountPIN", "identifier"), ("cardNumber", "identifier"),
    ("password", "identifier"),
])
def test_choices_measurements_and_secrets_are_not_inferred_as_ids(variable, dtype):
    assert identifier_kind({"config": {"entityType": dtype}}, variable) is None


async def test_all_letter_ids_correct_and_reconfirm_without_treating_yes_as_a_new_id(monkeypatch):
    entity = {"dataType": "identifier", "regexPattern": r"\b([A-Z]{2,10})\b"}
    wf = engine(monkeypatch, definition("bookingId", entity))
    pending = await turn(wf, "HONASA", reset=True)
    assert pending["awaitingIdentifierConfirmation"]
    rejected = await turn(wf, "no")
    assert not rejected["awaitingIdentifierConfirmation"] and not rejected["slots"]
    await turn(wf, "ABCDEF")
    corrected = await turn(wf, "yes, actually GHIJKL")
    assert not corrected["slots"] and "lookup" not in corrected["trace"]
    final = await turn(wf, "yes correct")
    assert final["slots"]["bookingId"] == "GHIJKL" and final["done"]


async def test_unavailable_outcomes_are_distinct_from_valid_all_letter_ids(monkeypatch):
    entity = {"dataType": "identifier", "regexPattern": r"\b([A-Z]{6})\b",
              "synonymPatterns": {"missing_reference": [r"I do not have it"]}}
    wf = engine(monkeypatch, definition("bookingId", entity))
    missing = await turn(wf, "I do not have it", reset=True)
    assert not missing["awaitingIdentifierConfirmation"]
    assert missing["slots"]["bookingId"] == "missing_reference"
    pending = await turn(wf, "HONASA", reset=True)
    assert pending["awaitingIdentifierConfirmation"] and not pending["slots"]


async def test_configured_unavailable_values_do_not_depend_on_digit_content(monkeypatch):
    entity = {"dataType": "identifier", "regexPattern": r"\b([A-Z]{2})\b",
              "unavailableValues": ["NA"]}
    wf = engine(monkeypatch, definition("bookingId", entity))
    missing = await turn(wf, "NA", reset=True)
    assert not missing["awaitingIdentifierConfirmation"] and missing["slots"]["bookingId"] == "NA"


@pytest.mark.parametrize("value", ["NO-5678", "LAST-5678", "NOT-5678"])
async def test_control_like_prefix_is_preserved_inside_a_reference(monkeypatch, value):
    entity = {"dataType": "identifier", "regexPattern": r"\b([A-Z]{2,4}-\d{4})\b"}
    wf = engine(monkeypatch, definition("bookingId", entity))
    await turn(wf, "AB-1234", reset=True)
    changed = await turn(wf, value)
    assert changed["awaitingIdentifierConfirmation"] and not changed["slots"]
    final = await turn(wf, value + " yes")
    assert final["slots"]["bookingId"] == value


@pytest.mark.parametrize("value", ["ONE", "DO", "NA", "NONE"])
async def test_valid_letter_ids_are_not_rewritten_as_number_words_or_unavailability(monkeypatch, value):
    entity = {"dataType": "identifier", "regexPattern": r"\b([A-Z]{2,6})\b"}
    wf = engine(monkeypatch, definition("bookingId", entity))
    pending = await turn(wf, value, reset=True)
    assert pending["awaitingIdentifierConfirmation"] and not pending["slots"]
    final = await turn(wf, "yes")
    assert final["slots"]["bookingId"] == value


async def test_corrected_reference_at_an_intent_hub_cannot_bypass_confirmation(monkeypatch):
    doc = definition("customValue", {"dataType": "identifier"})
    doc["nodes"][1] = {"id": "collect", "kind": "intent", "config": {
        "prompt": "Do you want an agent?",
        "identifierCorrection": {"variable": "customValue", "target": "lookup",
                                 "entity": {"dataType": "identifier",
                                            "regexPattern": r"\b([A-Z]{2}-\d{4})\b"}}}}
    # The API is reachable here only through the explicit correction shortcut.
    doc["edges"] = [e for e in doc["edges"] if e["from"] != "collect"]
    wf = engine(monkeypatch, doc)
    await turn(wf, "hello", reset=True, initial={"customValue": "ZZ-0000"})
    pending = await turn(wf, "AB-1234")
    assert pending["awaitingIdentifierConfirmation"] and pending["awaitingKind"] == "ask"
    assert not pending["slots"] and "lookup" not in pending["trace"]
    corrected = await turn(wf, "CD-5678")
    assert not corrected["slots"] and "lookup" not in corrected["trace"]
    await turn(wf, "no")
    pending = await turn(wf, "EF-9012")
    assert pending["awaitingIdentifierConfirmation"] and "lookup" not in pending["trace"]
    final = await turn(wf, "yes")
    assert final["slots"]["customValue"] == "EF-9012" and final["trace"].count("lookup") == 1


def test_entity_authoring_accepts_the_generic_identifier_type():
    from backend.routers.intents import EntityRequest, UpdateEntityRequest
    assert EntityRequest(name="Any reference", dataType="identifier").data_type == "identifier"
    assert UpdateEntityRequest(dataType="identifier").data_type == "identifier"


def test_default_false_does_not_disable_id_confirmation_but_yes_no_choices_are_exempt():
    assert identifier_kind({"config": {"entity": {"requireConfirmation": False}}}, "bookingId") == "identifier"
    for entity in ({"allowedValues": ["yes", "no"]},
                   {"synonyms": {"yes": ["haan"], "no": ["nahi"]}}):
        assert identifier_kind({"config": {"entity": entity}}, "phoneVerified") is None


@pytest.mark.parametrize("reply", ["Okay yes it's correct", "It's correct", "that's right"])
async def test_natural_confirmation_commits_original_candidate_once(monkeypatch, reply):
    wf = engine(monkeypatch)
    await turn(wf, "1132986A", reset=True)
    final = await turn(wf, reply)
    assert final["slots"]["terminal_id"] == "1132986A"
    assert final["done"] and final["trace"].count("lookup") == 1


@pytest.mark.parametrize("original,suffix,expected", [
    ("1132986", "and B, B for boy", "1132986B"),
    ("4166725A", "MM for mango", "4166725M"),
    ("1132986", "बी फोर बॉय", "1132986B"),
])
async def test_suffix_continuation_amends_private_candidate_then_reconfirms(monkeypatch, original, suffix, expected):
    wf = engine(monkeypatch)
    await turn(wf, original, reset=True)
    updated = await turn(wf, suffix, signal="affirm")
    assert updated["awaitingIdentifierConfirmation"] and not updated["slots"]
    assert "letter " + expected[-1] in updated["reply"]
    assert "lookup" not in updated["trace"]
    final = await turn(wf, "It's correct")
    assert final["slots"]["terminal_id"] == expected and final["trace"].count("lookup") == 1


@pytest.mark.parametrize("reply", ["It's correct?", "yes but last B", "B for Alpha", "double B"])
async def test_question_or_ambiguous_spelling_cannot_confirm_pending_id(monkeypatch, reply):
    wf = engine(monkeypatch)
    await turn(wf, "1132986", reset=True)
    result = await turn(wf, reply, signal="affirm")
    assert not result["done"] and not result["slots"] and "lookup" not in result["trace"]


async def test_contracted_negative_restarts_capture_instead_of_confirming(monkeypatch):
    wf = engine(monkeypatch)
    await turn(wf, "1132986A", reset=True)
    result = await turn(wf, "okay yes it isn't correct", signal="affirm")
    assert not result["awaitingIdentifierConfirmation"] and not result["slots"]
    assert "lookup" not in result["trace"] and not result["done"]
