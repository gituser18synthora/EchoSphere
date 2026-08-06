"""ConversationBrain + hybrid intent pipeline + tool verification.

End-to-end (text) proof of the redesign inside the live brain:

- an already-paid claim classified by the LLM triggers the CONFIGURED
  payment-status tool; the verified result — not the claim — decides what
  the bot may say and what is written back to the account state;
- an unverified / not-reflected check never marks the account paid;
- do-not-call ends the call immediately with a durable disposition;
- a generic (healthcare-style) bot runs on tenant-defined context with no
  collection policy and no loan wording — domain behavior is configuration;
- the deterministic platform paths (hang-up) are untouched by the pipeline.
"""

import asyncio
import json

from pipecat.frames.frames import EndWorkerFrame

from shared.bot_config import ResolvedBotConfig
from shared.runtime_context import build_runtime_context
from voice_runtime.brain import ConversationBrain

from tests.unit.test_brain_collection_policy import (
    GRACE,
    _RecorderStub,
    _StreamingLLMStub,
    _WorkflowStub,
    bot_replies,
    snapshot,
    turn,
    verify_identity,
)

ALREADY_PAID_INTENT = {
    "name": "already_paid",
    "description": "Customer claims the payment was already made",
    "samples": [],
    "confidence_threshold": 0.7,
    "entities": ["payment_date", "payment_method", "transaction_reference"],
    "route": "tool:check_payment_status",
}


class _ClassifierLLMStub(_StreamingLLMStub):
    """Streams replies AND answers classification calls with scripted JSON."""

    def __init__(self, classification: dict, tokens=("ठीक", " है।")):
        super().__init__(tokens=tokens)
        self._classification = classification
        self.generate_calls = []

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        self.generate_calls.append({"messages": messages, "system": system})

        payload = json.dumps(self._classification)

        class _Result:
            text = payload
            input_tokens = 100
            output_tokens = 30

        return _Result()


class _ToolStub:
    """Stands in for the validated executor; records every request."""

    def __init__(self, payload: dict, ok: bool = True):
        self._payload = payload
        self._ok = ok
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        from shared.orchestration.tool_executor import ToolResult

        if not self._ok:
            return ToolResult(tool=kwargs["tool"], ok=False, status="error",
                              error="upstream down")
        return ToolResult(
            tool=kwargs["tool"], ok=True, status="ok",
            data=self._payload, mapped=dict(self._payload),
        )


def make_hybrid_brain(*, context=None, runtime_context=None, llm=None,
                      intents=None, tool=None, workflows=None,
                      system_prompt="You are Collection Bot.") -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt=system_prompt,
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
    if tool is not None:
        brain._tools = tool
    return brain


CLASSIFIED_ALREADY_PAID = {
    "intent": "already_paid", "signal": "already_paid", "confidence": 0.96,
    "entities": {"payment_date": "kal", "payment_method": "UPI",
                 "transaction_reference": None},
    "should_interrupt_current_flow": True,
}


class TestAlreadyPaidToolVerification:
    async def test_confirmed_payment_reaches_prompt_and_state(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        tool = _ToolStub({"payment_status": "completed"})
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT], tool=tool)
        await verify_identity(brain)
        await turn(brain, "वो वाला बकाया तो कल ही निपटा दिया था यूपीआई से")
        # The configured tool ran, with backend-validated identity fields.
        assert tool.calls and tool.calls[-1]["tool"] == "check_payment_status"
        assert tool.calls[-1]["args"]["payment_date"] == "kal"
        assert tool.calls[-1]["intent"] == "already_paid"
        # The verified outcome is spoken SCRIPTED from the tool result —
        # the LLM never gets to phrase (or embellish) a verification claim.
        replies = bot_replies(brain)
        assert "पुष्टि हो" in replies[-1] and "भुगतान" in replies[-1]
        # …and ONLY the tool marks the account paid.
        assert brain._policy.payment_verified_status == "completed"
        assert brain._policy.verification_outcome == "verified"
        # The completion evaluator approved the close.
        assert brain._closing
        await brain.cleanup()
        assert brain._recorder.call_state.get("payment_status") == "completed"

    async def test_unreflected_payment_never_marks_paid(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        tool = _ToolStub({"payment_status": "pending"})
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT], tool=tool)
        await verify_identity(brain)
        await turn(brain, "maine payment kar di thi kal hi")
        # An inconclusive account-level check does NOT settle the claim: the
        # bot moves to capturing the transaction reference instead.
        replies = bot_replies(brain)
        assert "ट्रांजैक्शन" in replies[-1]
        assert brain._policy.awaiting_reference
        assert brain._policy.verification_outcome is None
        assert not brain._closing
        await brain.cleanup()
        # The claim alone must not settle the account.
        assert brain._recorder.call_state.get("payment_status") != "completed"

    async def test_no_tool_configured_keeps_unverified_flow(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        tool = _ToolStub({"payment_status": "completed"})
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[], tool=tool)
        await verify_identity(brain)
        await turn(brain, "payment to maine kar di thi")
        assert tool.calls == []  # nothing configured → nothing executed
        system = llm.calls[-1]["system"]
        assert "cannot check it on this call" in system \
            or "NO backend tools" in system
        await brain.cleanup()
        assert brain._recorder.call_state.get("payment_status") != "completed"

    async def test_tool_failure_is_honest(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        tool = _ToolStub({}, ok=False)
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT], tool=tool)
        await verify_identity(brain)
        await turn(brain, "bhugtan ho chuka hai mera")
        # A failed check verifies nothing: no success claim anywhere, and the
        # flow proceeds to capture the transaction reference.
        replies = bot_replies(brain)
        assert "पुष्टि हो चुकी" not in replies[-1]
        assert "ट्रांजैक्शन" in replies[-1]
        assert brain._policy.payment_verified_status is None
        assert brain._policy.verification_outcome is None

    async def test_pitch_not_repeated_after_claim(self):
        """The claim pauses the ladder: the workflow is never consulted."""
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        workflows = _WorkflowStub()
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT],
                                  tool=_ToolStub({"payment_status": "completed"}),
                                  workflows=workflows)
        brain._active_workflow = "payment_collection"
        await verify_identity(brain)
        await turn(brain, "are bhai payment kar di maine")
        assert workflows.calls == []


class TestDoNotCall:
    async def test_dnc_ends_call_with_disposition(self):
        brain = make_hybrid_brain(context=snapshot())
        await turn(brain, "mujhe dobara call mat karna")
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)
        assert brain._recorder.disposition == "do_not_call"
        assert brain._recorder.call_state["last_disposition"] == "do_not_call"
        replies = bot_replies(brain)
        assert replies and ("डू-नॉट-कॉल" in replies[-1] or "do-not-call" in replies[-1])
        # Disposition survives policy finalization at teardown.
        await brain.cleanup()
        assert brain._recorder.disposition == "do_not_call"

    async def test_nothing_speaks_after_dnc(self):
        brain = make_hybrid_brain(context=snapshot())
        await turn(brain, "phir se phone mat karo")
        spoken_before = len(bot_replies(brain))
        await turn(brain, "hello?")
        assert len(bot_replies(brain)) == spoken_before


class TestGenericDomainBot:
    """A healthcare-style bot: tenant context, no collection policy."""

    def _healthcare_context(self):
        return build_runtime_context(
            tenant_id="tn-x", bot_id="bot-x",
            field_definitions=[
                {"key": "patient_name", "type": "string"},
                {"key": "appointment", "type": "object"},
                {"key": "patient_id", "type": "string", "sensitive": True},
            ],
            payload={
                "patient_name": "Meera Iyer",
                "appointment": {"date": "2026-08-11", "doctor": "Dr. Kulkarni"},
                "patient_id": "MRN-778812",
            },
            payload_source="api",
        )

    async def test_no_policy_and_context_in_prompt(self):
        llm = _StreamingLLMStub()
        brain = make_hybrid_brain(
            runtime_context=self._healthcare_context(), llm=llm,
            system_prompt="You are {clinic_name} appointment assistant "
                          "for {patient_name}.",
        )
        assert brain._policy is None  # collections logic is NOT global
        await turn(brain, "मेरी अपॉइंटमेंट कब है?")
        system = llm.calls[-1]["system"]
        assert "Meera Iyer" in system
        assert "Caller context" in system
        assert "MRN-778812" not in system          # sensitive → masked
        # No loan-collection wording leaks into a generic domain.
        assert "Live call state" not in system
        assert "overdue" not in system.lower()
        assert "loan account" not in system.lower()

    async def test_prompt_variables_resolve_from_context(self):
        llm = _StreamingLLMStub()
        brain = make_hybrid_brain(
            runtime_context=self._healthcare_context(), llm=llm,
            system_prompt="You help {patient_name} with appointments.",
        )
        assert "You help Meera Iyer" in brain._static_system

    async def test_record_id_tracked_for_write_back(self):
        ctx = self._healthcare_context()
        ctx.record_id = "rcr_123"
        brain = make_hybrid_brain(runtime_context=ctx)
        assert brain._recorder.runtime_context_record_id == "rcr_123"


class TestDeterministicPathsUntouched:
    async def test_hangup_never_reaches_classifier_or_tool(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        tool = _ToolStub({"payment_status": "completed"})
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT], tool=tool)
        await turn(brain, "call band kar do")
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)
        assert llm.generate_calls == []
        assert tool.calls == []
