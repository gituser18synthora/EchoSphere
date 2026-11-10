"""Behavioral replay of the Haier no-defect branch through its real engine.

These tests isolate workflow execution; they do not stand in for a live-model
decision replay.  The recorded decision supplied no signal for the disputed
unresolved-problem answer.
"""
from __future__ import annotations

import asyncio
import copy

import pytest

from haier.setup.haier_unit_ok_repair import (
    APOLOGY_NODE,
    apply_unit_ok_repair,
)
from tests.golden.harness import patched


def _fixture() -> dict:
    def intent(node_id, prompt):
        return {"id": node_id, "kind": "intent", "config": {
            "prompt": prompt, "elseIsAnswer": True, "responseMode": "fixed"}}

    def edge(edge_id, source, target, label=""):
        return {"id": edge_id, "from": source, "to": target, "label": label}

    return {
        "id": "haier-unit-ok-test",
        "name": "Haier unit OK test",
        "version": 77,
        "nodes": [
            {"id": "start", "kind": "start", "config": {}},
            intent("n_reason_unit_ok_q", "Does the product still have a problem, or is it resolved?"),
            intent("n_reason_unit_ok_q_clarify", "Has the issue been resolved now?"),
            intent("n_service_need", "Do you require service from Haier for this request?"),
            intent("n_redispatch", "I am forwarding your request for re-dispatch. Is that okay?"),
            intent("n_resolved_how_q", "Was the issue fixed by a Haier technician?"),
            intent("n_v2_future_when", "When would you require the service?"),
            intent("n_future_confirm", "Do you require the service only after that later date?"),
            intent("n_v2_completed_check", "Is the service complete, with nothing pending?"),
            {"id": "unverified", "kind": "end", "config": {"text": "Status unconfirmed."}},
            {"id": "redispatch_outcome", "kind": "message", "config": {
                "silent": True, "setSlots": {"outcome": "redispatch"}}},
            {"id": "finish", "kind": "end", "config": {"text": "Thank you."}},
        ],
        "edges": [
            edge("entry", "start", "n_reason_unit_ok_q"),
            edge("e_unit_ok_resolved", "n_reason_unit_ok_q", "n_resolved_how_q",
                 "अब ठीक है/ठीक हो गया/issue resolved/problem fixed"),
            edge("e_unit_ok_issue", "n_reason_unit_ok_q", "n_service_need",
                 "काम नहीं कर रहा/ठीक नहीं हुआ/प्रॉब्लम है/problem persists/not working"),
            edge("e_unit_ok_bare", "n_reason_unit_ok_q", "n_reason_unit_ok_q_clarify",
                 "हाँ/yes/नहीं/no"),
            edge("e_v2_special_n_reason_unit_ok_q_n_v2_completed_check", "n_reason_unit_ok_q", "n_v2_completed_check",
                 "installation completed/काम हो गया"),
            edge("e_reason_unit_ok_q_else", "n_reason_unit_ok_q", "n_reason_unit_ok_q_clarify", "else"),
            edge("e_unit_ok_resolved__clarify", "n_reason_unit_ok_q_clarify", "n_resolved_how_q",
                 "हाँ/yes/ठीक हो गया/अब ठीक है"),
            edge("e_unit_ok_issue__clarify", "n_reason_unit_ok_q_clarify", "n_service_need",
                 "नहीं/no/ठीक नहीं हुआ/काम नहीं कर रहा"),
            edge("e_unit_ok_unknown__clarify", "n_reason_unit_ok_q_clarify", "unverified",
                 "पता नहीं/not sure/don't know"),
            edge("e_reason_unit_ok_q_clarify_else", "n_reason_unit_ok_q_clarify", "unverified", "else"),
            edge("e_redispatch_ok", "n_redispatch", "redispatch_outcome", "हाँ/yes/ठीक है/okay"),
            edge("e_redispatch_no", "n_redispatch", "n_service_need", "नहीं/no"),
            edge("e_v2_special_n_redispatch_n_v2_future_when", "n_redispatch", "n_v2_future_when",
                 "later/बाद में/अभी नहीं/abhi nahi/not now/है पर अभी नहीं/अभी नहीं चाहिए"),
            edge("e_v2_special_n_redispatch_n_future_confirm", "n_redispatch", "n_future_confirm",
                 "20 days/20 दिन बाद/next month"),
            edge("end", "redispatch_outcome", "finish"),
        ],
    }


async def _replay(turns):
    definition = apply_unit_ok_repair(_fixture())
    rows = []
    with patched(definition, []) as (engine, _):
        for index, (text, signal) in enumerate([("", None), *turns]):
            row = await engine.handle_turn_detailed(
                session_id="unit-ok-repair", tenant_id="haier-test", bot_id="haier-test",
                workflow_name=definition["name"], user_text=text, signal=signal,
                language="hi-IN", reset_state=(index == 0),
            )
            rows.append(row)
    return rows


@pytest.mark.parametrize("text", [
    "नहीं नहीं, अभी प्रॉब्लम है, सही से काम नहीं कर रहा।",
    "ठीक नहीं हुआ।",
    "The problem persists; it is not working.",
])
def test_confirmed_unresolved_apologises_then_proposes_redispatch(text):
    row = asyncio.run(_replay([(text, None)]))[-1]
    assert row["trace"][-1] == "n_redispatch"
    assert APOLOGY_NODE in row["trace"]
    assert "n_service_need" not in row["trace"]
    assert "माफ़ी चाहूँगी असुविधा के लिए।" in row["reply"]
    assert "re-dispatch" in row["reply"]
    assert "outcome" not in row["slots"]
    assert not row["done"]


@pytest.mark.parametrize(("answer", "signal"), [("हाँ", "affirm"), ("नहीं", "refusal")])
def test_bare_answer_to_two_part_status_question_still_clarifies(answer, signal):
    row = asyncio.run(_replay([(answer, signal)]))[-1]
    assert row["trace"][-1] == "n_reason_unit_ok_q_clarify"
    assert APOLOGY_NODE not in row["trace"]


def test_resolved_problem_still_verifies_who_fixed_it():
    row = asyncio.run(_replay([("अब ठीक है।", None)]))[-1]
    assert row["trace"][-1] == "n_resolved_how_q"
    assert APOLOGY_NODE not in row["trace"]


def test_negative_to_resolved_clarification_reaches_pending_branch():
    row = asyncio.run(_replay([("हाँ", "affirm"), ("नहीं", "refusal")]))[-1]
    assert row["trace"][-1] == "n_redispatch"
    assert APOLOGY_NODE in row["trace"]


def test_uncertain_status_never_becomes_redispatch():
    rows = asyncio.run(_replay([("पता नहीं।", None), ("पता नहीं।", None)]))
    assert all(APOLOGY_NODE not in row["trace"] for row in rows)
    assert "outcome" not in rows[-1]["slots"]


def test_customer_can_decline_proposal_before_outcome_is_recorded():
    row = asyncio.run(_replay([("प्रॉब्लम है।", None), ("नहीं", "refusal")]))[-1]
    assert row["trace"][-1] == "n_service_need"
    assert "outcome" not in row["slots"]
    assert not row["done"]


def test_outcome_requires_acknowledging_proposal():
    row = asyncio.run(_replay([("प्रॉब्लम है।", None), ("ठीक है", "affirm")]))[-1]
    assert row["done"]
    assert row["slots"]["outcome"] == "redispatch"


@pytest.mark.parametrize("clarify", [False, True])
@pytest.mark.parametrize(("text", "expected"), [
    ("Service अभी नहीं चाहिए, 20 दिन बाद चाहिए।", "n_future_confirm"),
    ("मुझे service बाद में चाहिए।", "n_v2_future_when"),
])
def test_volunteered_later_service_uses_existing_timing_flow(clarify, text, expected):
    turns = [("हाँ", "affirm")] if clarify else []
    row = asyncio.run(_replay([*turns, (text, None)]))[-1]
    assert row["trace"][-1] == expected
    assert APOLOGY_NODE not in row["trace"]
    assert "n_redispatch" not in row["trace"]
    assert "outcome" not in row["slots"]


def test_not_yet_to_resolved_question_means_pending_not_postponed_service():
    row = asyncio.run(_replay([("हाँ", "affirm"), ("अभी नहीं", "refusal")]))[-1]
    assert row["trace"][-1] == "n_redispatch"
    assert APOLOGY_NODE in row["trace"]
    assert "n_v2_future_when" not in row["trace"]


def test_bare_not_yet_to_initial_two_part_question_still_clarifies():
    row = asyncio.run(_replay([("अभी नहीं", "refusal")]))[-1]
    assert row["trace"][-1] == "n_reason_unit_ok_q_clarify"


def test_status_future_routes_only_use_existing_unambiguous_aliases():
    original = _fixture()
    repaired = apply_unit_ok_repair(original)
    source_aliases = {
        alias for edge in original["edges"]
        if edge["from"] == "n_redispatch" and "future" in edge["id"]
        for alias in edge["label"].split("/")
    }
    added = [edge for edge in repaired["edges"]
             if edge["id"].startswith("e_haier_") and edge["label"] != "correction"]
    assert len(added) == 4
    for edge in added:
        assert set(edge["label"].split("/")) <= source_aliases
        assert not {"अभी नहीं", "abhi nahi", "not now", "है पर अभी नहीं"} & set(edge["label"].split("/"))


def test_a_past_duration_with_completed_service_keeps_completed_checks():
    row = asyncio.run(_replay([("Installation completed 20 days ago.", None)]))[-1]
    assert row["trace"][-1] == "n_v2_completed_check"
    assert "n_future_confirm" not in row["trace"]


def test_existing_completed_special_keeps_priority_over_timing_capture():
    row = asyncio.run(_replay([("Installation completed; service later, 20 days.", None)]))[-1]
    assert row["trace"][-1] == "n_v2_completed_check"


def test_repair_does_not_mutate_input_and_is_idempotent():
    original = _fixture()
    unchanged = copy.deepcopy(original)
    repaired = apply_unit_ok_repair(original)
    assert original == unchanged
    assert apply_unit_ok_repair(repaired) == repaired


def test_unexpected_revision_fails_closed():
    original = _fixture()
    next(e for e in original["edges"] if e["id"] == "e_unit_ok_issue")["to"] = "unexpected"
    with pytest.raises(ValueError, match="pending-status edge"):
        apply_unit_ok_repair(original)
