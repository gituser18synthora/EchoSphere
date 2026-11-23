"""Remaining 23 Nov audit failures: hearing probes and terminal corrections."""

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from shared.orchestration.decision_schema import ConversationDecision
from shared.orchestration.goal_engine import BotGoalPolicy, GoalEngine
from shared.orchestration.router import RouteDecision, RouteKind
from shared.orchestration.intent_classifier import IntentClassification
from shared.orchestration.workflow_corrections import CORRECTION_STATE_KEY
from shared.post_call.analyzer import fallback_analysis
from shared.post_call.nba import decide_next_best_action
from shared.post_call.schema import PostCallAnalysis
from shared.post_call.structured import derive_structured_fields, merge_structured_fields
from shared.runtime_context import RuntimeContext, ContextValue
from tests.unit.test_brain_workflow_routing import (
    _LLMStub, _WorkflowStub, consumed_result, make_brain,
)
from tests.unit.test_workflow_closing import cleanup, closing_brain
from tests.unit.test_goal_engine import _DecisionLLM


POLICY = BotGoalPolicy.model_validate({"summaryFields": [
    {"name": "service_required", "type": "yes_no", "source": "outcome",
     "values": {"cancel_no_service": "No", "redispatch": "Yes"}},
    {"name": "issue_resolved", "type": "choice", "source": "outcome",
     "options": ["resolved", "not_resolved"],
     "values": {"redispatch_completion": "resolved"}},
    {"name": "disposition", "type": "choice", "source": "outcome",
     "options": ["Cancel WO", "Re-dispatch", "Issue Resolved"],
     "values": {"cancel_no_service": "Cancel WO", "redispatch": "Re-dispatch",
                "redispatch_completion": "Issue Resolved"}},
    {"name": "contacted", "type": "yes_no", "source": "technician_contact"},
    {"name": "payment_verified", "type": "yes_no", "source": "payment_verified"},
]})


def correction(text, *, slots=(), **kwargs):
    return ConversationDecision(
        confidence=1, next_action="answer",
        correction={"evidence": text, "slots": list(slots)}, **kwargs,
    )


@pytest.mark.parametrize("text,outcome", [
    ("TV abhi bhi theek nahi hua, 590 pay kar diya", "redispatch_completion"),
    ("abhi cancellation nahi karna hai", "cancel_no_service"),
    ("zaroorat hai, kyun nahi zaroorat", "cancel_no_service"),
    ("ab to mujhe karani hi nahi hai", "redispatch"),
])
async def test_captured_explicit_corrections_invalidate_proposal_without_repeating_actions(text, outcome):
    brain, workflow, llm = await closing_brain()
    try:
        brain._goal_policy = POLICY
        brain._terminal_outcome_slots = {"outcome"}
        brain._recorder.workflow_slots = {
            "outcome": outcome, "technician_contact": "yes", "payment_verified": "yes",
        }
        brain._take_decision.return_value = correction(text)
        brain._cancel_workflow_close("test_correction")
        await brain._handle_turn(text)
        assert "outcome" not in brain._recorder.workflow_slots
        assert brain._recorder.workflow_slots["technician_contact"] == "yes"
        assert brain._recorder.workflow_slots["payment_verified"] == "yes"
        state = brain._recorder.call_state[CORRECTION_STATE_KEY]
        assert state["status"] == "contested"
        assert state["previous_slots"] == {"outcome": outcome}
        assert "workflow_outcome_contested" in brain._recorder.event_kinds()
        assert len(workflow.calls) == 1
        workflow.rollback_last_turn.assert_not_called()
        assert "The caller explicitly contested" in llm.systems[-1]
        assert f"- outcome: {outcome}" not in llm.systems[-1]
        assert "actual tool result proves that restriction" in llm.systems[-1]
    finally:
        await cleanup(brain)


async def test_late_correction_cannot_repeat_configured_tool_side_effect():
    brain, workflow, _ = await closing_brain()
    try:
        brain._goal_policy = POLICY
        brain._terminal_outcome_slots = {"outcome"}
        brain._recorder.workflow_slots = {"outcome": "cancel_no_service"}
        brain._take_decision.return_value = correction("cancel mat karo")
        brain._intent_pipeline.from_decision = lambda _: IntentClassification(
            signal="deny", tool_name="cancel_order", confidence=1,
        )
        brain._run_intent_tool = AsyncMock(side_effect=AssertionError("Must not repeat action"))
        await brain._handle_turn("cancel mat karo")
        brain._run_intent_tool.assert_not_called()
        assert len(workflow.calls) == 1
        assert brain._recorder.call_state[CORRECTION_STATE_KEY]["status"] == "contested"
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("decision,text", [
    (ConversationDecision(signal="question", confidence=1), "When will the technician come?"),
    (ConversationDecision(slots={"payment": "590"}, confidence=1), "I paid 590"),
    (correction("different words"), "cancel mat karo"),
    (ConversationDecision(confidence=0.8, correction={"evidence": "cancel mat karo"}), "cancel mat karo"),
    (ConversationDecision(confidence=1, scope="injection_attempt",
                          correction={"evidence": "cancel mat karo"}), "cancel mat karo"),
])
def test_unvalidated_or_noncorrection_turns_cannot_invalidate_outcome(decision, text):
    brain = make_brain()
    brain._goal_policy = POLICY
    brain._terminal_outcome_slots = {"outcome"}
    brain._recorder.workflow_slots = {"outcome": "cancel_no_service"}
    brain._record_late_workflow_correction(text, decision)
    assert brain._recorder.workflow_slots == {"outcome": "cancel_no_service"}
    assert CORRECTION_STATE_KEY not in getattr(brain._recorder, "call_state", {})


def test_correction_only_invalidates_declared_facts_and_cannot_erase_verified_backend_state():
    brain = make_brain()
    brain._goal_policy = POLICY
    brain._terminal_outcome_slots = {"outcome"}
    brain._recorder.workflow_slots = {
        "outcome": "redispatch_completion", "technician_contact": "yes",
        "payment_verified": "yes", "unknown": "value",
    }
    brain._record_late_workflow_correction(
        "He never called", correction("never called", slots=(
            "technician_contact", "payment_verified", "unknown",
        )),
    )
    assert brain._recorder.workflow_slots == {"payment_verified": "yes", "unknown": "value"}


async def test_explicit_correction_can_need_clarification_and_next_short_answer_is_retained():
    brain, workflow, llm = await closing_brain(reply="Do you still want the service?")
    try:
        brain._goal_policy = POLICY
        brain._terminal_outcome_slots = {"outcome"}
        brain._recorder.workflow_slots = {"outcome": "cancel_no_service"}
        text = "जरूरत है, जरूरत है, क्यों नहीं जरूरत?"
        brain._take_decision.return_value = ConversationDecision(
            confidence=.99, needs_clarification=True, next_action="clarify",
            correction={"evidence": "जरूरत है, जरूरत है", "slots": ["outcome"]},
        )
        brain._cancel_workflow_close("test_correction")
        await brain._handle_turn(text)
        assert "outcome" not in brain._recorder.workflow_slots
        assert brain._recorder.call_state[CORRECTION_STATE_KEY]["status"] == "contested"
        assert brain._pending_workflow_close is None
        assert not brain._workflow_farewell_active
        brain._take_decision.return_value = ConversationDecision(
            confidence=1, signal="affirm", next_action="answer",
        )
        llm.reply = "I have noted your correction for review. Thank you."
        await brain._handle_turn("haan ji")
        assert len(llm.systems) == 2
        assert len(workflow.calls) == 1
        assert "workflow_closing_acknowledgement" not in brain._recorder.event_kinds()
        assert brain._recorder.call_state[CORRECTION_STATE_KEY]["status"] == "contested"
    finally:
        await cleanup(brain)


async def test_correction_from_unheard_partial_turn_rolls_back_for_full_turn_reinterpretation():
    brain = make_brain()
    brain._goal_policy = POLICY
    brain._terminal_outcome_slots = {"outcome"}
    brain._recorder.workflow_slots = {"outcome": "cancel_no_service", "technician_contact": "yes"}
    brain._recorder.call_state = {"independent": "retained"}
    brain._open_turn_text = "cancel mat karo"
    brain._record_late_workflow_correction("cancel mat karo", correction("cancel mat karo"))
    assert "outcome" not in brain._recorder.workflow_slots
    await brain._rollback_open_turn(brain._claim_open_turn())
    assert brain._recorder.workflow_slots == {"outcome": "cancel_no_service", "technician_contact": "yes"}
    assert brain._recorder.call_state == {"independent": "retained"}
    assert "workflow_correction_rolled_back" in brain._recorder.event_kinds()


async def test_contested_workflow_fact_is_removed_from_verified_prompt_and_rollback_restores_it():
    context = RuntimeContext(tenant_id="tn-x", bot_id="bot-x", session_verification_required=True)
    context.set_workflow_value("customer_verified", True)
    context.set_workflow_value("outcome", "cancel_no_service")
    context.set_workflow_value("payment_verified", True)
    context.values["backend_id"] = ContextValue(key="backend_id", value="immutable", source="system")
    brain = make_brain(runtime_context=context)
    brain._goal_policy = POLICY
    brain._terminal_outcome_slots = {"outcome"}
    brain._recorder.workflow_slots = {"outcome": "cancel_no_service", "payment_verified": "yes"}
    brain._verified_runtime_context_block = context.prompt_section()
    assert "cancel_no_service" in brain._verified_runtime_context_block
    brain._open_turn_text = "cancel mat karo"
    brain._record_late_workflow_correction("cancel mat karo", correction("cancel mat karo"))
    assert context.is_session_verified()
    assert "outcome" not in context.prompt_values()
    assert "cancel_no_service" not in brain._verified_runtime_context_block
    assert context.get("payment_verified") is True
    assert context.get("backend_id") == "immutable"
    await brain._rollback_open_turn(brain._claim_open_turn())
    assert context.get("outcome") == "cancel_no_service"
    assert "cancel_no_service" in brain._verified_runtime_context_block
    assert context.get("payment_verified") is True
    assert context.get("backend_id") == "immutable"


def test_context_invalidation_cannot_erase_same_named_external_fact():
    context = RuntimeContext(tenant_id="tn-x", bot_id="bot-x")
    context.values["outcome"] = ContextValue(key="outcome", value="backend_fact", source="api")
    assert context.invalidate_workflow_values({"outcome"}) == {}
    assert context.get("outcome") == "backend_fact"


def test_unmarked_legacy_terminal_outcome_is_not_implicitly_model_editable():
    brain = make_brain()
    brain._recorder.workflow_slots = {"outcome": "cancel_no_service"}
    brain._record_late_workflow_correction("cancel mat karo", correction("cancel mat karo"))
    assert brain._recorder.workflow_slots["outcome"] == "cancel_no_service"


def contested_state():
    return {CORRECTION_STATE_KEY: {
        "status": "contested", "source": "validated_caller_correction",
        "invalidated_slots": ["outcome"], "previous_slots": {"outcome": "redispatch_completion"},
    }}


def test_contested_structured_action_fields_stay_unknown_even_if_analyst_reasserts_them():
    state = contested_state()
    slots = {"outcome": "redispatch_completion", "technician_contact": "yes", "payment_verified": "yes"}
    fields, sources = merge_structured_fields(
        POLICY, slots, {"issue_resolved": "resolved", "disposition": "Issue Resolved",
                        "service_required": "Yes", "contacted": "No"},
        correction=state[CORRECTION_STATE_KEY],
    )
    assert fields == {"service_required": None, "issue_resolved": None,
                      "disposition": None, "contacted": "Yes", "payment_verified": "Yes"}
    assert sources == {"contacted": "workflow", "payment_verified": "workflow"}
    assert derive_structured_fields(POLICY, slots, correction=state[CORRECTION_STATE_KEY]) == fields


def test_untrusted_metadata_does_not_override_workflow_authority():
    fields, _ = merge_structured_fields(
        POLICY, {"outcome": "cancel_no_service"}, {},
        correction={"status": "contested", "source": "llm", "invalidated_slots": ["outcome"]},
    )
    assert fields["disposition"] == "Cancel WO"


def test_fallback_and_next_action_require_review_but_preserve_contact_revocation():
    state = contested_state()
    final_state = {"call_state": state, "workflow_slots": {"outcome": "redispatch_completion"}}
    analysis = fallback_analysis(policy=POLICY, final_state=final_state,
                                 reference=datetime.now(timezone.utc))
    assert analysis.call_outcome == "incomplete"
    assert analysis.follow_up_required
    assert analysis.structured_fields["issue_resolved"] is None
    assert analysis.next_best_action.action == "collect_missing_information"
    proposed_completed = PostCallAnalysis(call_outcome="goal_completed")
    action = decide_next_best_action(proposed_completed, call_state=state, disposition="completed")
    assert action.action == "collect_missing_information"
    assert decide_next_best_action(proposed_completed, call_state=state,
                                  disposition="do_not_call").action == "do_not_contact"


async def probe_brain():
    workflow = _WorkflowStub(consumed_result("Business advanced"))
    brain = make_brain(workflow_engine=workflow, llm=_LLMStub())
    brain._active_workflow = "service_flow"
    brain._pending_workflow_question = "May I forward the request for a technician visit?"
    brain._pending_ask_shape = {"kind": "yes_no"}
    brain._reply_nodes = ["redispatch_question"]
    brain._recorder.call_state = {}
    await brain._on_silence_timeout()
    assert brain._hearing_probe_pending is not None
    return brain, workflow


@pytest.mark.parametrize("reply", [
    "haan ji", "जी", "नहीं", "hello", "Yes I can hear you", "हाँ सुनाई दे रहा है",
    "Hello I can hear you now", "जी हाँ आपकी आवाज आ रही है",
])
@pytest.mark.parametrize("guard", [False, True])
async def test_presence_answer_never_approves_pending_business_action(reply, guard):
    brain, workflow = await probe_brain()
    try:
        brain._unheard_reply_guard = guard
        brain._take_decision = AsyncMock(side_effect=AssertionError("Presence needs no business decision"))
        brain._unheard_reply = {"nodes": ("redispatch_question",), "text": "old", "played": 0}
        await brain._handle_turn(reply)
        assert workflow.calls == []
        assert brain._active_workflow == "service_flow"
        assert brain._pending_workflow_question in brain._last_bot_reply
        assert brain._hearing_probe_pending is None
        assert brain._unheard_reply is None
        assert "presence_check_answered" in brain._recorder.event_kinds()
        assert brain._recorder.turns[-2].text == reply
        assert brain._recorder.turns[-2].route == "presence_check_answer"
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("reply", ["haan ji", "जी", "नहीं", "hello"])
async def test_stale_held_probe_answer_remains_presence_response(reply):
    brain, workflow = await probe_brain()
    try:
        brain._pending_segments = [reply]
        brain._pending_newest_final_at = time.monotonic() - 4
        assert not brain._drop_stale_held_short_text()
        assert not brain._drop_filler_only_held_text()
        await brain._consume_pending_turn()
        await asyncio.sleep(0.03)
        assert workflow.calls == []
        assert "presence_check_answered" in brain._recorder.event_kinds()
    finally:
        await cleanup(brain)


async def test_unheard_partial_presence_answer_keeps_probe_context_when_final_is_merged():
    brain, workflow = await probe_brain()
    try:
        brain._open_turn_text = "haan"
        await brain._handle_turn("haan")
        await brain._rollback_open_turn(brain._claim_open_turn())
        assert brain._hearing_probe_pending is not None
        brain._take_decision = AsyncMock(side_effect=AssertionError("Still only connection acknowledgement"))
        await brain._handle_turn("haan I can hear you now")
        assert workflow.calls == []
        assert brain._pending_workflow_question in brain._last_bot_reply
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("reply", ["yes, please send the technician", "service nahi chahiye", "No, he never visited"])
async def test_substantive_reply_after_probe_can_still_answer_business_question(reply):
    brain, workflow = await probe_brain()
    try:
        brain._take_decision = AsyncMock(return_value=ConversationDecision(
            signal="deny" if reply.startswith("No") or "nahi" in reply else "affirm",
            confidence=1, next_action="continue_workflow",
        ))
        await brain._handle_turn(reply)
        assert brain._hearing_probe_pending is None
        assert len(workflow.calls) == 1
        assert workflow.calls[0]["user_text"] == reply
        assert workflow.calls[0]["signal_authoritative"] is True
    finally:
        await cleanup(brain)


async def test_authoritative_null_is_forwarded_without_resurrecting_route_signal():
    workflow = _WorkflowStub(consumed_result("Wait"))
    brain = make_brain(workflow_engine=workflow)
    try:
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="service_flow", signal="affirm"),
            "I paid 590; the TV is still broken", 0,
            signal=None, signal_authoritative=True,
        )
        assert workflow.calls[0]["signal"] is None
        assert workflow.calls[0]["signal_authoritative"] is True
    finally:
        await cleanup(brain)


async def test_actual_stage_a_payload_contains_recorded_outcome_and_correction_provenance():
    llm = _DecisionLLM({"signal": None, "confidence": .99, "next_action": "answer",
                        "correction": {"evidence": "TV is still broken", "slots": ["outcome"]}})
    engine = GoalEngine(llm=llm, policy=POLICY, intents=[], enabled=True)
    brain = make_brain()
    brain._goal_policy = POLICY
    brain._completed_workflow = "service_flow"
    brain._terminal_outcome_slots = {"outcome"}
    brain._recorder.workflow_slots = {"outcome": "redispatch_completion"}
    decision = await engine.decide("TV is still broken", state=brain._orchestration_state(), history=[])
    assert decision.correction is not None
    actual_payload = str(llm.calls[0]["messages"])
    assert "redispatch_completion" in actual_payload
    assert "Conversational outcome source keys: outcome" in actual_payload
    assert "Caller correction handling" in actual_payload
    assert "Call is still connected: True" in actual_payload


@pytest.mark.parametrize("action", ["clarify", "continue_workflow", "answer"])
async def test_uncertain_business_answer_reasks_actual_pending_question_without_new_proposal(action):
    workflow = _WorkflowStub(consumed_result("FALSE completed", done=True))
    llm = _LLMStub(reply="May I forward your request for a technician visit?")
    brain = make_brain(workflow_engine=workflow, llm=llm)
    brain._active_workflow = "service_flow"
    brain._pending_workflow_question = "Was the work completed by our technician?"
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        confidence=.99, signal="affirm", needs_clarification=True, next_action=action,
    ))
    try:
        await brain._handle_turn("I paid 590; the TV is still broken")
        assert workflow.calls == []
        assert llm.systems == []
        assert brain._last_bot_reply == "Was the work completed by our technician?"
        assert brain._active_workflow == "service_flow"
        assert "visit" not in brain._last_bot_reply
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("reply", [
    "I understand the TV is still broken. May I forward your request for a technician visit?",
    "क्या मैं आपकी request technician visit के लिए आगे भेजूँ?",
    "Please confirm whether I should send the technician.",
    "I understand, please confirm whether you need service.",
    "I understand how frustrating that is, please share your address.",
    "Please provide your phone number.",
])
async def test_offscript_generation_cannot_change_business_question_without_graph_transition(reply):
    from tests.unit.test_brain_workflow_routing import off_script_result
    pending = "Was the work completed by our technician?"
    result = off_script_result(pending)
    result["awaitingKind"] = "intent"  # Business hubs are also intent nodes.
    workflow = _WorkflowStub(result)
    llm = _LLMStub(reply=reply)
    brain = make_brain(workflow_engine=workflow, llm=llm)
    brain._active_workflow = "service_flow"
    try:
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="service_flow"),
            "The TV is still broken", 0, signal_authoritative=True,
        )
        assert len(workflow.calls) == 1
        assert len(llm.systems) == 1
        assert brain._last_bot_reply == pending
        spoken = " ".join(turn.text for turn in brain._recorder.turns if turn.role == "bot")
        assert "May I forward" not in spoken
        assert "technician visit" not in spoken
        assert "confirm whether" not in spoken
        assert "share your address" not in spoken
        assert "provide your phone number" not in spoken
        assert "workflow_generated_question_suppressed" in brain._recorder.event_kinds()
        assert "not a required question" not in llm.systems[0]
    finally:
        await cleanup(brain)


@pytest.mark.parametrize("reply", [
    "I understand how frustrating that is.",
    "मुझे पता है कि यह कितना परेशान करने वाला है।",
])
async def test_question_lock_preserves_declarative_empathy_before_real_pending(reply):
    from tests.unit.test_brain_workflow_routing import off_script_result
    pending = "Was the work completed by our technician?"
    brain = make_brain(workflow_engine=_WorkflowStub(off_script_result(pending)), llm=_LLMStub(reply=reply))
    brain._active_workflow = "service_flow"
    try:
        await brain._handle_workflow(RouteDecision(kind=RouteKind.WORKFLOW, action="service_flow"),
                                     "You are not listening", 0)
        spoken = " ".join(turn.text for turn in brain._recorder.turns if turn.role == "bot")
        assert reply in spoken
        assert brain._last_bot_reply == pending
    finally:
        await cleanup(brain)
