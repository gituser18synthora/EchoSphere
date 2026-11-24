"""A caller's ambiguous question can be clarified without answering a gate."""

import copy
import json
import time

import pytest

from shared.orchestration.context_questions import (
    CONTEXT_QUESTION_CLASSIFICATION,
    CONTEXT_QUESTION_RESPONSE,
    CONTEXT_QUESTION_TURN,
)
from shared.orchestration.decision_schema import ConversationDecision
from shared.orchestration.goal_engine import GoalEngine, GoalSession
from shared.orchestration.router import RouteDecision, RouteKind
from tests.unit.test_brain_workflow_routing import _LLMStub, _WorkflowStub, make_brain
from tests.unit.test_goal_engine import LOAN_POLICY, _DecisionLLM


def _context_decision(**changes):
    return ConversationDecision.model_validate({
        "signal": "question",
        "confidence": .96,
        "context_question": True,
        "needs_clarification": True,
        "decision": "ambiguous",
        "next_action": "clarify",
        **changes,
    })


def test_ambiguous_context_referent_survives_validation_without_answering_gate():
    decision = _context_decision()

    assert decision.context_question
    assert decision.needs_clarification
    assert decision.next_action == "answer"
    assert decision.decision == "unrelated"
    assert decision.slots == {}
    assert decision.tool_request is None
    assert decision.as_event()["needs_clarification"] is True

    session = GoalSession(LOAN_POLICY)
    before = copy.deepcopy(session.live_state())
    session.apply(decision)
    assert session.live_state() == before
    assert session.identity_attempts == 0
    assert session.slots == {}
    assert not session.end_call_reason(decision)


@pytest.mark.parametrize("changes", [
    {"scope": "out_of_scope"},
    {"scope": "injection_attempt"},
    {"confidence": .59},
    {"signal": "affirm"},
    {"decision": "confirmed"},
    {"decision": "denied"},
    {"slots": {"transaction_reference": {"status": "provided", "value": "123456"}}},
    {"tool_request": "check_payment_status"},
    {"next_action": "answer_from_knowledge"},
    {"next_action": "call_tool"},
    {"next_action": "end_call"},
])
def test_clarification_flag_cannot_bypass_existing_context_question_gates(changes):
    decision = _context_decision(**changes)
    assert not decision.context_question
    if changes.get("scope") in ("out_of_scope", "injection_attempt"):
        assert decision.slots == {}
        assert decision.tool_request is None
        assert decision.next_action == "redirect_to_goal"


def test_clear_context_question_does_not_gain_a_clarification_request():
    decision = ConversationDecision(
        signal="question", confidence=.96, context_question=True,
    )
    assert decision.context_question
    assert not decision.needs_clarification
    assert decision.next_action == "answer"


@pytest.mark.parametrize("fast_output", [False, True])
async def test_stage_a_actual_payload_distinguishes_ambiguity_from_missing_facts(fast_output):
    llm = _DecisionLLM({
        "signal": "question", "confidence": .96, "scope": "in_scope",
        "context_question": True, "needs_clarification": True,
        "next_action": "answer",
    })
    engine = GoalEngine(
        llm=llm, policy=LOAN_POLICY, fast_output=fast_output,
        include_response_text=False,
    )
    pending = "Has the technician called, or has a visit taken place?"
    last = "The field team rejected the request. " + pending
    decision = await engine.decide(
        "कहाँ हुआ था?",
        [{"role": "assistant", "content": last}],
        state={"pending_question": pending, "last_bot_question": last},
    )

    assert decision is not None and decision.context_question
    assert decision.needs_clarification
    assert len(llm.calls) == 1
    payload = llm.calls[0]
    assert CONTEXT_QUESTION_CLASSIFICATION in payload["system"]
    assert "Missing information alone does not make the question's meaning ambiguous" in payload["system"]
    assert "Keep needs_clarification=false for a clear question answerable from known facts" in payload["system"]
    assert "context_question=true, signal=question and next_action=answer" in payload["system"]
    assert pending in payload["messages"][0]["content"]
    assert last in payload["messages"][0]["content"]
    assert "कहाँ हुआ था?" in payload["messages"][0]["content"]


def test_response_contract_limits_clarification_and_preserves_clear_answers():
    assert "context_question=true and needs_clarification=true" in CONTEXT_QUESTION_TURN
    assert "Ask ONE short clarification" in CONTEXT_QUESTION_TURN
    assert "do not ask for business consent" in CONTEXT_QUESTION_TURN
    assert "or ask the pending workflow question in the same reply" in CONTEXT_QUESTION_TURN
    assert "For an ordinary clear context question, answer it" in CONTEXT_QUESTION_TURN
    assert "requested fact is clearly identified but unavailable" in CONTEXT_QUESTION_TURN
    assert "public identity" in CONTEXT_QUESTION_TURN
    assert "Answer clear public identity/purpose questions and known facts directly" in CONTEXT_QUESTION_RESPONSE
    assert "Keep the workflow paused" in CONTEXT_QUESTION_RESPONSE


@pytest.mark.parametrize("text,needs_clarification", [
    ("I meant the technician visit.", False),
    ("हाँ।", True),
])
async def test_followup_clarification_is_in_actual_model_payload_without_becoming_business_consent(
    text, needs_clarification,
):
    clarification = {
        "original_question": "Where did that happen?",
        "clarification_question": "Do you mean the technician visit or the rejected request?",
        "business_question": "May I forward your request for a technician visit?",
    }
    llm = _DecisionLLM({
        "signal": "question", "confidence": .96,
        "context_question": True, "needs_clarification": needs_clarification,
        "next_action": "answer",
    })
    engine = GoalEngine(llm=llm, policy=LOAN_POLICY)

    decision = await engine.decide(text, state={"context_clarification": clarification})

    assert decision is not None
    assert decision.context_question
    assert decision.needs_clarification is needs_clarification
    assert decision.next_action == "answer"
    assert not decision.slots and not decision.tool_request
    assert decision.decision is None
    assert len(llm.calls) == 1
    request = llm.calls[0]
    assert json.dumps(clarification, ensure_ascii=False) in request["messages"][0]["content"]
    assert "A bare acknowledgement or yes/no is not business consent" in request["system"]
    assert "not against the paused business_question" in request["system"]
    assert "Explicit configured knowledge, tool, transfer, stop or end-call requests" in request["system"]


async def test_stage_b_actual_generation_payload_and_audio_keep_only_referent_clarification():
    question = "Did a technician call or visit?"
    clarification = "Are you asking about the technician visit or the request rejection?"
    llm = _LLMStub(reply=clarification)
    workflow = _WorkflowStub({})
    brain = make_brain(llm=llm, workflow_engine=workflow)
    brain._active_workflow = "tenant_flow"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = question
    brain._last_bot_reply = "The team rejected your request. " + question
    brain._history = [{"role": "user", "content": "Where did it happen?"}]

    await brain._generate_reply(
        "Where did it happen?",
        RouteDecision(kind=RouteKind.CHAT, signal="question", reason="context_question_in_workflow"),
        time.perf_counter(),
        question_response=True,
        pending_question=question,
        clarify_context_question=True,
    )

    assert len(llm.systems) == 1
    prompt = llm.systems[0]
    assert CONTEXT_QUESTION_RESPONSE in prompt
    assert CONTEXT_QUESTION_TURN in prompt
    assert "Authoritative response mode: context_question=true and needs_clarification=true." in prompt
    assert "# Workflow question ownership" not in prompt
    assert "Do not answer a guessed question, ask for business consent, or append the paused business question" in prompt
    assert "Response task: ask only one short clarification about the caller's referent" in llm.histories[0][-1]["content"]
    spoken = [turn.text for turn in brain._recorder.turns if turn.role == "bot"]
    assert spoken == [clarification]
    assert workflow.calls == []
    assert brain._pending_workflow_question == question
    assert not any(kind == "pending_question_resumed" for kind, _ in brain._recorder.events)


async def test_stage_b_clear_context_answer_keeps_existing_business_question_ownership():
    question = "Did a technician call or visit?"
    llm = _LLMStub(reply="I am calling from the support team.")
    workflow = _WorkflowStub({})
    brain = make_brain(llm=llm, workflow_engine=workflow)
    brain._active_workflow = "tenant_flow"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = question
    brain._history = [{"role": "user", "content": "Who is calling?"}]

    await brain._generate_reply(
        "Who is calling?",
        RouteDecision(kind=RouteKind.CHAT, signal="question", reason="context_question_in_workflow"),
        time.perf_counter(),
        question_response=True,
        pending_question=question,
        lock_workflow_question=True,
    )

    assert len(llm.systems) == 1
    assert "Authoritative response mode: context_question=true and needs_clarification=true." not in llm.systems[0]
    assert "# Workflow question ownership" in llm.systems[0]
    spoken = [turn.text for turn in brain._recorder.turns if turn.role == "bot"]
    assert spoken == [llm.reply, question]
    assert workflow.calls == []
    assert brain._pending_workflow_question == question


async def test_new_clear_context_question_in_a_clarification_frame_outranks_old_referent():
    frame = {
        "original_question": "Where did it happen?",
        "clarification_question": "Do you mean the visit or the rejected request?",
        "business_question": "Did a technician call or visit?",
    }
    latest = "Who is calling?"
    decision_llm = _DecisionLLM({
        "signal": "question", "confidence": .96, "context_question": True,
        "needs_clarification": False, "next_action": "answer",
    })
    engine = GoalEngine(llm=decision_llm, policy=LOAN_POLICY)
    decision = await engine.decide(latest, state={"context_clarification": frame})

    assert decision is not None and decision.context_question
    assert not decision.needs_clarification
    request = decision_llm.calls[0]
    assert "new clear context questions take priority over the old unresolved question" in request["system"]
    assert latest in request["messages"][0]["content"]
    assert json.dumps(frame, ensure_ascii=False) in request["messages"][0]["content"]

    reply_llm = _LLMStub(reply="I am calling from the support team.")
    workflow = _WorkflowStub({})
    brain = make_brain(llm=reply_llm, workflow_engine=workflow)
    brain._active_workflow = "tenant_flow"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = frame["business_question"]
    brain._pending_context_clarification = frame.copy()
    brain._last_bot_reply = frame["clarification_question"]
    brain._history = [{"role": "user", "content": latest}]

    await brain._answer_context_clarification(
        latest,
        RouteDecision(kind=RouteKind.CHAT, signal="question", reason="context_clarification_followup"),
        time.perf_counter(), decision,
    )

    assert len(reply_llm.systems) == 1
    prompt = reply_llm.systems[0]
    assert "caller asks a new explicit clear context question instead, answer that latest question" in prompt
    assert "Do not pretend the old referent was resolved or answer a guessed original question" in prompt
    assert json.dumps(frame, ensure_ascii=False) in prompt
    assert latest in reply_llm.histories[0][-1]["content"]
    assert "# Workflow question ownership" in prompt
    assert "Authoritative response mode: context_question=true and needs_clarification=true." not in prompt
    assert [turn.text for turn in brain._recorder.turns if turn.role == "bot"] == [
        reply_llm.reply, frame["business_question"],
    ]
    assert brain._pending_context_clarification is None
    assert workflow.calls == []


async def test_low_confidence_followup_does_not_discard_pending_context_clarification():
    question = "Did a technician call or visit?"
    clarification = "Which event are you asking about?"
    llm = _LLMStub(reply=clarification)
    workflow = _WorkflowStub({})
    brain = make_brain(llm=llm, workflow_engine=workflow)
    brain._active_workflow = "tenant_flow"
    brain._workflow_ever_routed = True
    brain._pending_workflow_question = question
    brain._pending_context_clarification = {
        "original_question": "Where did it happen?",
        "clarification_question": "Do you mean the visit or the rejected request?",
        "business_question": question,
    }
    brain._history = [{"role": "user", "content": "That event."}]
    decision = _context_decision(confidence=.59, needs_clarification=False)
    assert not decision.context_question

    await brain._answer_context_clarification(
        "That event.",
        RouteDecision(kind=RouteKind.CHAT, signal="question", reason="context_clarification_followup"),
        time.perf_counter(), decision,
    )

    assert [turn.text for turn in brain._recorder.turns if turn.role == "bot"] == [clarification]
    assert brain._pending_context_clarification is not None
    assert brain._pending_context_clarification["clarification_question"] == clarification
    assert workflow.calls == []
    assert not any(kind == "pending_question_resumed" for kind, _ in brain._recorder.events)


async def test_actual_stage_a_payload_allows_independent_business_answer_but_never_bare_ack():
    frame = {
        "original_question": "Where did it happen?",
        "clarification_question": "Do you mean the visit or the rejected request?",
        "business_question": "Did a technician call or visit?",
    }
    text = "The technician called me yesterday, but no visit happened."
    llm = _DecisionLLM({
        "signal": "affirm", "confidence": .97, "context_question": False,
        "decision": None, "needs_clarification": False,
        "next_action": "continue_workflow",
    })
    engine = GoalEngine(llm=llm, policy=LOAN_POLICY)

    decision = await engine.decide(text, state={"context_clarification": frame})

    assert decision is not None
    assert not decision.context_question and not decision.needs_clarification
    assert decision.decision is None
    assert decision.next_action == "continue_workflow"
    assert decision.signal == "affirm"
    assert not decision.slots and not decision.tool_request
    assert len(llm.calls) == 1
    request = llm.calls[0]
    assert "A complete, independent substantive answer to business_question" in request["system"]
    assert "a bare acknowledgement, yes/no, or a fragment resolving only the old referent never qualifies" in request["system"]
    assert "Do not infer a missing fact or consent" in request["system"]
    assert text in request["messages"][0]["content"]
    assert json.dumps(frame, ensure_ascii=False) in request["messages"][0]["content"]

