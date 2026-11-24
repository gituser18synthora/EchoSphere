"""The current business step owns a refusal after identity is confirmed."""
import pytest

from shared.orchestration.goal_engine import GoalEngine
from tests.unit.test_goal_engine import HEALTHCARE_POLICY, LOAN_POLICY, _DecisionLLM


@pytest.mark.parametrize("policy", (HEALTHCARE_POLICY, LOAN_POLICY))
@pytest.mark.parametrize("fast_output", (False, True))
async def test_provider_receives_business_question_ownership_and_scoped_identity_rules(policy, fast_output):
    llm = _DecisionLLM({
        "scope": "in_scope", "signal": "refusal", "decision": None,
        "confidence": 0.99, "next_action": "continue_workflow",
        "needs_clarification": False,
    })
    engine = GoalEngine(llm=llm, policy=policy, fast_output=fast_output)
    state = {
        "identity_state": "confirmed", "workflow_stage": "workflow:follow_up",
        "pending_question": "May I arrange the requested visit?",
        "expected_answer": "yes/no",
    }
    result = await engine.decide("No, I do not need that service.", [], state=state)
    system = llm.calls[-1]["system"]
    payload = llm.calls[-1]["messages"][-1]["content"]
    assert "primary goal and broad intent examples do not replace it" in system
    assert "Denying a product, service, event or proposed action is not denying who is speaking" in system
    assert "Do not require confirmation of an unambiguous refusal" in system
    assert "ordinary business answers and action permission use null" in system
    assert "denied identity/verification gate re-asks" in system
    assert "May I arrange the requested visit?" in payload
    assert "confirmed" in payload and "workflow:follow_up" in payload
    assert result.decision is None
    assert result.signal == "refusal" and not result.needs_clarification
    assert result.next_action == "continue_workflow"


def test_generic_ownership_rules_do_not_embed_tenant_product_or_phrase_examples():
    system = GoalEngine(llm=None)._build_system()
    ownership = system.split("- Pending-question ownership:", 1)[1].split("- slots:", 1)[0]
    assert "Haier" not in ownership
    assert "technician" not in ownership
    assert "नहीं" not in ownership
    assert "person/name/number mismatch" in ownership
