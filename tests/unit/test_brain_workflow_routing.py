"""Workflow routing inside the brain: every complete user message is
evaluated before a response is selected.

Off-script turns (hardship, complaints, questions the current workflow node
has no edge for) must be answered by the LLM — grounded in the paused step,
with the no-invented-facts rules in force — while the workflow stays at the
same node. Barge-in must never advance the workflow, and a new call starts
with clean conversation and workflow state.
"""

import asyncio

from pipecat.frames.frames import (
    InterruptionFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.runtime_context import RuntimeContext
from voice_runtime.brain import ConversationBrain


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0,
                      "llm_input_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def flush_event_soon(self, kind, **data):
        self.events.append((kind, data))


    def event_kinds(self):
        return [kind for kind, _ in self.events]


class _WorkflowStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def handle_turn_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _LLMStub:
    """Records the system prompt and streams a fixed reply."""

    def __init__(self, reply="ठीक है, मैं समझ गया।"):
        self.reply = reply
        self.systems = []
        self.histories = []

    def stream(self, history, *, system, temperature, max_tokens):
        self.systems.append(system)
        self.histories.append(list(history))

        async def _gen():
            yield self.reply

        return _gen()


GRACE = 0.05


def make_brain(workflow_engine=None, llm=None, runtime_context=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        workflow_engine=workflow_engine, finalize_grace=GRACE,
        runtime_context=runtime_context,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    return brain


def off_script_result(node_prompt="क्या आप अभी payment करेंगे?", signal="complaint"):
    return {
        "reply": "", "done": False, "status": "collecting",
        "source": "definition", "workflowId": "wf_1", "trace": ["n_push"],
        "slots": {}, "handoffQueue": None,
        "offScript": True, "nodePrompt": node_prompt, "signal": signal,
    }


def consumed_result(reply, done=False):
    return {
        "reply": reply, "done": done, "status": "done" if done else "collecting",
        "source": "definition", "workflowId": "wf_1", "trace": ["n_push"],
        "slots": {}, "handoffQueue": None,
        "offScript": False, "nodePrompt": None, "signal": None,
    }


def transcript(text):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t",
                              language="hi-IN")


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


class TestOffScriptTurns:
    async def test_verified_fact_question_bypasses_active_workflow(self):
        context = RuntimeContext(
            tenant_id="tn-x", bot_id="bot-x",
            session_verification_required=True,
        )
        context.set_workflow_value("customer_verified", True)
        context.set_workflow_value("booking_id", "601001")
        context.set_workflow_value("hotel_name", "OYO Townhouse 121")
        engine = _WorkflowStub(consumed_result("generic details menu"))
        llm = _LLMStub(reply="Your hotel is OYO Townhouse 121.")
        brain = make_brain(
            workflow_engine=engine, llm=llm, runtime_context=context,
        )
        brain._active_workflow = "oyo_booking_support_journey"

        await brain._handle_turn("Can you confirm my hotel name?")

        assert engine.calls == []
        assert brain._active_workflow == "oyo_booking_support_journey"
        assert brain._history[-1]["content"] == "Your hotel is OYO Townhouse 121."
        route_events = [data for kind, data in brain._recorder.events
                        if kind == "route_decision"]
        assert route_events[-1]["route"] == "chat"
        assert route_events[-1]["reason"] == (
            "verified_context_question_during_workflow"
        )

    async def test_related_workflow_reuses_verified_slots(self):
        context = RuntimeContext(
            tenant_id="tn-x", bot_id="bot-x",
            session_verification_required=True,
        )
        context.set_workflow_value("customer_verified", True)
        context.set_workflow_value("booking_id", "601001")
        context.set_workflow_value("guest_name", "Rahul Sharma")
        engine = _WorkflowStub(consumed_result("send voucher?"))
        brain = make_brain(
            workflow_engine=engine, llm=_LLMStub(), runtime_context=context,
        )

        await brain._handle_workflow(
            type("Decision", (), {"action": "oyo_booking_support_journey",
                                  "signal": None})(),
            "please email my booking voucher", 0.0,
        )

        assert engine.calls[0]["initial_slots"]["booking_id"] == "601001"
        assert engine.calls[0]["initial_slots"]["guest_name"] == "Rahul Sharma"
        assert engine.calls[0]["reset_state"] is False
        assert context.is_session_verified()

    async def test_complaint_answered_by_llm_and_node_kept(self):
        """The caller says the bot is not listening: the workflow must not
        advance and the LLM must answer, grounded in the paused step."""
        engine = _WorkflowStub(off_script_result())
        llm = _LLMStub(reply="माफ़ कीजिए, बताइए क्या कहना चाहते हैं?")
        brain = make_brain(workflow_engine=engine, llm=llm)
        brain._active_workflow = "edas_collection_call"

        await brain._handle_turn("aap meri baat sun nahi rahe ho")

        assert len(engine.calls) == 1
        assert brain._active_workflow == "edas_collection_call"  # node kept
        assert len(llm.systems) == 1
        system = llm.systems[0]
        assert "Paused call flow" in system
        assert "क्या आप अभी payment करेंगे?" in system  # grounded in the step
        assert "Never invent promises" in system  # no unsupported claims
        assert "workflow_off_script" in brain._recorder.event_kinds()
        # The LLM's answer was spoken and recorded.
        assert any(isinstance(f, TextFrame) for f in brain._pushed)
        assert brain._history[-1]["content"] == "माफ़ कीजिए, बताइए क्या कहना चाहते हैं?"

    async def test_hardship_signal_reaches_the_route_decision(self):
        engine = _WorkflowStub(off_script_result(signal="hardship"))
        llm = _LLMStub()
        brain = make_brain(workflow_engine=engine, llm=llm)
        brain._active_workflow = "edas_collection_call"

        await brain._handle_turn("मेरे पास पैसे नहीं हैं")

        route_events = [d for k, d in brain._recorder.events
                        if k == "route_decision"]
        assert route_events and route_events[0]["signal"] == "hardship"

    async def test_consumed_workflow_turn_speaks_reply_without_llm(self):
        engine = _WorkflowStub(consumed_result("मैं समझ रहा हूँ, अफ़सोस है।"))
        llm = _LLMStub()
        brain = make_brain(workflow_engine=engine, llm=llm)
        brain._active_workflow = "edas_collection_call"

        await brain._handle_turn("mere paas paise nahi hain")

        assert llm.systems == []  # scripted reply, no generation
        assert brain._active_workflow == "edas_collection_call"
        assert brain._history[-1]["content"] == "मैं समझ रहा हूँ, अफ़सोस है।"

    async def test_done_workflow_clears_active_state(self):
        engine = _WorkflowStub(consumed_result("धन्यवाद, शुभ दिन!", done=True))
        brain = make_brain(workflow_engine=engine, llm=_LLMStub())
        brain._active_workflow = "edas_collection_call"
        await brain._handle_turn("haan theek hai")
        assert brain._active_workflow is None


class TestBargeInAndIsolation:
    async def test_barge_in_does_not_advance_the_workflow(self):
        """A barge-in (caller interrupting TTS) must never reach the workflow
        engine — only a completed user turn may."""
        engine = _WorkflowStub(consumed_result("reply"))
        brain = make_brain(workflow_engine=engine, llm=_LLMStub())
        brain._active_workflow = "edas_collection_call"

        # Transcript arrives with no open turn → finalize debounce armed.
        await brain.process_frame(transcript("अच्छा"), FrameDirection.DOWNSTREAM)
        # The caller keeps talking (barge-in) before the debounce fires.
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 3)
        await settle()
        assert engine.calls == []  # nothing advanced

        # The full utterance closes the turn → exactly ONE workflow call.
        await brain.process_frame(transcript("ठीक है बताइए"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(GRACE * 3)
        await settle()
        assert len(engine.calls) == 1
        assert engine.calls[0]["user_text"] == "अच्छा ठीक है बताइए"

    async def test_new_call_starts_with_clean_state(self):
        """Each call builds a fresh brain: no history, no active workflow,
        no leftover segments from any previous session."""
        first = make_brain(workflow_engine=_WorkflowStub(consumed_result("r")),
                           llm=_LLMStub())
        first._active_workflow = "edas_collection_call"
        first._history.append({"role": "user", "content": "पैसे नहीं हैं"})

        fresh = make_brain(workflow_engine=_WorkflowStub(consumed_result("r")),
                           llm=_LLMStub())
        assert fresh._active_workflow is None
        assert fresh._history == []
        assert fresh._pending_segments == []


class TestHoldFastPath:
    """'Ek minute ruko' during a workflow: one canned acknowledgement, no LLM
    turn, no workflow step consumed — and the node is still there when the
    caller returns. Explicit callback wording keeps routing to the flow."""

    async def test_hold_acknowledged_without_engine_or_llm(self):
        engine = _WorkflowStub(off_script_result())
        llm = _LLMStub()
        brain = make_brain(workflow_engine=engine, llm=llm)
        brain._active_workflow = "frankfinn_seminar"

        await brain._handle_turn("हां, एक मिनट रुक जाओ")

        assert engine.calls == []                       # node untouched
        assert llm.systems == []                        # no generation
        assert brain._active_workflow == "frankfinn_seminar"
        assert "hold_acknowledged" in brain._recorder.event_kinds()
        assert brain._history[-1] == {"role": "assistant",
                                      "content": "जी बिल्कुल, मैं line पर हूँ।"}
        assert brain._hold_requested_at is not None
        route = [d for k, d in brain._recorder.events if k == "route_decision"][-1]
        assert route["signal"] == "hold"

    async def test_dont_disconnect_is_hold_not_hangup(self):
        engine = _WorkflowStub(off_script_result())
        brain = make_brain(workflow_engine=engine, llm=_LLMStub())
        brain._active_workflow = "frankfinn_seminar"

        await brain._handle_turn("मुझे एक मिनट दो, कट मत करो")

        assert brain._closing is False
        assert "hold_acknowledged" in brain._recorder.event_kinds()
        assert not any(k == "call_control" for k, _ in brain._recorder.events)

    async def test_callback_wording_still_reaches_the_workflow(self):
        engine = _WorkflowStub(consumed_result("kab call karein?"))
        brain = make_brain(workflow_engine=engine, llm=_LLMStub())
        brain._active_workflow = "frankfinn_seminar"

        await brain._handle_turn("ek minute baad call karo")

        assert len(engine.calls) == 1
        assert engine.calls[0]["signal"] == "callback"
        assert "hold_acknowledged" not in brain._recorder.event_kinds()
