"""Caller control remarks must never become digits or discard a partial ID.

The quoted Hindi turns reproduce cv_0a537c60c4d4. All workflow state is in
memory; the lookup connection is mocked and no customer data is required.
"""
import copy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as we
from shared.orchestration.ask_resolution import identifier_readback_request
from shared.orchestration.identifier_confirmation import spoken_readback
from tenant.Wordline.setup.identifier_confirmation import TID_PATTERN


def definition(pattern=TID_PATTERN):
    return {"id": "wf_partial_recovery", "version": 1, "name": "Partial recovery", "nodes": [
        {"id": "start", "kind": "start", "config": {}},
        {"id": "ask", "kind": "ask", "config": {
            "variable": "terminal_id", "question": "Please tell me your complete Terminal ID.",
            "entity": {"dataType": "identifier", "preserveAlphanumeric": True,
                       "regexPattern": pattern,
                       "synonymPatterns": {"no_tid": [r"I do not have it", r"पता नहीं"]}}}},
        {"id": "unavailable", "kind": "condition", "config": {
            "variable": "terminal_id", "operator": "equals", "value": "no_tid"}},
        {"id": "none", "kind": "end", "config": {"text": "Without an ID I cannot proceed."}},
        {"id": "lookup", "kind": "api", "config": {"connection": "lookup"}},
        {"id": "done", "kind": "end", "config": {"text": "Thank you."}},
    ], "edges": [
        {"from": "start", "to": "ask"}, {"from": "ask", "to": "unavailable"},
        {"from": "ask", "to": "none", "label": "fallback"},
        {"from": "unavailable", "to": "none", "label": "true"},
        {"from": "unavailable", "to": "lookup", "label": "false"},
        {"from": "lookup", "to": "done", "label": "success"},
    ]}


def runtime(monkeypatch, doc=None, language="hi-IN"):
    doc = doc or definition()
    frozen = copy.deepcopy(doc)
    monkeypatch.setattr(we, "load_workflow_definition", lambda *args: doc)
    workflow, saver = we.WorkflowEngine(), MemorySaver()

    async def memory():
        return saver

    monkeypatch.setattr(workflow, "_get_checkpointer", memory)

    async def turn(text, signal=None):
        result = await workflow.handle_turn_detailed(
            session_id="partial-recovery", tenant_id="tenant", bot_id="bot", workflow_name=doc["id"],
            user_text=text, language=language, signal=signal,
            mock_tool_results={"lookup": {"ok": True}})
        assert doc == frozen
        return result

    return turn


@pytest.mark.parametrize("readback_request", [
    "कौन सा नोट किया है फिर से बोलिए एक बार।",
    "क्या नोट किया है बताओ तो इसे",
    "अब तक क्या नोट किया?",
    "अब तक क्या लिखा?",
    "what number did you hear?",
    "read that back",
])
async def test_readback_request_wins_over_incidental_restart_and_digit_words(monkeypatch, readback_request):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    partial = await turn("One Two Three Four B, B for ball.")
    assert partial["awaitingIdentifier"]["held_digits"] == "1234B"
    result = await turn(readback_request, "question")
    assert result["awaitingIdentifier"]["held_digits"] == "1234B"
    assert spoken_readback("1234B") in result["reply"]
    assert not result["offScript"] and not result["done"]
    assert not result["awaitingIdentifierConfirmation"]
    assert not result["slots"].get("terminal_id") and "lookup" not in result["trace"]


async def test_invalid_full_restatement_is_retained_without_stale_prefix_or_acceptance(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("one")
    await turn("one")
    fresh = await turn("एक दो तीन चार B for ball, paanch, chha, saat.")
    assert fresh["awaitingIdentifier"]["held_digits"] == "1234B567"
    assert "7 अंक" in fresh["reply"]
    assert spoken_readback("1234B567") in fresh["reply"]
    assert "आगे बताइए" not in fresh["reply"]
    assert not fresh["awaitingIdentifierConfirmation"] and not fresh["done"]
    assert not fresh["slots"].get("terminal_id") and "lookup" not in fresh["trace"]


@pytest.mark.parametrize("completion", ["बस इतने ही हैं।", "that's all", "this is it"])
async def test_completion_remark_cannot_turn_an_invalid_candidate_into_unavailable_or_fallback(monkeypatch, completion):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    # Exhaust the ordinary retry allowance; a completion remark still cannot
    # take the authored fallback that says the caller has no ID.
    await turn("unintelligible")
    await turn("unclear")
    await turn("one two three four B for ball five six seven")
    result = await turn(completion, "deny")
    assert result["awaitingIdentifier"]["held_digits"] == "1234B567"
    assert not result["done"] and not result["awaitingIdentifierConfirmation"]
    assert not result["slots"].get("terminal_id")
    assert spoken_readback("1234B567") in result["reply"]
    assert "आगे बताइए" not in result["reply"]
    assert "none" not in result["trace"] and "lookup" not in result["trace"]
    assert "Without an ID" not in result["reply"]


async def test_valid_chunk_accumulation_wins_when_both_fresh_and_combined_lengths_are_allowed(monkeypatch):
    turn = runtime(monkeypatch, definition(r"^(\d{7,10})$"))
    await turn("Please help me")
    await turn("one two three")
    completed = await turn("four five six seven eight nine zero")
    assert completed["awaitingIdentifierConfirmation"]
    assert "lookup" not in completed["trace"]
    assert spoken_readback("1234567890") in completed["reply"]
    confirmed = await turn("yes")
    assert confirmed["done"] and confirmed["slots"]["terminal_id"] == "1234567890"


async def test_explicit_unavailable_answer_keeps_its_authored_exit(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("one two three four")
    result = await turn("I do not have it")
    assert result["done"] and result["slots"]["terminal_id"] == "no_tid"
    assert "none" in result["trace"] and "lookup" not in result["trace"]


async def test_exact_spelling_reminder_does_not_append_the_same_letter_twice(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    partial = await turn("मेरी ID है 123 B, B for boy.")
    assert partial["awaitingIdentifier"]["held_digits"] == "123B"
    repeated = await turn("मैंने अभी बोला ना बी फॉर बॉल।")
    assert repeated["awaitingIdentifier"]["held_digits"] == "123B"
    assert spoken_readback("123B") in repeated["reply"]
    assert not repeated["awaitingIdentifierConfirmation"] and "lookup" not in repeated["trace"]


async def test_explicit_double_letter_continuation_is_preserved(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("one two three four")
    result = await turn("double B")
    assert result["awaitingIdentifier"]["held_digits"] == "1234BB"
    assert not result["awaitingIdentifierConfirmation"] and "lookup" not in result["trace"]


async def test_explicit_restart_with_an_invalid_new_attempt_discards_old_digits(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("nine nine")
    result = await turn("start again one two three four B for ball five six seven")
    assert result["awaitingIdentifier"]["held_digits"] == "1234B567"
    assert not result["awaitingIdentifierConfirmation"] and "lookup" not in result["trace"]


async def test_extended_spelled_partial_is_a_fresh_attempt_not_a_duplicate_prefix(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("मेरी ID है 123 B, B for boy.")
    result = await turn("One Two Three Four B, B for ball.")
    assert result["awaitingIdentifier"]["held_digits"] == "1234B"
    assert not result["awaitingIdentifierConfirmation"] and "lookup" not in result["trace"]


async def test_repeated_digit_prefix_without_spelled_ending_is_still_a_legal_chunk(monkeypatch):
    turn = runtime(monkeypatch)
    await turn("Please help me")
    await turn("one one")
    result = await turn("one one two two")
    assert result["awaitingIdentifier"]["held_digits"] == "111122"
    assert not result["awaitingIdentifierConfirmation"] and "lookup" not in result["trace"]


async def test_custom_structured_id_prefix_remains_appendable_when_not_yet_a_full_match(monkeypatch):
    turn = runtime(monkeypatch, definition(r"^(\d{3}[A-Z]\d{4}[A-Z]\d)$"))
    await turn("Please help me")
    await turn("123B")
    partial = await turn("1234B")
    assert partial["awaitingIdentifier"]["held_digits"] == "123B1234B"
    assert not partial["awaitingIdentifierConfirmation"] and "lookup" not in partial["trace"]
    complete = await turn("5")
    assert complete["awaitingIdentifierConfirmation"]
    assert spoken_readback("123B1234B5") in complete["reply"]
    assert "lookup" not in complete["trace"]
    confirmed = await turn("yes")
    assert confirmed["done"] and confirmed["slots"]["terminal_id"] == "123B1234B5"


@pytest.mark.parametrize("text", [
    "what did you note? wrong, start again",
    "क्या नोट किया? गलत, दोबारा",
    "क्या बैंक नोट बदलना होगा?",
    "what did you hear about the refund policy?",
])
def test_readback_fast_path_rejects_explicit_corrections_and_unrelated_questions(text):
    assert not identifier_readback_request(text)


@pytest.mark.parametrize("entity,variable", [
    ({"dataType": "text", "regexPattern": r"^(\d{10})$"}, "phone_number"),
    ({"dataType": "identifier", "regexPattern": r"^(\d{10})$"}, "phone_number"),
    ({"dataType": "text", "semanticType": "phone", "regexPattern": r"^(\d{10})$"}, "opaque_value"),
])
async def test_inferred_phone_partial_readback_and_completion_remain_masked(monkeypatch, entity, variable):
    doc = definition()
    config = doc["nodes"][1]["config"]
    config.update(variable=variable, entity=entity)
    turn = runtime(monkeypatch, doc, language="en-US")
    await turn("Please help me")
    await turn("nine eight seven six five four")
    for text in ("what did you note?", "that's all"):
        result = await turn(text)
        assert result["awaitingIdentifier"]["held_digits"] == "987654"
        assert "five, four" in result["reply"]
        assert "nine" not in result["reply"] and "eight" not in result["reply"]
        assert "six digits" in result["reply"] or "6 digits" in result["reply"]
        assert not result["awaitingIdentifierConfirmation"] and "lookup" not in result["trace"]
