"""Bill time facts stay independent from warranty and existing outcomes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from haier.setup.haier_bill_summary_policy_repair import repair_summary_policy
from shared.orchestration.goal_engine import BotGoalPolicy
from shared.post_call.structured import (
    derive_structured_fields,
    evidence_fields_prompt_block,
    merge_structured_fields,
    summary_fields_prompt_block,
)


def fixture():
    policy = json.loads((Path(__file__).parent / "fixtures" / "haier_demo_summary_policy_v3.json").read_text())
    policy["summaryFields"].insert(7, {
        "name": "warranty_claimed", "type": "yes_no", "source": "warranty_claimed",
        "values": {"yes": "Yes"}, "allowLlm": True, "evidenceRequired": True,
        "openWhenUnmapped": True,
        "description": "Customer claims warranty or questions extra charges.",
        "extractionInstructions": "Warranty mein hai or extra charge kyun = Yes.",
    })
    return policy


def compiled():
    return BotGoalPolicy.model_validate(repair_summary_policy(fixture()))


@pytest.mark.parametrize(("at_visit", "now"), [("no", "yes"), ("yes", "yes"), ("no", "no"), ("yes", "no")])
def test_explicit_bill_time_slots_are_independent_and_never_imply_warranty(at_visit, now):
    fields = derive_structured_fields(compiled(), {
        "bill_available_at_visit": at_visit, "bill_available_now": now,
    })
    assert fields["bill_available_at_visit"] == {"yes": "Yes", "no": "No"}[at_visit]
    assert fields["bill_available_now"] == {"yes": "Yes", "no": "No"}[now]
    assert fields["warranty_claimed"] is None


def test_workflow_bill_correction_outranks_an_analyst_reusing_the_historical_answer():
    fields, sources = merge_structured_fields(
        compiled(), {"bill_available_at_visit": "no", "bill_available_now": "yes"},
        {"bill_available_at_visit": "Yes", "bill_available_now": "No"},
    )
    assert fields["bill_available_at_visit"] == "No"
    assert fields["bill_available_now"] == "Yes"
    assert sources["bill_available_at_visit"] == sources["bill_available_now"] == "workflow"


def test_older_call_can_extract_both_bill_times_from_one_attributed_correction():
    text = "उस समय नहीं था, अभी है मेरे पास।"
    fields, sources = merge_structured_fields(
        compiled(), {},
        {"bill_available_at_visit": "No", "bill_available_now": "Yes", "warranty_claimed": None},
        evidence={"bill_available_at_visit": text, "bill_available_now": text},
        turns=[{"role": "user", "text": text}],
    )
    assert fields["bill_available_at_visit"] == "No"
    assert fields["bill_available_now"] == "Yes"
    assert fields["warranty_claimed"] is None
    assert sources["bill_available_now"] == sources["bill_available_at_visit"] == "analysis"


@pytest.mark.parametrize("evidence", [{}, {"bill_available_now": "I have the bill"}])
def test_bill_analyst_value_without_matching_customer_evidence_stays_unknown(evidence):
    fields, _ = merge_structured_fields(
        compiled(), {}, {"bill_available_now": "Yes"}, evidence=evidence,
        turns=[{"role": "bot", "text": "I have the bill"}, {"role": "user", "text": "Maybe"}],
    )
    assert fields["bill_available_now"] is None


def test_explicit_warranty_slot_remains_authoritative_and_does_not_set_bill_facts():
    fields, sources = merge_structured_fields(compiled(), {"warranty_claimed": "yes"}, {})
    assert fields["warranty_claimed"] == "Yes"
    assert sources["warranty_claimed"] == "workflow"
    assert fields["bill_available_now"] is fields["bill_available_at_visit"] is None


def test_caveats_reach_both_actual_summary_requests_without_conversation_prompt_changes():
    policy = compiled()
    evidence_prompt = evidence_fields_prompt_block(policy)
    narrative_prompt = summary_fields_prompt_block(policy)
    assert "generic charge objection or question" in evidence_prompt
    assert "jab bill hai to service charges kyun doon" in evidence_prompt
    assert "earlier time only" in evidence_prompt
    assert "current time only" in evidence_prompt
    assert "neither slot proves that a visit occurred" in narrative_prompt
    assert "does not establish warranty" in narrative_prompt


def test_repair_is_pure_idempotent_and_preserves_all_outcome_and_consent_contracts():
    before = fixture()
    original = deepcopy(before)
    after = repair_summary_policy(before)
    assert before == original
    assert repair_summary_policy(after) == after
    by_name = {field["name"]: field for field in after["summaryFields"]}
    for field in before["summaryFields"]:
        name = field["name"]
        ignored = {"description", "extractionInstructions"} if name == "warranty_claimed" else (
            {"extractionInstructions"} if name == "call_completion" else set())
        assert {k: v for k, v in field.items() if k not in ignored} == {
            k: v for k, v in by_name[name].items() if k not in ignored}
    for outcome in next(f["options"] for f in before["summaryFields"] if f["name"] == "workflow_outcome"):
        old = derive_structured_fields(BotGoalPolicy.model_validate(before), {"outcome": outcome})
        new = derive_structured_fields(BotGoalPolicy.model_validate(after), {"outcome": outcome})
        assert {name: new[name] for name in old} == old


@pytest.mark.parametrize("change", ["missing", "duplicate", "source", "type", "values", "existing_bill"])
def test_unreviewed_contracts_fail_closed(change):
    policy = fixture()
    warranty = next(f for f in policy["summaryFields"] if f["name"] == "warranty_claimed")
    if change == "missing":
        policy["summaryFields"].remove(warranty)
    elif change == "duplicate":
        policy["summaryFields"].append(deepcopy(warranty))
    elif change == "existing_bill":
        policy["summaryFields"].append({"name": "bill_available_now", "type": "text"})
    else:
        warranty[change] = {"yes": "Sure"} if change == "values" else "unreviewed"
    with pytest.raises(ValueError):
        repair_summary_policy(policy)
