"""Telephony control events from the brain: transfer/stop controls are queued
and only leave for the transport once the bot has finished SPEAKING the
accompanying announcement (BotStoppedSpeaking) — or immediately on a caller
barge-in — never racing ahead of the still-rendering TTS audio. Workflow
`handover` nodes escalate through the same path, carrying their configured
queue. Dialer call-context variables become reference data in the system
prompt."""

from pipecat.frames.frames import BotStoppedSpeakingFrame, UserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.router import RouteDecision, RouteKind
from voice_runtime.brain import ConversationBrain


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        pass

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))


class _WorkflowStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def handle_turn_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def make_brain(workflow_engine=None, call_context=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        workflow_engine=workflow_engine, call_context=call_context,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    return brain


class TestDeferredTransferControls:
    async def test_handoff_defers_transfer_until_bot_stops_speaking(self):
        brain = make_brain()
        await brain._handle_handoff(
            RouteDecision(kind=RouteKind.HANDOFF, reason="explicit_transfer_request")
        )
        # announcement queued for TTS, control NOT yet on the wire
        assert brain._pending_controls and not any(
            n.get("type") == "telephony_control" for n in brain._notified
        )
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        controls = [n for n in brain._notified if n.get("type") == "telephony_control"]
        assert controls == [{
            "type": "telephony_control", "event": "transfer",
            "reason": "explicit_transfer_request",
        }]
        assert brain._pending_controls == []
        # a later bot-stopped must not re-send it
        await brain.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        assert len([n for n in brain._notified
                    if n.get("type") == "telephony_control"]) == 1

    async def test_barge_in_flushes_pending_control(self):
        brain = make_brain()
        await brain._handle_handoff(RouteDecision(kind=RouteKind.HANDOFF, reason=""))
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        controls = [n for n in brain._notified if n.get("type") == "telephony_control"]
        assert len(controls) == 1 and controls[0]["reason"] == "transfer"

    async def test_workflow_handover_queues_transfer_with_queue(self):
        engine = _WorkflowStub({
            "reply": "That amount needs an agent's approval.",
            "done": True, "status": "handoff", "source": "definition",
            "workflowId": "wf_1", "trace": ["n6"], "slots": {},
            "handoffQueue": "billing",
        })
        brain = make_brain(workflow_engine=engine)
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="payment_plan_journey"),
            "only 100",
        )
        assert brain._active_workflow is None
        assert brain._pending_controls == [{
            "type": "telephony_control", "event": "transfer",
            "reason": "workflow_handover", "transfer_queue": "billing",
        }]

    async def test_workflow_done_without_handoff_queues_nothing(self):
        engine = _WorkflowStub({
            "reply": "Thank you, goodbye!", "done": True, "status": "done",
            "source": "definition", "workflowId": "wf_1", "trace": ["n7"],
            "slots": {}, "handoffQueue": None,
        })
        brain = make_brain(workflow_engine=engine)
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="payment_plan_journey"),
            "yes",
        )
        assert brain._pending_controls == []


class TestCallContext:
    def test_context_instruction_lists_dialer_values(self):
        brain = make_brain(call_context={"customer_name": "Rahul",
                                         "outstanding_amount": "2000"})
        text = brain._call_context_instruction()
        assert "# Call context" in text
        assert "- customer_name: Rahul" in text
        assert "- outstanding_amount: 2000" in text
        assert "never invent values" in text

    def test_no_context_means_no_instruction(self):
        assert make_brain()._call_context_instruction() == ""
