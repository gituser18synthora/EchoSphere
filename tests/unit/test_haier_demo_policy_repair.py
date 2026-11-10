"""Haier terminal outcomes outrank contradictory narrative completion guesses.

The frozen v3 policy contains configuration only, with no call transcripts or
customer records.  Every existing outcome remains covered so completion
repair cannot change Haier's established disposition and resolution rules.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from haier.setup.haier_demo_policy_repair import repair_summary_policy
from shared.orchestration.goal_engine import BotGoalPolicy
from shared.post_call.structured import merge_structured_fields


BASE_POLICY = json.loads((
    Path(__file__).parent / "fixtures" / "haier_demo_summary_policy_v3.json"
).read_text(encoding="utf-8"))
OUTCOMES = next(
    field["options"] for field in BASE_POLICY["summaryFields"]
    if field["name"] == "workflow_outcome"
)


def _policy(*, repaired=True):
    raw = repair_summary_policy(BASE_POLICY) if repaired else BASE_POLICY
    return BotGoalPolicy.model_validate(raw)


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_terminal_outcome_survives_caller_hangup_and_wrong_analyst_midflow(outcome):
    # The analyst can see caller_hangup and wrongly conclude that the call
    # ended midway even though the existing flow already recorded an outcome.
    # Merge must keep the authoritative guided-flow completion instead.
    final_state = {"workflow_slots": {"outcome": outcome}, "end_reason": "caller_hangup"}
    fields, sources = merge_structured_fields(
        _policy(), final_state["workflow_slots"],
        {"call_completion": "customer_disconnected_midway"},
        turns=[
            {"role": "user", "text": "ठीक है, ओके जी।"},
            {"role": "bot", "text": "Confirm करने के लिए धन्यवाद।"},
        ],
    )
    assert final_state["end_reason"] == "caller_hangup"
    assert fields["call_completion"] == "flow_completed"
    assert sources["call_completion"] == "workflow"
    assert fields["workflow_outcome"] == outcome


def test_recorded_redispatch_regression_before_and_after():
    slots = {"outcome": "redispatch"}
    analyst = {"call_completion": "customer_disconnected_midway"}
    before, before_sources = merge_structured_fields(_policy(repaired=False), slots, analyst)
    after, after_sources = merge_structured_fields(_policy(), slots, analyst)
    assert before["call_completion"] == "customer_disconnected_midway"
    assert before_sources["call_completion"] == "analysis"
    assert after["call_completion"] == "flow_completed"
    assert after_sources["call_completion"] == "workflow"
    assert after["disposition"] == "Connected – Service Required (Re-dispatch)"
    assert after["backend_action_status"] == "no_integration_configured"


@pytest.mark.parametrize("slots", [{}, {"outcome": None}, {"outcome": ""}, {"outcome": "unrecognized_future_outcome"}])
@pytest.mark.parametrize("analyst_completion", ["customer_disconnected_midway", "ended_without_outcome"])
def test_no_terminal_outcome_leaves_completion_to_analysis(slots, analyst_completion):
    fields, sources = merge_structured_fields(
        _policy(), slots, {"call_completion": analyst_completion},
    )
    assert fields["call_completion"] == analyst_completion
    assert sources["call_completion"] == "analysis"


@pytest.mark.parametrize("slots", [{}, {"outcome": "unrecognized_future_outcome"}])
def test_no_outcome_or_valid_analyst_never_invents_completion(slots):
    fields, sources = merge_structured_fields(_policy(), slots, {"call_completion": "probably_completed"})
    assert fields["call_completion"] is None
    assert "call_completion" not in sources


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_every_existing_disposition_resolution_and_service_mapping_is_preserved(outcome):
    slots = {"outcome": outcome}
    proposed = {
        "disposition": "Not Connected – Switch off",
        "workflow_outcome": "cancel_no_service",
        "issue_resolved": "not_resolved",
        "service_required": "no",
        "backend_action_status": "confirmed",
        "call_completion": "customer_disconnected_midway",
    }
    before, before_sources = merge_structured_fields(_policy(repaired=False), slots, proposed)
    after, after_sources = merge_structured_fields(_policy(), slots, proposed)
    assert {key: value for key, value in after.items() if key not in ("call_completion", "issue_resolved")} == {
        key: value for key, value in before.items() if key not in ("call_completion", "issue_resolved")
    }
    assert {key: value for key, value in after_sources.items() if key not in ("call_completion", "issue_resolved")} == {
        key: value for key, value in before_sources.items() if key not in ("call_completion", "issue_resolved")
    }
    if outcome != "close_completed":
        assert after["issue_resolved"] == before["issue_resolved"]
    else:
        assert after["issue_resolved"] is None
    assert after["workflow_outcome"] == outcome
    assert after["backend_action_status"] == "no_integration_configured"


@pytest.mark.parametrize(("outcome", "disposition", "issue_resolved"), [
    ("redispatch_completion", "Connected – Issue Resolved by Haier Tech", "resolved_by_haier_technician"),
    ("close_completed_other", "Connected – Issue Resolved (Either by customer or local Tech.)", "resolved_without_haier"),
    ("cancel_resolved", "Connected – Issue Resolved (Either by customer or local Tech.)", "resolved_without_haier"),
    ("redispatch", "Connected – Service Required (Re-dispatch)", "not_resolved"),
])
def test_previously_validated_completion_dispositions_remain_exact(outcome, disposition, issue_resolved):
    fields, sources = merge_structured_fields(
        _policy(), {"outcome": outcome},
        {"disposition": "Not Connected – Switch off", "issue_resolved": "not_resolved"},
    )
    assert fields["disposition"] == disposition
    assert fields["issue_resolved"] == issue_resolved
    assert sources["disposition"] == sources["issue_resolved"] == "workflow"


def test_policy_transform_is_isolated_and_preserves_existing_vocabularies():
    original = copy.deepcopy(BASE_POLICY)
    updated = repair_summary_policy(BASE_POLICY)
    assert BASE_POLICY == original
    assert repair_summary_policy(updated) == updated
    by_name = {field["name"]: field for field in updated["summaryFields"]}
    for field in original["summaryFields"]:
        if field["name"] == "technician_contact":
            # The actual analyst regression covers ambiguous contact. Only
            # its extraction guidance changes; vocabulary and evidence rules
            # remain the existing contract.
            assert {key: value for key, value in by_name[field["name"]].items()
                    if key not in ("description", "extractionInstructions")} == {
                key: value for key, value in field.items()
                if key not in ("description", "extractionInstructions")
            }
        elif field["name"] == "issue_resolved":
            expected = copy.deepcopy(field)
            expected.setdefault("values", {})["close_completed"] = ""
            assert by_name[field["name"]] == expected
        elif field["name"] != "call_completion":
            assert by_name[field["name"]] == field
        else:
            assert by_name[field["name"]]["options"] == field["options"]
            assert by_name[field["name"]]["allowLlm"] == field["allowLlm"]


def test_missing_authoritative_outcome_field_rejects_policy_repair():
    unrelated = {"summaryFields": [{"name": "call_completion", "type": "choice"}]}
    with pytest.raises(ValueError, match="summary fields"):
        repair_summary_policy(unrelated)


def test_unknown_installer_cannot_be_guessed_as_non_haier_resolution():
    text = "I do not know who installed it; I was not at home."
    fields, sources = merge_structured_fields(
        _policy(), {"outcome": "close_completed"},
        {"issue_resolved": "resolved_without_haier"},
        evidence={"issue_resolved": text},
        turns=[{"role": "user", "text": text}],
    )
    assert fields["issue_resolved"] is None
    assert "issue_resolved" not in sources
    assert fields["disposition"] == "Unmapped (needs review)"
