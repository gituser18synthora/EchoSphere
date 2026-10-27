"""Deterministic contracts and isolated worker/persistence/API tests.

Semantic multilingual extraction is evaluated separately with a real model
in tests/evals/test_jeeves_summary_intents.py; no keyword extractor is mocked
as though it could validate the model's understanding.
"""

import json
import runpy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jeeves.summary_intents import BOT_ID, INTENT_SUMMARY_FIELDS, enhance_summary_fields
from shared.orchestration.goal_engine import BotGoalPolicy, compile_goal_policy
from shared.post_call.analyzer import _build_system, _transcript_block, analyze_call, fallback_analysis
from shared.post_call.schema import parse_analysis
from shared.post_call.structured import customer_evidence, display_structured_fields, merge_structured_fields

ORIGINAL = runpy.run_path("jeeves/setup/01_structured_summary.py")["SUMMARY_FIELDS"]


def policy():
    return compile_goal_policy({"summaryFields": enhance_summary_fields(ORIGINAL)}, bot_name="Jeeves")


def merge(proposed, evidence=None, text="", slots=None):
    return merge_structured_fields(
        policy(), slots or {}, proposed, evidence=evidence,
        turns=[{"role": "user", "text": text}],
    )


def test_configuration_preserves_existing_fields_and_is_idempotent():
    enhanced = enhance_summary_fields(ORIGINAL)
    assert enhanced[:len(ORIGINAL)] == ORIGINAL
    assert enhance_summary_fields(enhanced) == enhanced
    assert len(enhanced) == len(ORIGINAL) + 7
    assert policy().source == "derived"
    assert all(not spec.allow_llm for spec in policy().summary_fields[:len(ORIGINAL)])
    custom = {"name": "custom", "type": "text", "allowLlm": False}
    assert enhance_summary_fields([custom])[0] == custom
    assert len(ORIGINAL) == 8  # no mutation of the old configuration


@pytest.mark.parametrize("quote,turns", [
    (None, [{"role": "user", "text": "Pickup ho chuka hai"}]),
    ("Device collected", [{"role": "bot", "text": "Device collected"}]),
    ("Device collected", [{"role": "user", "text": "Pickup karwa do"}]),
    ("Yes", [{"role": "user", "text": "Yesterday I called"}]),
    ({"quote": "Yes"}, [{"role": "user", "text": "Yes"}]),
])
def test_missing_bot_only_invented_and_non_span_evidence_is_rejected(quote, turns):
    assert customer_evidence(quote, turns) is None
    fields, sources = merge_structured_fields(
        policy(), {}, {"already_picked_up": "Yes"},
        evidence={"already_picked_up": quote}, turns=turns,
    )
    assert fields["already_picked_up"] is None
    assert "already_picked_up" not in sources


@pytest.mark.parametrize("text", ["Maine device already de diya hai.", "डिवाइस कल दे दिया था।", "Device already collected."])
def test_customer_quotes_are_attributed_in_hindi_hinglish_and_english(text):
    fields, sources = merge({"already_picked_up": True}, {"already_picked_up": text}, text)
    assert fields["already_picked_up"] == "Yes"
    assert sources["already_picked_up"] == "analysis"


def test_evidence_tolerates_terminal_punctuation_and_returns_actual_customer_span():
    assert customer_evidence("दे दिया था。", [{"role": "user", "text": "डिवाइस दे दिया था।"}]) == "दे दिया था"
    assert customer_evidence("device collected.", [{"role": "user", "text": "Device collected."}]) == "Device collected"


def test_evidence_keys_follow_existing_case_insensitive_field_matching():
    fields, sources = merge({"Already_Picked_Up": "Yes"}, {"ALREADY_PICKED_UP": "Device already collected"}, "Device already collected")
    assert fields["already_picked_up"] == "Yes"
    assert sources["already_picked_up"] == "analysis"


@pytest.mark.parametrize("status,expected", [
    ("requested", "Yes"), ("reported_updated", "No"),
    ("withdrawn", None), ("not_requested", "No"), ("invalid", None), (None, None),
])
def test_address_boolean_is_computed_from_validated_status(status, expected):
    fields, _ = merge(
        {"address_update_status": status, "address_change_requested": "Yes"},
        {"address_update_status": "Address ki baat"}, "Address ki baat",
    )
    assert fields["address_change_requested"] == expected


@pytest.mark.parametrize("status,expected", [
    ("requested", "Yes"), ("status_inquiry", "Yes"),
    ("reported_refunded", "No"), ("withdrawn", "No"), ("invalid", None), (None, None),
])
def test_refund_boolean_cannot_contradict_status(status, expected):
    fields, _ = merge(
        {"refund_status": status, "refund_requested": "Yes"},
        {"refund_status": "Refund ki baat"}, "Refund ki baat",
    )
    assert fields["refund_requested"] == expected


def test_address_details_require_supported_change_status_and_own_evidence():
    proposed = {"address_update_status": "requested", "updated_address": "Sector 14", "address_update_date": "2026-10-26"}
    fields, _ = merge(proposed, {"updated_address": "Sector 14"}, "Sector 14")
    assert fields["address_update_status"] is None
    assert fields["updated_address"] is None
    fields, _ = merge(proposed, {"address_update_status": "Address badal do"}, "Address badal do")
    assert fields["address_change_requested"] == "Yes"
    assert fields["updated_address"] is None
    assert fields["address_update_date"] is None


def test_dates_are_strict_and_long_addresses_survive_parse_merge_and_json():
    address = "Flat B_21, " + "Lotus Apartments, " * 12 + "Sector 62, Noida, Uttar Pradesh 201301"
    text = "Address change kar do: " + address
    raw = {"address_update_status": "requested", "updated_address": address, "address_update_date": "2026-02-31"}
    parsed = parse_analysis({"summary": "Address correction requested.", "structured_fields": raw})
    assert parsed.structured_fields["updated_address"] == address
    fields, sources = merge(parsed.structured_fields, {name: text for name in raw}, text)
    assert fields["updated_address"] == address
    assert display_structured_fields(enhance_summary_fields(ORIGINAL), fields)["updated_address"] == address
    assert fields["address_update_date"] is None
    assert "address_update_date" not in sources
    for invalid in ["kal", "2026-10-26T12:00:00", "26 October 2026"]:
        fields, _ = merge({**raw, "address_update_date": invalid}, {name: text for name in raw}, text)
        assert fields["address_update_date"] is None


def test_existing_fields_still_come_only_from_workflow_slots():
    fields, sources = merge(
        {"appointment_booked": "No", "address_verified": "Yes", "already_picked_up": "Yes"},
        {"already_picked_up": "Device already collected"}, "Device already collected",
        slots={"visit_status": "booked", "visit_date_iso": "2026-10-28"},
    )
    assert fields["appointment_booked"] == "Yes"
    assert fields["appointment_date"] == "2026-10-28"
    assert fields["address_verified"] is None
    assert sources["appointment_booked"] == "workflow"
    assert fields["already_picked_up"] == "Yes"  # independent customer report


def test_no_model_fallback_and_other_bots_do_not_invent_new_fields():
    fallback = fallback_analysis(final_state={"workflow_slots": {"visit_status": "incomplete"}}, policy=policy())
    assert all(fallback.structured_fields[f["name"]] is None for f in INTENT_SUMMARY_FIELDS)
    other = BotGoalPolicy.model_validate({"summaryFields": ORIGINAL})
    fields, _ = merge_structured_fields(other, {}, {"already_picked_up": "Yes"})
    assert "already_picked_up" not in fields


def test_prompt_has_full_rules_local_calendar_date_and_early_customer_turns():
    system = _build_system(policy(), reference=datetime(2026, 10, 26, 20, tzinfo=timezone.utc))
    assert "Call date in Asia/Kolkata: 2026-10-27" in system
    assert "The call happened on 2026-10-27" in system
    assert "kal update kiya tha" in system and "yesterday" in system
    assert "refund nahi mila" in system.lower()
    assert "structured_field_evidence" in system
    assert "computed by the platform" in system
    turns = [{"role": "user", "text": "Device already collected"}] + [{"role": "bot", "text": "Next"}] * 70
    assert "Device already collected" in _transcript_block(turns, detailed=True)
    assert "Device already collected" not in _transcript_block(turns)


@pytest.mark.parametrize("turn_number,accepted", [(2, True), (1, False), (0, False), (3, False), (-1, False), (True, False)])
async def test_analyst_turn_references_expand_only_actual_customer_text(turn_number, accepted):
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text=json.dumps({
        "summary": "Customer reported collection.", "structured_fields": {"already_picked_up": "Yes"},
        "structured_field_evidence": {"already_picked_up": turn_number},
    }))))
    turns = [{"role": "bot", "text": "Device collected?"}, {"role": "user", "text": "Device already collected."}]
    analysis, _ = await analyze_call(llm, policy=policy(), turns=turns, events=[], final_state={},
                                     reference=datetime(2026, 10, 27, tzinfo=timezone.utc))
    fields, _ = merge_structured_fields(policy(), {}, analysis.structured_fields,
                                       evidence=analysis.structured_field_evidence, turns=turns)
    assert fields["already_picked_up"] == ("Yes" if accepted else None)
    if accepted:
        assert analysis.structured_field_evidence["already_picked_up"] == turns[1]["text"]


async def test_worker_persists_fields_and_evidence_and_api_exposes_them(monkeypatch):
    """Run the real analyzer/merge/persistence/serializer with an isolated DB stub."""
    from backend.serializers import serialize_conversation_memory
    from shared.post_call import processor
    import shared.bot_config

    text = "Device already collected. Address already updated hai. Refund kab milega?"
    llm = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(
        text=json.dumps({
            "summary": "Customer reports collection and an address update; asks about refund.",
            "structured_fields": {"already_picked_up": "Yes", "address_update_status": "reported_updated", "refund_status": "status_inquiry", "refund_requested": "No"},
            "structured_field_evidence": {"already_picked_up": 1, "address_update_status": 1, "refund_status": 1},
        }), input_tokens=0, output_tokens=0,
    )))
    config = SimpleNamespace(goal_policy={"summaryFields": enhance_summary_fields(ORIGINAL)}, bot_name="Jeeves", system_prompt="", intents=[], llm={})
    monkeypatch.setattr(shared.bot_config, "resolve_bot_config", AsyncMock(return_value=config))
    monkeypatch.setattr(processor, "_load_row_sync", lambda _: {
        "conversation_id": "test_cv", "bot_id": BOT_ID, "session_id": "test_session", "language": "hi-IN",
        "final_state": {"workflow_slots": {"visit_status": "incomplete"}, "started_at": "2026-10-27T10:00:00+05:30"},
    })
    monkeypatch.setattr(processor, "_load_transcript_doc", AsyncMock(return_value={"turns": [{"role": "user", "text": text}]}))
    monkeypatch.setattr(processor, "build_analysis_llm", lambda _: llm)
    row = SimpleNamespace(conversation_id="test_cv")
    committed = []
    db = SimpleNamespace(get=lambda model, _: row if model is processor.ConversationMemory else None,
                         commit=lambda: committed.append(True), rollback=lambda: pytest.fail("Unexpected rollback"), close=lambda: None)
    monkeypatch.setattr(processor, "get_sessionmaker", lambda: lambda: db)
    assert await processor.process_memory_row("test_memory") is True
    assert committed == [True]
    payload = serialize_conversation_memory(row, enhance_summary_fields(ORIGINAL))
    assert payload["structuredFields"]["already_picked_up"] == "Yes"
    assert payload["structuredFields"]["address_change_requested"] == "No"
    assert payload["structuredFields"]["refund_requested"] == "Yes"
    assert payload["structuredFieldEvidence"]["refund_requested"] == text.rstrip("?")
    assert payload["structuredFieldLabels"]["already_picked_up"] == "Device already collected"
    assert payload["structuredFieldSources"]["refund_requested"] == "analysis"
    assert "structured_fields" in json.loads(json.dumps(row.memory))
