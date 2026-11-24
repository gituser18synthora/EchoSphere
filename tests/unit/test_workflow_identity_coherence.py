"""Identity means the same thing to the voice rule and the authored gate."""
from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.goal_engine import _WARMUP_MAX_TOKENS
from shared.orchestration.identity_confirmation import STANDARD, STRICT, decision_prompt_rule
from shared.runtime_context import build_runtime_context
from tests.unit.test_agentic_orchestration import _AgenticLLMStub, events, make_agentic_brain
from tests.unit.test_brain_collection_policy import bot_replies, turn


QUESTION = "क्या मेरी बात Gaurav जी से हो रही है?"
CONTACT = "क्या technician का call आया था या visit हुई थी?"
IDENTITY = dict(session_id="identity-coherence", tenant_id="tn-x", bot_id="bot-x",
                workflow_name="wf_identity_coherence", language="hi-IN")


def definition(*, marked=True, values=None):
    config = {
        "variable": "identity_answer", "question": QUESTION,
        "entity": {"dataType": "text", "synonymPatterns": {
            "yes": [r"^(?:yes|haan|हाँ)[।.]?$"],
            "no": [r"^(?:no|nahi|नहीं)[।.]?$"],
            "service_declined": [r"service nahi chahiye"],
        }},
    }
    if marked:
        config.update(confirmationRole="identity", confirmationValues=values or {
            "confirmed": "yes", "denied": "no",
        })
    return {
        "id": "wf_identity_coherence", "name": "Identity coherence", "version": 1,
        "nodes": [
            {"id": "start", "kind": "start"},
            {"id": "identity", "kind": "ask", "config": config},
            {"id": "is_customer", "kind": "condition", "config": {
                "variable": "identity_answer", "operator": "equals", "value": "yes",
            }},
            {"id": "contact", "kind": "ask", "config": {
                "variable": "contact_answer", "question": "This is the call introduction. " + CONTACT,
                "resumePrompt": "Did a technician call or visit?",
                "resumePromptByLanguage": {"hi": CONTACT},
                "entity": {"dataType": "text", "allowedValues": ["yes", "no"]},
            }},
            {"id": "denied", "kind": "end", "config": {"text": "Wrong person."}},
        ],
        "edges": [
            {"from": "start", "to": "identity"},
            {"from": "identity", "to": "is_customer"},
            {"from": "is_customer", "to": "contact", "label": "yes"},
            {"from": "is_customer", "to": "denied", "label": "no"},
        ],
    }


@pytest.fixture
def engine(monkeypatch):
    source = definition()
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: deepcopy(source))
    instance = wfe.WorkflowEngine()
    instance._checkpointer = MemorySaver()
    return instance, source


@pytest.mark.parametrize("awaiting", (False, True))
@pytest.mark.parametrize("outcome,value", (("confirmed", "yes"), ("denied", "no")))
async def test_canonical_current_reply_only_fills_authored_identity_gate(engine, awaiting, outcome, value):
    instance, _ = engine
    if awaiting:
        initial = await instance.handle_turn_detailed(user_text="", **IDENTITY)
        assert initial["awaitingAsk"] == {
            "kind": "yes_no", "options": ["yes", "no"], "confirmationRole": "identity",
        }
    result = await instance.handle_turn_detailed(
        user_text="हाँ बोलो।" if outcome == "confirmed" else "मैं उनका भाई हूँ।",
        identity_resolution=outcome, **IDENTITY,
    )
    assert result["slots"]["identity_answer"] == value
    assert result["identityConfirmation"] == {
        "outcome": outcome, "node": "identity", "variable": "identity_answer",
    }
    if outcome == "confirmed":
        assert not result["done"]
        assert result["nodeResumePrompt"] == CONTACT
        assert result["nodePrompt"].startswith("This is the call introduction.")


@pytest.mark.parametrize("marked,values", [
    (False, None), (True, {"confirmed": "yes"}),
    (True, {"confirmed": "yes", "denied": "yes"}),
    (True, {"confirmed": True, "denied": False}),
])
async def test_unmarked_or_invalid_authoring_never_accepts_identity_override(engine, marked, values):
    instance, source = engine
    source.update(definition(marked=marked, values=values))
    result = await instance.handle_turn_detailed(
        user_text="हाँ बोलो।", identity_resolution="confirmed", **IDENTITY,
    )
    assert "identity_answer" not in result["slots"]
    assert result["identityConfirmation"] is None


async def test_identity_authority_is_single_turn_single_gate(engine):
    instance, _ = engine
    accepted = await instance.handle_turn_detailed(
        user_text="हाँ बोलो।", identity_resolution="confirmed", **IDENTITY,
    )
    assert "contact_answer" not in accepted["slots"]
    ambiguous = await instance.handle_turn_detailed(user_text="जो कहा वह?", **IDENTITY)
    assert "contact_answer" not in ambiguous["slots"]
    assert ambiguous["identityConfirmation"] is None
    graph = next(iter(instance._graphs.values()))
    snapshot = await graph.aget_state({"configurable": {
        "thread_id": "identity-coherence:wf_identity_coherence",
    }})
    assert snapshot.values["identity_resolution"] is None


async def test_protected_authored_opt_out_is_not_rewritten_as_identity_denial(engine):
    instance, _ = engine
    result = await instance.handle_turn_detailed(
        user_text="service nahi chahiye", identity_resolution="denied", **IDENTITY,
    )
    assert result["slots"]["identity_answer"] == "service_declined"
    assert result["identityConfirmation"] is None


@pytest.mark.parametrize("awaiting", (False, True))
async def test_actual_authored_capture_reports_acceptance_without_model_override(engine, awaiting):
    instance, _ = engine
    if awaiting:
        await instance.handle_turn_detailed(user_text="", **IDENTITY)
    result = await instance.handle_turn_detailed(user_text="हाँ।", **IDENTITY)
    assert result["identityConfirmation"]["outcome"] == "confirmed"
    assert result["slots"]["identity_answer"] == "yes"


async def test_empty_reply_and_reused_slots_never_claim_current_identity_confirmation(engine):
    instance, _ = engine
    result = await instance.handle_turn_detailed(
        user_text="", initial_slots={"identity_answer": "yes"},
        identity_resolution="confirmed", **IDENTITY,
    )
    assert result["identityConfirmation"] is None


async def test_model_confirmation_cannot_override_authored_current_denial(engine):
    instance, _ = engine
    result = await instance.handle_turn_detailed(
        user_text="नहीं।", identity_resolution="confirmed", **IDENTITY,
    )
    assert result["identityConfirmation"]["outcome"] == "denied"
    assert result["slots"]["identity_answer"] == "no"


async def test_resume_prompt_is_available_on_context_question_without_consuming_turn(engine):
    instance, _ = engine
    await instance.handle_turn_detailed(user_text="हाँ बोलो।", identity_resolution="confirmed", **IDENTITY)
    result = await instance.handle_turn_detailed(user_text="कहाँ हुआ था?", pause_for_context=True, **IDENTITY)
    assert result["contextQuestion"]
    assert result["nodeResumePrompt"] == CONTACT
    assert "contact_answer" not in result["slots"]


def brain_with_engine(instance, *, mode=STANDARD, decisions=()):
    llm = _AgenticLLMStub(list(decisions))
    context = build_runtime_context(
        tenant_id="tn-x", bot_id="bot-x",
        field_definitions=[{"key": "customer_name", "type": "string"}],
        payload={"customer_name": "Gaurav"}, payload_source="test",
    )
    brain = make_agentic_brain(
        runtime_context=context, llm=llm, workflows=instance,
        intents=[{"name": "caller_go_ahead", "samples": ["haan", "yes"],
                  "route": "workflow:wf_identity_coherence", "confidence_threshold": 0.6}],
        goal_policy={"identity": {"requireConfirmation": True}},
    )
    brain._config.greeting = QUESTION
    brain._identity.mode = mode
    return brain, llm


@pytest.mark.parametrize("text", (
    "हाँ बोलो।", "हाँ बोलिए।", "जी बोलिये।", "yes go ahead", "हम्म।", "hmm",
))
async def test_actual_brain_and_graph_accept_first_standard_identity_answer_once(engine, text):
    instance, _ = engine
    brain, llm = brain_with_engine(instance)
    await brain._say(QUESTION)
    await turn(brain, text)
    assert brain._identity.confirmed
    assert brain._goal_session.identity_state == "confirmed"
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"
    assert bot_replies(brain)[-1].endswith(CONTACT)
    assert len(events(brain, "workflow_identity_accepted")) == 1
    assert "contact_answer" not in brain._recorder.workflow_slots
    assert not [c for c in llm.generate_calls if c["max_tokens"] != _WARMUP_MAX_TOKENS]


async def test_actual_brain_honors_strict_go_ahead_then_confirms(engine):
    instance, _ = engine
    brain, _ = brain_with_engine(instance, mode=STRICT)
    await brain._say(QUESTION)
    await turn(brain, "हाँ बोलो।")
    assert not brain._identity.confirmed
    assert brain._active_workflow is None
    await turn(brain, "हाँ बोलिए।")
    assert brain._identity.confirmed
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"


@pytest.mark.parametrize("explicit_asked", (False, True))
async def test_strict_actual_brain_and_graph_do_not_confirm_bare_hum(engine, explicit_asked):
    instance, _ = engine
    brain, llm = brain_with_engine(instance, mode=STRICT, decisions=[{
        "scope": "in_scope", "decision": "ambiguous", "confidence": 0.99,
        "next_action": "ask_identity_confirmation", "needs_clarification": True,
    }])
    brain._identity.explicit_asked = explicit_asked
    await brain._say("क्या आप ही Gaurav जी बोल रहे हैं?" if explicit_asked else QUESTION)
    await turn(brain, "हम्म।")
    assert not brain._identity.confirmed
    assert brain._goal_session.identity_state == "unconfirmed"
    assert not getattr(brain._recorder, "workflow_slots", {})
    assert not events(brain, "workflow_identity_accepted")
    assert len([c for c in llm.generate_calls if c["max_tokens"] != _WARMUP_MAX_TOKENS]) == 1


@pytest.mark.parametrize("text", ("हाँ बोलिए।", "हम्म।"))
async def test_pending_identity_gate_keeps_rule_after_off_question_generated_response(engine, text):
    instance, _ = engine
    brain, llm = brain_with_engine(instance, decisions=[{
        "scope": "in_scope", "decision": "ambiguous", "confidence": 0.95,
        "next_action": "ask_identity_confirmation", "needs_clarification": True,
    }])
    initial = await instance.handle_turn_detailed(user_text="", **{
        **IDENTITY, "session_id": brain._recorder.session_id,
    })
    brain._active_workflow = "wf_identity_coherence"
    brain._pending_workflow_question = initial["nodePrompt"]
    brain._pending_ask_shape = initial["awaitingAsk"]
    await brain._say("मैं customer care से बोल रही हूँ।")
    state = brain._orchestration_state()
    assert state["workflow_stage"] == "workflow:wf_identity_coherence"
    assert state["expected_answer"] == "identity confirmation (yes/no)"
    assert decision_prompt_rule(STANDARD) in state["pending_question"]
    await brain._decide_turn("क्या बोलिए?")
    payload = llm.generate_calls[-1]["messages"][-1]["content"]
    assert decision_prompt_rule(STANDARD) in payload
    await turn(brain, text)
    assert brain._identity.confirmed
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"


async def test_model_confirmation_does_not_mark_tracker_if_gate_rejects(engine):
    instance, source = engine
    source["nodes"][1]["config"]["entity"]["synonymPatterns"]["service_declined"] = ["withdraw"]
    brain, _ = brain_with_engine(instance, decisions=[{
        "scope": "in_scope", "signal": "affirm", "decision": "confirmed",
        "confidence": 0.99, "next_action": "continue_workflow",
    }])
    await brain._say(QUESTION)
    await turn(brain, "withdraw")
    assert not brain._identity.confirmed
    assert brain._goal_session.identity_state == "unconfirmed"
    assert brain._recorder.workflow_slots["identity_answer"] == "service_declined"


async def test_late_final_rollback_restores_identity_before_interpreting_denial_tail(engine):
    instance, _ = engine
    brain, _ = brain_with_engine(instance, decisions=[{
        "scope": "in_scope", "signal": "wrong_person", "decision": "denied",
        "confidence": 0.99, "next_action": "continue_workflow",
    }])
    await brain._say(QUESTION)
    partial = "हाँ बोलो।"
    # A completed early endpoint whose generated reply has not been delivered.
    # Reproduce the real graph commit, then the late-final merge's rewind.
    brain._open_turn_text = partial
    brain._open_turn_semantic_snapshot = {
        "text": partial, "hearing_probe": None, "context_clarification": None,
    }
    await brain._handle_turn(partial)
    assert brain._identity.confirmed
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"
    assert brain._goal_session.identity_state == "confirmed"
    claim = brain._claim_open_turn()
    assert "hearing_probe" in brain._claimed_semantic_snapshots[id(claim)]
    assert "context_clarification" in brain._claimed_semantic_snapshots[id(claim)]
    await brain._rollback_open_turn(claim)
    assert not brain._identity.confirmed and not brain._identity.denied
    assert brain._goal_session.identity_state == "unconfirmed"
    assert brain._recorder.workflow_slots == {}
    assert brain._active_workflow is None
    assert events(brain, "workflow_identity_rolled_back")
    await brain._handle_turn("हाँ बोलो, नहीं मैं उनका भाई हूँ।")
    assert not brain._identity.confirmed
    assert brain._goal_session.identity_state != "confirmed"
    assert brain._recorder.workflow_slots.get("identity_answer") != "yes"


async def test_unmarked_legacy_identity_gate_confirms_after_actual_matcher_acceptance(engine):
    instance, source = engine
    source.update(definition(marked=False))
    brain, _ = brain_with_engine(instance)
    await brain._say(QUESTION)
    await turn(brain, "हाँ।")
    assert brain._identity.confirmed
    assert brain._goal_session.identity_state == "confirmed"
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"
    assert events(brain, "workflow_identity_accepted")[-1]["legacy"] is True


async def test_unmarked_legacy_identity_gate_failed_capture_does_not_confirm(engine):
    instance, source = engine
    source.update(definition(marked=False))
    brain, _ = brain_with_engine(instance)
    await brain._say(QUESTION)
    await turn(brain, "हाँ बोलो।")
    assert not brain._identity.confirmed
    assert brain._goal_session.identity_state == "unconfirmed"
    assert "identity_answer" not in brain._recorder.workflow_slots
    assert not events(brain, "workflow_identity_accepted")


async def test_initial_greeting_yes_does_not_confirm_unrelated_legacy_graph_question(engine):
    instance, source = engine
    source.update(definition(marked=False))
    source["nodes"][1]["config"]["question"] = "क्या आपको technician की visit चाहिए?"
    brain, _ = brain_with_engine(instance)
    await brain._say(QUESTION)
    await turn(brain, "हाँ।")
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"
    assert not brain._identity.confirmed
    assert brain._goal_session.identity_state == "unconfirmed"
    assert not events(brain, "workflow_identity_accepted")


async def test_invalid_explicit_identity_contract_cannot_fall_back_to_legacy_confirmation(engine):
    instance, source = engine
    source.update(definition(values={"confirmed": "yes"}))
    brain, _ = brain_with_engine(instance)
    await brain._say(QUESTION)
    await turn(brain, "हाँ।")
    assert brain._recorder.workflow_slots["identity_answer"] == "yes"
    assert not brain._identity.confirmed
    assert brain._goal_session.identity_state == "unconfirmed"
    assert not events(brain, "workflow_identity_accepted")

