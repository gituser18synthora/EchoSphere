"""Hybrid intent pipeline — LLM understanding with deterministic edges.

Pins the redesign's ordering: tenant phrases are only a fast path, the LLM
classifies the completed turn (multilingual already_paid without regex),
confidence gates actions, tool selection comes from intent CONFIG (never
from the model), and an LLM failure/timeout degrades to the legacy regex
signals instead of dropping the turn.
"""

import asyncio
import json

from shared.orchestration.intent_classifier import (
    HybridIntentPipeline,
    IntentClassification,
)

INTENTS = [
    {
        "name": "already_paid",
        "description": "Customer claims the payment was already made",
        "samples": ["maine payment kar diya"],
        "confidence_threshold": 0.7,
        "entities": ["payment_date", "payment_method", "transaction_reference"],
        "route": "tool:check_payment_status",
    },
    {
        "name": "book_appointment",
        "description": "Patient wants to book a clinic appointment",
        "samples": ["book an appointment"],
        "confidence_threshold": 0.6,
        "entities": ["appointment_date"],
        "route": "workflow:appointment_booking",
    },
]


class _FakeLLM:
    """Returns a scripted classifier answer; records the request."""

    def __init__(self, reply: str | dict, delay: float = 0.0, fail: bool = False):
        self._reply = json.dumps(reply) if isinstance(reply, dict) else reply
        self._delay = delay
        self._fail = fail
        self.calls: list[dict] = []

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        self.calls.append({"messages": messages, "system": system})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("provider down")

        class _Result:
            text = self._reply
            input_tokens = 120
            output_tokens = 40

        return _Result()


class TestLLMClassification:
    async def test_hindi_already_paid_without_regex_shape(self):
        """A phrasing the legacy regex does NOT cover still classifies."""
        llm = _FakeLLM({
            "intent": "already_paid", "signal": "already_paid",
            "confidence": 0.96,
            "entities": {"payment_date": "kal", "payment_method": "UPI",
                         "transaction_reference": None},
            "should_interrupt_current_flow": True,
        })
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        # Deliberately regex-unfriendly phrasing.
        result = await pipeline.classify("वो वाला बकाया तो कल ही निपटा दिया था यूपीआई से")
        assert result.source == "llm"
        assert result.intent == "already_paid"
        assert result.signal == "already_paid"
        assert result.confidence == 0.96
        assert result.below_threshold is False
        assert result.entities["payment_date"] == "kal"
        assert result.entities["transaction_reference"] is None
        assert result.should_interrupt_current_flow is True
        # Tool from CONFIG, not from the model's answer.
        assert result.requires_tool is True
        assert result.tool_name == "check_payment_status"

    async def test_english_and_hinglish_prompt_content(self):
        llm = _FakeLLM({"intent": None, "signal": "already_paid",
                        "confidence": 0.9, "entities": {}})
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        result = await pipeline.classify("I have already cleared that EMI last week")
        assert result.signal == "already_paid"
        # The classifier saw the tenant intents and the signal vocabulary.
        system = llm.calls[0]["system"]
        assert "already_paid" in system and "book_appointment" in system
        assert "Hinglish" in system
        assert "NEVER invent values" in system

    async def test_unknown_intent_name_discarded(self):
        llm = _FakeLLM({"intent": "made_up_thing", "confidence": 0.99,
                        "entities": {}})
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        result = await pipeline.classify("kuch bhi")
        assert result.intent is None       # a fabricated name routes nowhere
        assert result.requires_tool is False

    async def test_platform_signal_in_intent_slot_is_recovered(self):
        llm = _FakeLLM({"intent": "hardship", "confidence": 0.9, "entities": {}})
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        result = await pipeline.classify("नौकरी चली गई है, अभी बिल्कुल संभव नहीं")
        assert result.intent is None
        assert result.signal == "hardship"

    async def test_below_threshold_flagged_not_routed(self):
        llm = _FakeLLM({"intent": "already_paid", "confidence": 0.4,
                        "entities": {}})
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        result = await pipeline.classify("hmm paisa …")
        assert result.intent == "already_paid"
        assert result.below_threshold is True

    async def test_history_and_workflow_context_passed(self):
        llm = _FakeLLM({"intent": None, "signal": "affirm", "confidence": 0.8,
                        "entities": {}})
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        await pipeline.classify(
            "haan", [{"role": "assistant", "content": "Shall I book it?"}],
            active_workflow="appointment_booking",
        )
        prompt = llm.calls[0]["messages"][0]["content"]
        assert "Shall I book it?" in prompt
        assert "appointment_booking" in prompt


class TestFallbacks:
    async def test_phrase_fast_path_skips_llm(self):
        llm = _FakeLLM({"intent": "wrong"}, fail=True)  # would blow up if called
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        result = await pipeline.classify("maine payment kar diya")
        assert result.source == "phrase"
        assert result.intent == "already_paid"
        assert result.tool_name == "check_payment_status"
        assert llm.calls == []

    async def test_llm_failure_falls_back_to_regex(self):
        llm = _FakeLLM("", fail=True)
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        result = await pipeline.classify("पेमेंट कर दी है मैंने")
        assert result.source == "regex"
        assert result.signal == "already_paid"

    async def test_llm_timeout_falls_back(self):
        llm = _FakeLLM({"intent": "already_paid", "confidence": 1.0,
                        "entities": {}}, delay=0.5)
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS,
                                        timeout_seconds=0.05)
        result = await pipeline.classify("payment ho chuki hai")
        assert result.source == "regex"
        assert result.signal == "already_paid"

    async def test_garbage_json_falls_back(self):
        llm = _FakeLLM("sure! the intent is probably already_paid :)")
        pipeline = HybridIntentPipeline(llm=llm, intents=INTENTS)
        # A phrasing the legacy regex DOES cover — the fallback's job.
        result = await pipeline.classify("payment ho gayi hai meri")
        assert result.source == "regex"
        assert result.signal == "already_paid"

    async def test_disabled_pipeline_uses_regex_only(self):
        pipeline = HybridIntentPipeline(llm=None, intents=INTENTS, enabled=False)
        result = await pipeline.classify("main busy hoon, baad mein call karna")
        assert result.source == "regex"
        assert result.signal == "callback"

    async def test_empty_turn(self):
        pipeline = HybridIntentPipeline(llm=None, intents=[])
        result = await pipeline.classify("   ")
        assert result.source == "none"
        assert result.intent is None


class TestEventShape:
    def test_as_event_is_json_safe(self):
        event = IntentClassification(
            intent="already_paid", signal="already_paid", confidence=0.956,
            entities={"payment_date": "kal", "payment_method": None},
            requires_tool=True, tool_name="check_payment_status",
            should_interrupt_current_flow=True, source="llm", latency_ms=412.3,
        ).as_event()
        assert event["confidence"] == 0.956
        assert event["entities"] == {"payment_date": "kal"}
        assert event["tool"] == "check_payment_status"
        json.dumps(event)
