"""Deterministic fast paths and the one-LLM-per-turn guarantees.

The latency contract this suite pins:

1. **Zero LLM calls** for turns the platform can resolve deterministically
   against the CURRENT pending question — a clear yes/no to the identity
   question, an explicit transaction reference for the pending slot, hang-up,
   do-not-call, an accepted agent offer, an explicit agent request. The Goal
   Engine is never consulted and no reply generation runs when the reply is
   fully scripted from verified facts.
2. **Ambiguous, compound or off-question turns still go to the Goal Engine**
   — the fast path must never guess.
3. **One LLM call total** for an ordinary successful decision turn: the
   validated ``response_text`` is spoken directly, with no second
   reply-generation request.
4. **A Goal Engine timeout falls back deterministically and immediately** —
   it never triggers a sequential intent-classification LLM call.
5. **KB and tool turns keep their required stages** — direct speech never
   bypasses retrieval or verification.
"""

import asyncio
import json
import time

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.phrases import canned as platform_canned
from voice_runtime.brain import ConversationBrain
from voice_runtime.call_policy import canned

from tests.unit.test_agentic_orchestration import (
    IDENTITY_QUESTION,
    _AgenticLLMStub,
    events,
    make_agentic_brain,
)
from tests.unit.test_brain_collection_policy import (
    GRACE,
    _RecorderStub,
    _StreamingLLMStub,
    bot_replies,
    snapshot,
    turn,
)


def stream_calls(llm):
    """Stage-B reply generations the turn actually paid for."""
    return llm.calls


def decision_calls(llm):
    """Stage-A decision calls the turn actually paid for."""
    return llm.generate_calls


# ── 1: zero-LLM deterministic turns ──────────────────────────────────────────


class TestZeroLlmFastPaths:
    async def test_clear_identity_yes_never_calls_the_llm(self):
        # recording_notice_required=False so the opener is fully scripted:
        # the whole turn — understanding AND reply — runs without a model.
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(
            context=snapshot(recording_notice_required=False), llm=llm,
        )
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "हाँ, मैं बोल रहा हूँ")
        assert decision_calls(llm) == [], "identity yes paid a decision call"
        assert stream_calls(llm) == [], "identity yes paid a reply generation"
        assert brain._policy.verified
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "identity_confirmed"
        rows = events(brain, "orchestration_turn")
        assert rows[-1]["interpretation"] == "deterministic"

    async def test_clear_identity_no_never_calls_the_llm(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "गलत नंबर है")
        assert decision_calls(llm) == []
        assert brain._policy.wrong_party
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "identity_denied"

    async def test_hangup_never_calls_the_llm(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await turn(brain, "फोन काट दो")
        assert decision_calls(llm) == []
        assert stream_calls(llm) == []
        assert brain._closing
        assert bot_replies(brain)[-1] == platform_canned("hangup_ack", "hi-IN")

    async def test_do_not_call_never_calls_the_llm(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await turn(brain, "dobara call mat karna")
        assert decision_calls(llm) == []
        assert stream_calls(llm) == []
        assert brain._recorder.disposition == "do_not_call"

    async def test_agent_offer_yes_transfers_without_the_llm(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await brain._say("क्या मैं आपको हमारे agent से connect कर दूँ?")
        await turn(brain, "जी हाँ")
        assert decision_calls(llm) == []
        assert stream_calls(llm) == []
        assert "handoff" in [k for k, _ in brain._recorder.events]
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "agent_offer_accepted"

    async def test_short_explicit_agent_request_skips_the_engine(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "एजेंट से बात कराओ")
        assert decision_calls(llm) == []
        assert "handoff" in [k for k, _ in brain._recorder.events]
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "agent_requested"

    async def test_explicit_reference_for_the_pending_slot_is_zero_llm(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        policy = brain._policy
        policy.payment_claimed = True
        policy.payment_claim_stage = 1
        policy.awaiting_reference = True
        await brain._say("कृपया ट्रांजैक्शन नंबर बताइए।")
        await turn(brain, "एक दो तीन चार पांच छह सात आठ नौ शून्य एक दो")
        assert decision_calls(llm) == []
        assert stream_calls(llm) == []
        assert policy.transaction_reference == "123456789012"
        # No tool on this call: the outcome stays honestly unverified and the
        # scripted close never claims verification.
        assert policy.verification_outcome == "unverified"
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "reference_provided"

    async def test_clear_payment_commitment_skips_the_decision_call(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "हाँ कल payment कर दूंगा")
        # Understanding was deterministic (no decision LLM); the natural
        # reply still generates — exactly one model call for the turn.
        assert decision_calls(llm) == []
        assert len(stream_calls(llm)) == 1
        assert brain._policy.promise_to_pay
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "payment_commitment"

    async def test_clear_payment_hardship_skips_the_decision_call(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "नहीं यार, मेरे पास पैसे अभी नहीं हैं।")
        assert decision_calls(llm) == []
        assert brain._policy.hardship_raised
        fast = events(brain, "deterministic_fast_path")
        assert fast and fast[-1]["rule"] == "payment_hardship"

    async def test_fast_path_turns_never_start_a_decision_prefetch(self):
        llm = _AgenticLLMStub([])
        brain = make_agentic_brain(
            context=snapshot(recording_notice_required=False), llm=llm,
        )
        await brain._say(IDENTITY_QUESTION)
        brain._pending_segments = ["हाँ, मैं बोल रहा हूँ"]
        brain._start_decision_prefetch()
        assert brain._decision_prefetch is None
        assert decision_calls(llm) == []


# ── 2: anything unclear still goes to the Goal Engine ────────────────────────


class TestAmbiguityStillUsesTheEngine:
    async def test_ambiguous_identity_answer_consults_the_engine(self):
        llm = _AgenticLLMStub([{
            "intent": "identity_confirmation", "decision": "ambiguous",
            "scope": "in_scope", "confidence": 0.7,
            "next_action": "ask_identity_confirmation",
            "response_text": "क्या मेरी बात Ramesh Kumar जी से हो रही है?",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "I mean बोल रहा हूँ।")
        assert len(decision_calls(llm)) == 1
        assert events(brain, "deterministic_fast_path") == []
        assert not brain._policy.verified

    async def test_compound_identity_answer_consults_the_engine(self):
        llm = _AgenticLLMStub([{
            "intent": "identity_confirmation", "decision": "confirmed",
            "signal": "hardship", "scope": "in_scope", "confidence": 0.9,
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm)
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "हाँ मैं ही बोल रहा हूँ लेकिन अभी पैसे नहीं हैं भाई")
        assert len(decision_calls(llm)) == 1
        assert events(brain, "deterministic_fast_path") == []

    async def test_exists_claimed_reference_consults_the_engine(self):
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9,
            "slots": {"transaction_reference": {"status": "exists_claimed"}},
            "next_action": "request_slot_value",
            "response_text": "जी, कृपया ट्रांजैक्शन नंबर बोलिए।",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        policy = brain._policy
        policy.payment_claimed = True
        policy.payment_claim_stage = 1
        policy.awaiting_reference = True
        await brain._say("कृपया ट्रांजैक्शन नंबर बताइए।")
        await turn(brain, "हाँ हाँ नंबर है।")
        assert len(decision_calls(llm)) == 1
        assert policy.transaction_reference is None

    async def test_agent_mention_in_a_long_sentence_is_not_a_request(self):
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.8, "next_action": "answer",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "कल आपके agent से मेरी बात हुई थी उस बारे में बताना था मुझे")
        assert len(decision_calls(llm)) == 1
        assert events(brain, "deterministic_fast_path") == []
        assert "handoff" not in [k for k, _ in brain._recorder.events]

    async def test_payment_words_with_open_blockers_consult_the_engine(self):
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.8, "signal": "payment_intent",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        brain._policy.dispute_raised = True  # open blocker: judgement call
        await turn(brain, "हाँ payment कर दूंगा")
        assert len(decision_calls(llm)) == 1
        assert events(brain, "deterministic_fast_path") == []


# ── 3: one LLM request total on the normal decision path ─────────────────────


class TestSingleLlmDecisionTurns:
    async def test_valid_response_text_skips_the_second_llm(self):
        reply = "आपका बकाया चार हज़ार आठ सौ पचास रुपये है। क्या आप आज payment कर पाएंगे?"
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9, "next_action": "answer",
            "response_text": reply,
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "मुझे अपने बकाया के बारे में बताइए")
        assert len(decision_calls(llm)) == 1, "the decision call"
        assert stream_calls(llm) == [], "a second reply-generation ran"
        assert bot_replies(brain)[-1] == reply

    async def test_continue_workflow_without_active_workflow_is_direct(self):
        reply = "जी बिल्कुल, आप UPI से payment कर सकते हैं।"
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9,
            "next_action": "continue_workflow", "response_text": reply,
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "UPI से payment हो जाएगा क्या बताइए")
        assert len(decision_calls(llm)) == 1
        assert stream_calls(llm) == []
        assert bot_replies(brain)[-1] == reply

    async def test_empty_response_text_falls_back_to_generation(self):
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9, "next_action": "answer",
            "response_text": "",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        await turn(brain, "मुझे अपने बकाया के बारे में बताइए")
        assert len(decision_calls(llm)) == 1
        assert len(stream_calls(llm)) == 1  # Stage-B covered the turn

    async def test_language_mismatched_response_text_is_not_spoken(self):
        # The decision wrote Hindi while the caller switched to English: the
        # direct path refuses and Stage-B (strict language instruction) runs.
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9, "next_action": "answer",
            "response_text": "आपका बकाया चार हज़ार रुपये है।",
        }])
        brain = make_agentic_brain(context=snapshot(), llm=llm, verified=True)
        brain._conversation_language = "en-IN"
        await brain._handle_turn("please tell me my outstanding balance")
        assert len(stream_calls(llm)) == 1
        mismatches = events(brain, "direct_reply_language_mismatch")
        assert mismatches

    async def test_unsafe_next_actions_never_speak_directly(self):
        brain = make_agentic_brain(context=snapshot(), verified=True)
        from shared.orchestration.decision_schema import ConversationDecision

        for action in ("call_tool", "answer_from_knowledge", "end_call",
                       "escalate_to_human"):
            decision = ConversationDecision.model_validate({
                "scope": "in_scope", "next_action": action,
                "response_text": "कुछ भी",
            })
            assert brain._direct_reply_text(decision, None, "") == "", action

    async def test_tool_result_blocks_direct_speech(self):
        brain = make_agentic_brain(context=snapshot(), verified=True)
        from shared.orchestration.decision_schema import ConversationDecision

        decision = ConversationDecision.model_validate({
            "scope": "in_scope", "next_action": "answer",
            "response_text": "पेमेंट verify हो गया।",
        })
        assert brain._direct_reply_text(decision, None, "\n\n# Tool result") == ""

    async def test_active_workflow_blocks_the_clean_state_direct_answer(self):
        brain = make_agentic_brain(context=snapshot(), verified=True)
        brain._active_workflow = "ladder"
        from shared.orchestration.decision_schema import ConversationDecision

        decision = ConversationDecision.model_validate({
            "scope": "in_scope", "next_action": "answer",
            "response_text": "जी बताइए।",
        })
        plan = brain._policy.plan_turn("ठीक है", None)
        plan.action = ""
        assert brain._direct_reply_text(decision, plan, "") == ""


# ── 4: timeout falls back immediately, never a sequential classifier ─────────


class _SlowDecisionLLM(_StreamingLLMStub):
    """Stage-A calls hang past the deadline; Stage-B streams normally."""

    def __init__(self):
        super().__init__(tokens=("ठीक", " है।"))
        self.generate_calls = []

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        self.generate_calls.append({"system": system})
        await asyncio.sleep(30)


class TestTimeoutFallback:
    async def test_timeout_uses_deterministic_fallback_without_classifier(self):
        llm = _SlowDecisionLLM()
        config = ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
            published=True, language="hi-IN", languages=["hi-IN"],
            stt={"provider": "sarvam"}, system_prompt="You are Collection Bot.",
            llm={"settings": {"orchestration_timeout_seconds": 0.5,
                              "intent_timeout_seconds": 5.0}},
            intents=[{"name": "billing_query", "samples": ["billing problem"],
                      "route": "", "confidence_threshold": 0.6}],
        )
        brain = ConversationBrain(
            config=config, llm=llm, recorder=_RecorderStub(),
            customer_context=snapshot(),
            finalize_grace=GRACE, complete_endpoint=GRACE,
        )
        brain._notified = []

        async def _push(frame, direction=None):
            pass

        async def _notify(payload):
            brain._notified.append(payload)

        brain.push_frame = _push
        brain._notify_client = _notify
        brain.create_task = lambda coro, name=None: (
            asyncio.get_event_loop().create_task(coro)
        )

        async def _cancel_task(task, timeout=None):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        brain.cancel_task = _cancel_task

        await brain._say(IDENTITY_QUESTION)
        started = time.monotonic()
        await turn(brain, "I mean बोल रहा हूँ।")
        if brain._generation is not None:
            await brain._generation  # the decision deadline elapses inside
        elapsed = time.monotonic() - started

        # Exactly ONE model call was attempted (the timed-out decision); the
        # intent classifier never ran sequentially on top of it.
        assert len(llm.generate_calls) == 1
        fallbacks = events(brain, "orchestration_fallback")
        assert fallbacks and fallbacks[-1]["reason"] == "timeout"
        # Deterministic fallback answered the turn (scripted identity re-ask).
        assert bot_replies(brain)[-1] == canned(
            "collections_identity_reask", "hi-IN"
        ).format(name="Ramesh Kumar")
        # Immediate: decision deadline + scheduling, nowhere near a second
        # sequential timeout window.
        assert elapsed < 2.0, f"fallback took {elapsed:.2f}s"


# ── 5: KB turns keep retrieval even when a response_text exists ──────────────


class _KnowledgeStub:
    class _Source:
        kb_id = "kb1"
        document_id = "doc1"
        document_name = "policy.pdf"
        chunk_id = "ch1"
        page_number = 3
        score = 0.92
        text = "Grace period is 5 days after the due date."

    class _Result:
        answerable = True
        confidence = 0.9
        kb_ids = ["kb1"]
        duration_ms = 12.0

        def __init__(self, sources):
            self.sources = sources

    def __init__(self):
        self.requests = []

    async def search(self, request):
        self.requests.append(request)
        return self._Result([self._Source()])


class TestKnowledgeRouteKeepsItsStages:
    async def test_kb_route_retrieves_and_generates(self):
        llm = _AgenticLLMStub([{
            "scope": "in_scope", "confidence": 0.9,
            "next_action": "answer_from_knowledge",
            "response_text": "The grace period is five days.",
        }])
        knowledge = _KnowledgeStub()
        config = ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
            published=True, language="en-IN", languages=["en-IN"],
            stt={"provider": "sarvam"}, system_prompt="You are Support Bot.",
            kb_ids=["kb1"],
            goal_policy={"role": "support assistant",
                         "goals": [{"id": "g", "description": "answer policy questions"}]},
        )
        brain = ConversationBrain(
            config=config, llm=llm, recorder=_RecorderStub(),
            knowledge_service=knowledge,
            finalize_grace=GRACE, complete_endpoint=GRACE,
        )
        brain._notified = []

        async def _push(frame, direction=None):
            pass

        async def _notify(payload):
            brain._notified.append(payload)

        brain.push_frame = _push
        brain._notify_client = _notify
        brain.create_task = lambda coro, name=None: (
            asyncio.get_event_loop().create_task(coro)
        )

        async def _cancel_task(task, timeout=None):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        brain.cancel_task = _cancel_task

        await turn(brain, "What is the grace period for the premium payment?")
        # Retrieval ran, and the reply was GENERATED under the retrieved
        # context — the co-generated response_text was not spoken directly.
        assert knowledge.requests, "KB retrieval was skipped"
        assert len(stream_calls(llm)) == 1
        system = stream_calls(llm)[-1]["system"]
        assert "Answer using ONLY the reference context" in system
        assert "Grace period is 5 days" in system
        replies = [n["text"] for n in brain._notified if n.get("type") == "bot_text"]
        assert replies and replies[-1] != "The grace period is five days."


if __name__ == "__main__":  # pragma: no cover
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
