"""Context questions pause tenant workflows without answering their fields."""

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.decision_schema import ConversationDecision
from shared.orchestration.goal_engine import GoalSession
from shared.orchestration.intent_classifier import HybridIntentPipeline, IntentClassification
from shared.runtime_context import RuntimeContext, ContextValue
from tests.unit.test_goal_engine import LOAN_POLICY
from tests.unit.test_brain_workflow_routing import make_brain, _LLMStub, _WorkflowStub, off_script_result


def context_classification():
    return IntentClassification(signal="question", confidence=.9, context_question=True, source="llm")


def test_context_question_does_not_confirm_identity_or_consume_a_retry():
    decision = ConversationDecision(
        signal="question", confidence=.9, context_question=True,
        decision="ambiguous", next_action="ask_identity_confirmation",
    )
    session = GoalSession(LOAN_POLICY)
    before = session.identity_state
    session.apply(decision)
    assert session.identity_state == before
    assert session.identity_attempts == 0
    assert decision.next_action == "answer"
    assert decision.decision == "unrelated"


@pytest.mark.parametrize("extra", [
    {"context_question": "false"}, {"confidence": .2}, {"signal": "affirm"},
    {"scope": "injection_attempt"}, {"scope": "out_of_scope"},
    {"decision": "confirmed"}, {"decision": "denied"},
    {"slots": {"amount": {"status": "provided", "value": "500"}}},
    {"tool_request": "lookup"}, {"next_action": "answer_from_knowledge"},
])
def test_context_hint_cannot_replace_scope_gates_values_or_actions(extra):
    d = ConversationDecision.model_validate({
        "signal": "question", "confidence": .9, "context_question": True, **extra,
    })
    assert not d.context_question


@pytest.mark.parametrize("route", ["knowledge", "tool:lookup", "handoff", "hangup"])
def test_context_hint_cannot_bypass_tenant_configured_routes(route):
    pipeline = HybridIntentPipeline(intents=[{"name": "configured", "route": route}])
    result = pipeline._from_llm({
        "intent": "configured", "signal": "question", "confidence": .9,
        "context_question": True,
    }, "caller question")
    assert not result.context_question


def test_explicit_knowledge_match_wins_over_conflicting_context_hint():
    from shared.orchestration.router import RouteDecision, RouteKind
    brain = make_brain()
    decision = RouteDecision(kind=RouteKind.KNOWLEDGE, intent="office_location",
                             reason="intent_knowledge")
    assert brain._apply_classification(decision, context_classification()) is decision


async def test_separate_bots_answer_from_their_own_context_and_preserve_verification():
    from dataclasses import replace
    from voice_runtime.brain import ConversationBrain
    from tests.unit.test_brain_workflow_routing import _RecorderStub

    base = make_brain()._config
    systems = []
    for tenant, bot, organisation in (("t-clinic", "b-clinic", "Savera Clinic"),
                                       ("t-hotel", "b-hotel", "Harbor Hotel")):
        ctx = RuntimeContext(
            tenant_id=tenant, bot_id=bot, session_verification_required=True,
            values={"private_reference": ContextValue(
                key="private_reference", value=f"PRIVATE-{bot}", source="test",
            )},
        )
        llm = _LLMStub(reply=f"This is {organisation}.")
        brain = ConversationBrain(
            config=replace(base, tenant_id=tenant, bot_id=bot,
                           system_prompt=f"You represent {organisation}."),
            llm=llm, recorder=_RecorderStub(), runtime_context=ctx,
        )
        brain.push_frame = AsyncMock()
        brain._notify_client = AsyncMock()
        brain._take_decision = AsyncMock(return_value=None)
        brain._classify_turn = AsyncMock(return_value=context_classification())
        await brain._handle_turn("Which organisation are you calling from?")
        assert organisation in llm.systems[0]
        assert "PRIVATE-" not in llm.systems[0]
        assert "Identity is NOT verified" in llm.systems[0]
        assert not ctx.is_session_verified()
        systems.append(llm.systems[0])
    assert "Harbor Hotel" not in systems[0]
    assert "Savera Clinic" not in systems[1]


@pytest.mark.parametrize("language,text", [
    ("hi-IN", "आपने किसके रिगार्डिंग कॉल किया है मुझे?"),
    ("en-IN", "Which organisation are you calling from?"),
    ("ta-IN", "நீங்கள் எங்கிருந்து அழைக்கிறீர்கள்?"),
    ("ml-IN", "നിങ്ങൾ എന്തിനാണ് വിളിച്ചത്?"),
])
async def test_context_question_keeps_workflow_and_skips_unrelated_knowledge(language, text):
    engine = _WorkflowStub(off_script_result("Please describe the issue.", signal="question"))
    llm = _LLMStub(reply="A response from this bot's own context.")
    brain = make_brain(workflow_engine=engine, llm=llm)
    brain._conversation_language = language
    brain._active_workflow = "tenant_specific_flow"
    brain._config.kb_ids = ["tenant-kb"]
    brain._knowledge = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("context is already available")))
    brain._take_decision = AsyncMock(return_value=None)
    brain._classify_turn = AsyncMock(return_value=context_classification())
    await brain._handle_turn(text)
    assert engine.calls[0]["pause_for_context"] is True
    assert engine.calls[0]["language"] == language
    assert brain._active_workflow == "tenant_specific_flow"
    assert "Questions about the current call" in llm.systems[0]
    assert "Please describe the issue." in llm.systems[0]
    brain._knowledge.search.assert_not_called()


@pytest.mark.parametrize("semantic", [True, False])
async def test_opening_question_is_answered_without_starting_a_flow_or_repeating_greeting(semantic):
    from shared.orchestration.router import TurnRouter
    llm = _LLMStub(reply="मैं इस organisation की support team से बोल रहा हूँ।")
    engine = _WorkflowStub({})
    brain = make_brain(workflow_engine=engine, llm=llm)
    brain._router = TurnRouter(intents=[{
        "name": "open", "samples": ["yes"], "route": "workflow:tenant_flow",
        "fallback_behavior": "clarify",
    }], has_knowledge_bases=False)
    brain._config.greeting = "Hello, may I speak with the intended customer?"
    brain._take_decision = AsyncMock(return_value=None)
    brain._classify_turn = AsyncMock(return_value=(
        context_classification() if semantic else IntentClassification(signal="question", source="regex")
    ))
    await brain._handle_turn("आपने कॉल क्यों किया है?")
    assert len(llm.systems) == 1
    assert "# Pending opening step" in llm.systems[0]
    assert brain._config.greeting in llm.systems[0]
    assert not engine.calls and not brain._workflow_ever_routed
    assert brain._active_workflow is None
    assert not any(k == "route_decision" and v["reason"] == "entry_reprompt" for k,v in brain._recorder.events)


def test_opening_question_uses_full_context_even_when_model_omits_context_hint():
    from shared.orchestration.router import TurnRouter
    brain = make_brain()
    brain._router = TurnRouter(intents=[{
        "name": "begin", "samples": ["yes"], "route": "workflow:tenant_flow",
    }], has_knowledge_bases=False)
    decision = ConversationDecision(signal="question", confidence=.9,
                                    response_text="A reply from the short decision prompt.")
    assert brain._direct_reply_text(decision, None, "") == ""


def test_context_question_does_not_turn_pending_confirmation_into_optional_followup():
    brain = make_brain()
    result = off_script_result("May we continue?", signal="question")
    result.update(awaitingKind="intent", contextQuestion=True)
    instruction = brain._workflow_context_instruction(result)
    assert "May we continue?" in instruction
    assert "not a required question" not in instruction


@pytest.mark.parametrize("text,hint", [
    ("हाँ, कहाँ से कॉल किया है तुमने?", True),
    ("जी आप कौन बोल रहे हैं कहाँ से कॉल किए हो", False),
    ("Yes, who is calling and where from?", True),
    ("சரி, நீங்கள் யார் பேசுகிறீர்கள்?", True),
    ("ശരി, ആരാണ് വിളിക്കുന്നത്?", True),
])
async def test_opening_question_is_grounded_at_response_stage_and_note_is_not_persisted(text, hint):
    from shared.orchestration.router import TurnRouter

    llm = _LLMStub(reply="I am Mira from Savera Clinic.")
    brain = make_brain(llm=llm, workflow_engine=_WorkflowStub({}))
    brain._router = TurnRouter(intents=[{
        "name": "begin", "samples": ["yes"], "route": "workflow:clinic_flow",
        "fallback_behavior": "clarify",
    }], has_knowledge_bases=False)
    brain._config.greeting = "I am Mira from Savera Clinic. Am I speaking with the intended person?"
    brain._history = [
        {"role": "assistant", "content": brain._config.greeting},
        {"role": "user", "content": "Who are you?"},
        {"role": "assistant", "content": "I can only help with the appointment. What time can you come?"},
    ]
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        signal="question", confidence=.9, context_question=hint,
    ))
    await brain._handle_turn(text)
    assert "# Current question — response task" in llm.systems[0]
    assert "Savera Clinic" in llm.systems[0]
    assert "return to the supplied pending question" in llm.histories[0][-1]["content"]
    assert "If an earlier reply failed" in llm.histories[0][-1]["content"]
    assert llm.histories[0][-1]["content"].startswith(text)
    assert not brain._workflow_ever_routed
    assert [m["content"] for m in brain._history if m["role"] == "user"][-1] == text
    assert all("Platform note" not in t.text for t in brain._recorder.turns)


async def test_short_opening_ack_cannot_invent_business_question():
    from shared.orchestration.router import TurnRouter
    llm = _LLMStub(reply="An invented later business question?")
    engine = _WorkflowStub({})
    brain = make_brain(llm=llm, workflow_engine=engine)
    brain._router = TurnRouter(intents=[{
        "name": "begin", "samples": ["yes"], "route": "workflow:clinic_flow",
    }], has_knowledge_bases=False)
    brain._config.greeting = "नमस्ते। क्या मैं सही व्यक्ति से बात कर रहा हूँ?"
    brain._take_decision = AsyncMock(return_value=None)
    brain._classify_turn = AsyncMock(return_value=IntentClassification())
    await brain._handle_turn("अच्छा।")
    assert brain._recorder.turns[-1].text == "क्या मैं सही व्यक्ति से बात कर रहा हूँ?"
    assert not llm.systems and not engine.calls
    assert not brain._workflow_ever_routed


@pytest.mark.parametrize("already_asked", [False, True])
async def test_context_answer_resumes_pending_question_once_without_advancing(already_asked):
    question = "क्या आप customer की location पर पहुंचे थे, और क्या आपने customer को call किया था?"
    reply = "मैं support से बोल रहा हूँ।" + (" " + question if already_asked else "")
    engine = _WorkflowStub(off_script_result(question, signal="question"))
    brain = make_brain(llm=_LLMStub(reply=reply), workflow_engine=engine)
    brain._active_workflow = "tenant_flow"
    brain._workflow_ever_routed = True
    brain._take_decision = AsyncMock(return_value=None)
    brain._classify_turn = AsyncMock(return_value=context_classification())
    await brain._handle_turn("कहाँ से बोल रहे हो?")
    spoken = " ".join(t.text for t in brain._recorder.turns if t.role == "bot")
    assert spoken.count(question) == 1
    assert engine.calls[0]["pause_for_context"] is True
    assert len(engine.calls) == 1 and brain._active_workflow == "tenant_flow"
    assert brain._orchestration_state()["pending_question"] == question


async def test_repeat_request_uses_current_pending_question_and_last_utterance():
    question = "क्या आप customer की location पर पहुंचे थे, और क्या आपने customer को call किया था?"
    llm = _LLMStub(reply="जी, मैं पूछ रहा था—" + question)
    engine = _WorkflowStub(off_script_result(question, signal="question"))
    brain = make_brain(llm=llm, workflow_engine=engine)
    brain._active_workflow = "tenant_flow"
    brain._last_bot_reply = question
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        signal="question", confidence=0, needs_clarification=True,
    ))
    await brain._handle_turn("हाँ बोलो क्या बोल रहे हो?")
    request = llm.histories[0][-1]["content"]
    assert question in request and "Do not introduce yourself unless" in request
    assert "End after the answer" not in request
    assert [t.text for t in brain._recorder.turns if t.role == "bot"] == [llm.reply]


def test_question_note_is_scoped_to_one_request_and_combines_with_language_switch():
    brain = make_brain()
    brain._conversation_language = "en-IN"
    brain._history = [{"role": "user", "content": "Who are you?"}]
    first = brain._generation_messages("Answer this context question.")
    assert "Answer this context question." in first[-1]["content"]
    assert "entire reply must be in English" in first[-1]["content"]
    assert brain._history == [{"role": "user", "content": "Who are you?"}]
    assert "Answer this context question." not in brain._generation_messages()[-1]["content"]


async def test_offscript_question_gets_response_grounding_when_context_hint_is_missing():
    llm = _LLMStub(reply="I am Mira from Savera Clinic.")
    engine = _WorkflowStub(off_script_result("What time works?", signal="question"))
    brain = make_brain(llm=llm, workflow_engine=engine)
    brain._active_workflow = "clinic_flow"
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        signal="question", confidence=.9, context_question=False,
    ))
    await brain._handle_turn("Who is calling?")
    assert "# Current question — response task" in llm.systems[0]
    assert "public identity" in llm.histories[0][-1]["content"]
    assert brain._active_workflow == "clinic_flow"


@pytest.mark.parametrize("kind", ["ask", "intent"])
async def test_pause_preserves_checkpoint_and_resumes_same_tenant_step(monkeypatch, kind):
    definition = {
        "id": "wf_context_test", "version": 1, "name": "Context test",
        "nodes": [
            {"id": "start", "kind": "start"},
            {"id": "pending", "kind": kind, "config": {
                "question": "What happened?", "prompt": "May we continue?",
                "variable": "description", "entityType": "text",
            }},
            {"id": "finish", "kind": "end", "config": {"text": "Done."}},
        ],
        "edges": [{"from": "start", "to": "pending"},
                  {"from": "pending", "to": "finish", "label": "yes"}],
    }
    monkeypatch.setattr(wfe, "load_workflow_definition", lambda *args: definition)
    engine = wfe.WorkflowEngine()
    engine._checkpointer = MemorySaver()
    args = dict(tenant_id="tenant-A", bot_id="bot-A", session_id="call-A", workflow_name="flow")
    first = await engine.handle_turn_detailed(**args, user_text="start")
    graph = await engine._get_definition_graph(definition)
    thread = {"configurable": {"thread_id": "call-A:flow"}}
    before = copy.deepcopy((await graph.aget_state(thread)).values)
    for question in ("आप कहाँ से बोल रहे हैं?", "Why did you call me?", "നിങ്ങൾ എന്തിനാണ് വിളിച്ചത്?"):
        result = await engine.handle_turn_detailed(**args, user_text=question,
                                                  signal="question", pause_for_context=True)
        assert result["offScript"] and not result["done"]
        assert result["slots"] == first["slots"]
        assert result["awaitingKind"] == kind
        assert (await graph.aget_state(thread)).values == before
    answer = "The package was returned" if kind == "ask" else "yes"
    final = await engine.handle_turn_detailed(**args, user_text=answer, signal="affirm" if kind == "intent" else None)
    assert final["done"]
    if kind == "ask":
        assert final["slots"]["description"] == answer
