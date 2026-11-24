"""Generated factual answers can contain embedded wh-clauses."""

import time

import pytest

from shared.orchestration.generated_questions import is_generated_question_clause
from shared.orchestration.router import RouteDecision, RouteKind, looks_like_question
from tests.unit.test_brain_workflow_routing import _LLMStub, _WorkflowStub, make_brain


@pytest.mark.parametrize("clause,following", [
    ("Technician की visit कहाँ हुई थी", "इसकी जानकारी मेरे पास नहीं है।"),
    ("Delivery kahan hui thi", "iski jankari mere paas nahi hai."),
    ("Where the appointment took place", "I do not know."),
    ("I do not know where the appointment took place.", ""),
    ("We cannot confirm when it happened.", ""),
    ("I understand how frustrating that is.", ""),
    ("मुझे पता है कि यह कितना परेशान करने वाला है।", ""),
])
def test_embedded_wh_declarations_do_not_ask_the_caller_for_an_answer(clause, following):
    assert looks_like_question(clause)
    assert not is_generated_question_clause(clause, following)


@pytest.mark.parametrize("clause,following", [
    ("Where did the technician visit?", "I do not know."),
    ("Technician कहाँ आया था?", "इसकी जानकारी मेरे पास नहीं है।"),
    ("आप कहाँ रहते हैं", ""),
    ("Do you know where it happened", ""),
    ("क्या मैं आपकी request आगे बढ़ा दूँ", ""),
    ("Please confirm whether I should send the technician.", ""),
    ("Tell me where it happened", "I do not know."),
    ("Please provide your phone number.", ""),
    ("बताइए कहाँ हुआ था", ""),
    ("bataiye aapko service chahiye", ""),
    ("எங்கிருந்து அழைக்கிறீர்கள்?", ""),
    ("എവിടെയാണ്?", ""),
])
def test_new_direct_questions_and_value_requests_remain_suppressed(clause, following):
    assert is_generated_question_clause(clause, following)


@pytest.mark.parametrize("answer", [
    "Technician की visit कहाँ हुई थी, इसकी जानकारी मेरे पास नहीं है।",
    "Delivery kahan hui thi, iski jankari mere paas nahi hai.",
    "I do not know where the appointment took place.",
])
@pytest.mark.parametrize("extra_question", ["", " May I forward your request for a technician visit?"])
async def test_actual_question_lock_preserves_factual_wh_answer_then_resumes_authored_question(
    answer, extra_question,
):
    pending = "Did the technician call or visit?"
    llm = _LLMStub(reply=answer + extra_question)
    workflow = _WorkflowStub({})
    brain = make_brain(llm=llm, workflow_engine=workflow)
    brain._active_workflow = "tenant_flow"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = pending
    brain._history = [{"role": "user", "content": "I meant where the visit took place."}]

    await brain._generate_reply(
        "I meant where the visit took place.",
        RouteDecision(kind=RouteKind.CHAT, signal="question", reason="context_clarification_followup"),
        time.perf_counter(),
        question_response=True, pending_question=pending,
        lock_workflow_question=True,
    )

    spoken = [turn.text for turn in brain._recorder.turns if turn.role == "bot"]
    assert spoken == [answer, pending]
    assert "May I forward" not in " ".join(spoken)
    assert workflow.calls == []
    assert brain._pending_workflow_question == pending
    if extra_question:
        assert any(kind == "workflow_generated_question_suppressed" for kind, _ in brain._recorder.events)
