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


def make_brain(workflow_engine=None, llm=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=llm, recorder=_RecorderStub(),
        workflow_engine=workflow_engine, finalize_grace=GRACE,
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
