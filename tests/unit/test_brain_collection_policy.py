"""ConversationBrain + CollectionCallPolicy — live-turn behavior.

Proves the customer context is actually consumed by the live brain (not just
stored): verified facts reach the LLM's system prompt per turn, identity
gating hides them, the policy pauses the workflow ladder on disputes and
claims, short replies go to the LLM instead of canned clarifications, an
affirmative to the bot's agent offer transfers, terminal states end the call
with the right disposition, and interruptions of audible speech are recorded.
"""

import asyncio

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndWorkerFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.customer_context import CustomerContextSnapshot
from voice_runtime.brain import ConversationBrain

GRACE = 0.05


def snapshot(**overrides) -> CustomerContextSnapshot:
    base = dict(
        context_id="cctx_brain1", tenant_id="tn-x", bot_id="bot-x",
        customer_name="Ramesh Kumar", lender_name="eDAS Finance",
        dcs_name="eDAS Recoveries", loan_account_masked="XX8976",
        phone_masked="XXXXXX0001", phone_last4="0001",
        preferred_language="hi-IN", overdue_amount=4850.0,
        total_outstanding=5120.0, days_overdue=12, due_date="2026-07-23",
        partial_payment_allowed=True, payment_methods=("UPI", "Debit Card"),
        payment_status="pending", customer_verified=False,
        recording_notice_required=True,
        grievance_contact="grievance@example",
    )
    base.update(overrides)
    return CustomerContextSnapshot(**base)


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-policy"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"
        self.disposition = None
        self.customer_context_id = None
        self.call_state = {}

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def event_kinds(self):
        return [kind for kind, _ in self.events]


class _StreamingLLMStub:
    def __init__(self, tokens=("ठीक", " है।")):
        self._tokens = list(tokens)
        self.calls = []
        self.last_stream_usage = None

    def stream(self, history, *, system, temperature, max_tokens):
        self.calls.append({"history": [dict(m) for m in history], "system": system})

        async def _gen():
            for token in self._tokens:
                yield token

        return _gen()


class _WorkflowStub:
    """Would always advance the ladder; records whether it was even asked."""

    def __init__(self):
        self.calls = []

    async def handle_turn_detailed(self, **kwargs):
        self.calls.append(kwargs)
        return {"reply": "RUNG TEXT: कृपया अभी payment कर दीजिए। क्या आप payment करेंगे?",
                "done": False, "status": "collecting", "offScript": False,
                "source": "definition", "workflowId": "wf-x", "trace": [],
                "slots": {}, "signal": None}


def make_brain(*, context=None, llm=None, workflows=None,
               intents=None, verified=False,
               runtime_context=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Collection Bot.",
        intents=intents or [],
    )
    brain = ConversationBrain(
        config=config, llm=llm or _StreamingLLMStub(),
        recorder=_RecorderStub(), workflow_engine=workflows,
        customer_context=context, runtime_context=runtime_context,
        finalize_grace=GRACE, complete_endpoint=GRACE,
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
    if verified and brain._policy is not None:
        brain._policy.verified = True  # in-call verification already done
    return brain


def transcript(text, language="hi-IN"):
    return TranscriptionFrame(text=text, user_id="u", timestamp="t",
                              language=language)


async def turn(brain, text):
    await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await brain.process_frame(transcript(text), FrameDirection.DOWNSTREAM)
    await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(GRACE * 3)
    for _ in range(10):
        await asyncio.sleep(0)


def bot_replies(brain):
    return [n["text"] for n in brain._notified if n.get("type") == "bot_text"]


async def verify_identity(brain):
    """Drive the standard opening: identity question asked, customer affirms."""
    await brain._say("क्या मेरी बात Ramesh Kumar से हो रही है?")
    await turn(brain, "हाँ जी बोल रहा हूँ")


class TestContextReachesTheLLM:
    async def test_verified_facts_in_system_prompt_per_turn(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm)
        await verify_identity(brain)
        await turn(brain, "मेरा कितना अमाउंट overdue है बताइए?")
        system = llm.calls[-1]["system"]
        assert "₹4,850" in system
        assert "XX8976" in system
        assert "Days overdue: 12" in system
        assert "2026-07-23" in system
        assert "UPI, Debit Card" in system
        # Sensitive raw values can never leak into the prompt.
        assert "9000000001" not in system

    async def test_amounts_absent_before_verification(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm)
        await turn(brain, "मुझे बताओ क्या बात है, कौन बोल रहा है?")
        system = llm.calls[-1]["system"]
        assert "NOT confirmed" in system
        assert "₹4,850" not in system
        assert "XX8976" not in system

    async def test_static_prompt_names_the_customer(self):
        brain = make_brain(context=snapshot())
        assert "Ramesh Kumar" in brain._static_system
        assert "eDAS Finance" in brain._static_system

    async def test_identity_confirmation_drives_straight_to_the_ask(self):
        """The turn after "yes, speaking" must open the recovery, not offer help.

        Regression: with no domain policy this turn fell through to a plain
        LLM reply under a generic assistant persona, which produced support
        openers ("kuch madad chahiye?") and handed the agenda to the customer.
        """
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm)
        await verify_identity(brain)
        system = llm.calls[-1]["system"]

        assert "Conversation phase: account_explanation" in system
        assert "Identity: CONFIRMED" in system
        # The next step must be the ask, stated with the verified facts.
        assert "can they pay today" in system
        assert "₹4,850" in system
        assert "Days overdue: 12" in system

    async def test_runtime_context_activates_the_policy(self):
        """A tenant opts in via the schema's domain_policy, not a code path."""
        from shared.runtime_context import build_runtime_context

        fields = [{"key": "customer_name", "type": "string"},
                  {"key": "overdue_amount", "type": "number"},
                  {"key": "due_date", "type": "date"}]
        payload = {"customer_name": "Devendra Mishra",
                   "overdue_amount": 3500, "due_date": "2026-07-28"}

        generic = make_brain(runtime_context=build_runtime_context(
            tenant_id="tn-x", bot_id="bot-x", field_definitions=fields,
            payload=payload, domain_policy="generic"))
        collections = make_brain(runtime_context=build_runtime_context(
            tenant_id="tn-x", bot_id="bot-x", field_definitions=fields,
            payload=payload, domain_policy="collections"))

        assert generic._policy is None
        assert collections._policy is not None
        assert collections._policy.context.overdue_amount == 3500.0

    async def test_short_affirm_goes_to_llm_not_canned_clarify(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm)
        await verify_identity(brain)
        calls_before = len(llm.calls)
        await turn(brain, "जी।")
        assert len(llm.calls) == calls_before + 1  # LLM answered, not canned


class TestPolicyPausesTheLadder:
    async def test_dispute_never_reaches_the_workflow(self):
        workflows = _WorkflowStub()
        llm = _StreamingLLMStub(("ठीक है,", " दर्ज कर लिया।"))
        brain = make_brain(context=snapshot(), llm=llm, workflows=workflows)
        brain._active_workflow = "edas_collection_call"
        await verify_identity(brain)
        await turn(brain, "मैंने कोई लोन लिया ही नहीं है आपसे")
        assert workflows.calls == []  # ladder paused, LLM answered
        system = llm.calls[-1]["system"]
        assert "OPEN ISSUES" in system
        assert "account disputed" in system
        assert brain._recorder.disposition == "account_disputed"

    async def test_payment_claim_never_reaches_the_workflow(self):
        workflows = _WorkflowStub()
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm, workflows=workflows)
        brain._active_workflow = "edas_collection_call"
        await verify_identity(brain)
        await turn(brain, "payment to maine kal hi kar di thi")
        assert workflows.calls == []
        assert brain._recorder.disposition == "payment_claimed"

    async def test_question_answered_not_rung_repeated(self):
        workflows = _WorkflowStub()
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm, workflows=workflows)
        brain._active_workflow = "edas_collection_call"
        await verify_identity(brain)
        await turn(brain, "कौन से लोन की बात कर रहे हो? कितना अमाउंट है?")
        assert workflows.calls == []
        assert "₹4,850" in llm.calls[-1]["system"]


class TestTerminalStates:
    async def test_wrong_party_closes_with_disposition(self):
        llm = _StreamingLLMStub(("माफ़ कीजिए,", " धन्यवाद।"))
        brain = make_brain(context=snapshot(), llm=llm)
        await brain._say("क्या मेरी बात Ramesh Kumar से हो रही है?")
        await turn(brain, "नहीं, मेरा नाम तो सुरेश है, galat number hai")
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)
        assert brain._recorder.disposition in ("identity_mismatch", "wrong_number")
        assert "call_completed_by_policy" in brain._recorder.event_kinds()
        # No account facts were ever available to the goodbye turn.
        assert "₹4,850" not in llm.calls[-1]["system"]

    async def test_callback_with_time_closes(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "main busy hoon, shaam ko call back karna")
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)
        assert brain._recorder.disposition == "callback_requested"

    async def test_affirm_to_agent_offer_transfers(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm, verified=True)
        await brain._say("क्या मैं आपको हमारे agent से connect कर दूँ?")
        await turn(brain, "जी हाँ")
        assert "handoff" in brain._recorder.event_kinds()
        controls = [p for p in brain._pending_controls + brain._notified
                    if p.get("type") == "telephony_control"]
        assert controls and controls[0]["event"] == "transfer"

    async def test_cleanup_records_call_state(self):
        brain = make_brain(context=snapshot())
        await brain._say("क्या मेरी बात Ramesh Kumar से हो रही है?")
        await turn(brain, "हाँ बोल रहा हूँ")
        await brain.cleanup()
        assert brain._recorder.call_state.get("customer_verified") is True
        assert brain._recorder.call_state.get("is_final_transcript") is True


class TestInterruption:
    async def test_barge_in_during_audible_speech_recorded(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "अच्छा बताइए क्या बात है?")
        await brain.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._policy.interruption_detected is True
        assert "barge_in" in brain._recorder.event_kinds()

    async def test_resume_before_audio_is_not_an_interruption(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm, verified=True)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(transcript("मैं अभी"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._policy.interruption_detected is False
