"""Outbound-recovery flow: identity gating, payment verification, actions,
completion evaluation and per-turn timing — the fixes for the live failures
observed in conversation review (cv_ebc77d48829b / cv_e6e87a8fe061):

- "I mean बोल रहा हूँ।" was treated as an identity confirmation and the bot
  fell into a generic "आपकी मदद कैसे कर सकता हूँ?" assistant persona;
- "हाँ हाँ नंबर है" was treated as the transaction number itself and the bot
  claimed payment details were recorded and the account would be updated,
  without ever capturing a value or running a verification.

Covers the required scenarios at policy level (state machine, action
validation, completion evaluator) and at brain level (scripted replies, tool
execution, persistence, structured timing).
"""

import pytest

from voice_runtime.call_policy import (
    AWAITING_IDENTITY_CONFIRMATION,
    AWAITING_TRANSACTION_REFERENCE,
    CollectionCallPolicy,
    IDENTITY_UNCLEAR,
    PAYMENT_VERIFICATION_FAILED,
    PAYMENT_VERIFICATION_PENDING,
    PAYMENT_VERIFIED,
    VERIFYING_PAYMENT,
    WRONG_PERSON,
    classify_identity_answer,
    extract_transaction_reference,
    is_valid_transaction_reference,
)
from voice_runtime.turn_metrics import TurnLatencyTracker

from tests.unit.test_brain_collection_policy import (
    _StreamingLLMStub,
    bot_replies,
    make_brain,
    snapshot,
    turn,
    verify_identity,
)
from tests.unit.test_brain_hybrid_intents import (
    ALREADY_PAID_INTENT,
    CLASSIFIED_ALREADY_PAID,
    _ClassifierLLMStub,
    _ToolStub,
    make_hybrid_brain,
)

IDENTITY_QUESTION = "क्या मेरी बात Ramesh Kumar जी से हो रही है?"


def make_policy(**overrides) -> CollectionCallPolicy:
    return CollectionCallPolicy(context=snapshot(**overrides))


def awaiting_identity_policy(**overrides) -> CollectionCallPolicy:
    policy = make_policy(**overrides)
    policy.observe_bot(IDENTITY_QUESTION)
    assert policy.awaiting_identity
    return policy


def claimed_policy(**overrides) -> CollectionCallPolicy:
    policy = make_policy(**overrides)
    policy.verified = True
    policy.observe_user("मैंने कल पेमेंट किया था और यूपीआई से किया था।",
                        "already_paid")
    policy.plan_turn("मैंने कल पेमेंट किया था और यूपीआई से किया था।",
                     "already_paid")
    return policy


# ── 1–5: identity confirmation matrix ────────────────────────────────────────

class TestIdentityAnswers:
    @pytest.mark.parametrize("text,signal", [
        ("हाँ, मैं बोल रहा हूँ।", None),
        ("जी, देवेंद्र बोल रहा हूँ।", None),
        ("Yes, speaking.", None),
        ("This is Devendra.", None),
        ("हाँ जी", "affirm"),
    ])
    def test_clear_confirmation_verifies(self, text, signal):
        policy = awaiting_identity_policy()
        policy.observe_user(text, signal)
        assert policy.verified
        assert not policy.awaiting_identity

    @pytest.mark.parametrize("text,signal", [
        ("I mean बोल रहा हूँ।", "clarify"),   # the live bug utterance
        ("Hello?", None),
        ("क्या?", None),
        ("बोलिए।", None),
        ("70% 70% total edition", None),      # malformed STT / noise
        ("रहा हूँ", None),                    # partial STT output
    ])
    def test_ambiguous_answer_never_verifies(self, text, signal):
        policy = awaiting_identity_policy()
        policy.observe_user(text, signal)
        assert not policy.verified
        assert policy.identity_unclear_count == 1
        assert policy.conversation_state() == IDENTITY_UNCLEAR
        plan = policy.plan_turn(text, signal)
        # Deterministic scripted re-ask — never a free LLM answer.
        assert plan.action == "ask_identity_confirmation"
        assert plan.scripted_reply == (
            "माफ़ कीजिए, क्या मैं Ramesh Kumar जी से बात कर रहा हूँ?"
        )
        assert plan.scripted_final and not plan.close_after_reply
        # The re-ask keeps waiting for the identity answer.
        policy.observe_bot(plan.scripted_reply)
        assert policy.awaiting_identity

    def test_wrong_person_closes_without_disclosure(self):
        policy = awaiting_identity_policy()
        policy.observe_user("नहीं, मेरा नाम तो सुरेश है", "refusal")
        assert policy.wrong_party
        assert policy.conversation_state() == WRONG_PERSON
        instruction = policy.turn_instruction()
        assert "4,850" not in instruction and "XX8976" not in instruction
        complete, reason = policy.evaluate_completion()
        assert complete and reason == "wrong_person_closed"

    def test_repeatedly_unclear_identity_closes_unverified(self):
        policy = awaiting_identity_policy()
        replies = []
        for _ in range(5):
            policy.observe_user("क्या?", None)
            plan = policy.plan_turn("क्या?", None)
            replies.append(plan.scripted_reply)
            if plan.close_after_reply:
                break
            policy.observe_bot(plan.scripted_reply)
        assert plan.close_after_reply
        assert plan.action == "close_unverified"
        # Closed WITHOUT verification and without a single account fact.
        assert not policy.verified
        assert "साझा नहीं कर सकता" in plan.scripted_reply
        assert policy.disposition() == "identity_unverified"
        assert policy.evaluate_completion()[0]

    def test_state_starts_awaiting_identity(self):
        policy = awaiting_identity_policy()
        assert policy.conversation_state() == AWAITING_IDENTITY_CONFIRMATION


# ── 6–7: no generic assistance, no pre-verification disclosure ──────────────

class TestOutboundObjective:
    async def test_ambiguous_identity_gets_scripted_reask_not_llm(self):
        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm)
        await brain._say(IDENTITY_QUESTION)
        await turn(brain, "I mean बोल रहा हूँ।")
        # No LLM reply ran at all — the re-ask is deterministic, so the model
        # cannot confirm identity on the customer's behalf or fall into a
        # generic assistant persona.
        assert llm.calls == []
        replies = bot_replies(brain)
        assert replies[-1] == (
            "माफ़ कीजिए, क्या मैं Ramesh Kumar जी से बात कर रहा हूँ?"
        )
        assert not brain._policy.verified

    def test_prompt_prohibits_generic_assistance_and_identity_invention(self):
        policy = awaiting_identity_policy()
        instruction = policy.turn_instruction()
        assert "outbound_recovery" in instruction
        assert "How may I help you?" in instruction  # named as prohibited
        assert "generic_assistance_response" in instruction
        assert "Never claim or assume you are speaking with the customer" \
            in instruction

    def test_no_account_details_before_confirmation(self):
        policy = awaiting_identity_policy()
        policy.observe_user("Hello?", None)
        instruction = policy.turn_instruction()
        assert "NOT confirmed" in instruction
        for secret in ("4,850", "XX8976", "2026-07-23", "5,120"):
            assert secret not in instruction
        assert "disclose_account_details" in policy.prohibited_actions()


# ── 8–12: payment claim → transaction reference capture ─────────────────────

class TestTransactionReferenceCapture:
    def test_claim_without_reference_asks_for_the_number(self):
        policy = claimed_policy()
        assert policy.awaiting_reference
        assert policy.conversation_state() == AWAITING_TRANSACTION_REFERENCE
        assert policy.payment_method_claimed == "UPI"
        assert policy.payment_date_claimed  # "कल" recorded as claimed date

    def test_haan_number_hai_is_not_the_number(self):
        policy = claimed_policy()
        policy.observe_user("हाँ हाँ नंबर है।", "affirm")
        # NOT captured, NOT recorded — the bot must ask for the value.
        assert policy.transaction_reference is None
        assert policy.reference_attempts == 1
        plan = policy.plan_turn("हाँ हाँ नंबर है।", "affirm")
        assert plan.action == "clarify_transaction_reference"
        assert "ट्रांजैक्शन नंबर" in plan.scripted_reply
        assert "नोट" not in plan.scripted_reply  # no false "recorded" claim
        assert not plan.close_after_reply

    def test_actual_number_is_captured_normalized_and_verified(self):
        policy = claimed_policy()
        policy.observe_user("नंबर है 1234 5678 9012", None)
        assert policy.transaction_reference == "123456789012"
        assert policy.conversation_state() == VERIFYING_PAYMENT
        plan = policy.plan_turn("नंबर है 1234 5678 9012", None)
        # Captured ≠ saved ≠ verified: the next step is a REAL verification.
        assert plan.action == "verify_payment"
        assert plan.verify_reference == "123456789012"
        assert not plan.close_after_reply

    def test_devanagari_and_spoken_digits_normalize(self):
        assert extract_transaction_reference("४५६७८९१२३४५६") == "456789123456"
        assert extract_transaction_reference(
            "ek do teen char paanch chhe saat"
        ) == "1234567"

    def test_reference_format_validation(self):
        assert is_valid_transaction_reference("123456789012")
        assert is_valid_transaction_reference("AXIS123456789")
        assert not is_valid_transaction_reference("12345")        # too short
        assert not is_valid_transaction_reference("12 34 56")     # separators
        assert not is_valid_transaction_reference("")
        assert not is_valid_transaction_reference(None)
        assert extract_transaction_reference("सिर्फ 12345 है") is None

    def test_customer_without_reference_closes_honestly(self):
        policy = claimed_policy()
        policy.observe_user("नंबर याद नहीं है भाई", None)
        assert policy.reference_unavailable
        plan = policy.plan_turn("नंबर याद नहीं है भाई", None)
        assert plan.close_after_reply
        assert "जाँच" in plan.scripted_reply
        assert "पुष्टि हो चुकी" not in plan.scripted_reply
        complete, reason = policy.evaluate_completion()
        assert complete and reason == "customer_could_not_provide_reference"

    def test_repeated_unusable_answers_close_with_follow_up(self):
        policy = claimed_policy()
        for _ in range(3):
            policy.observe_user("हाँ है ना", "affirm")
            plan = policy.plan_turn("हाँ है ना", "affirm")
        assert plan.close_after_reply
        assert policy.evaluate_completion()[0]
        assert policy.payment_record()["reference_unavailable"]


# ── 13–16: verification outcomes ─────────────────────────────────────────────

class TestVerificationOutcomes:
    def _captured(self) -> CollectionCallPolicy:
        policy = claimed_policy()
        policy.observe_user("UTR 123456789012 hai", None)
        assert policy.transaction_reference == "123456789012"
        return policy

    def test_verified_outcome(self):
        policy = self._captured()
        policy.record_payment_verification(
            "completed", "check_payment_status", for_reference=True
        )
        assert policy.conversation_state() == PAYMENT_VERIFIED
        plan = policy.plan_turn("UTR 123456789012 hai", None)
        assert plan.action == "mark_payment_verified"
        assert "पुष्टि हो चुकी है" in plan.scripted_reply
        assert plan.close_after_reply
        assert policy.evaluate_completion() == (True, "payment_verified")

    def test_pending_outcome_never_claims_verified(self):
        policy = self._captured()
        policy.record_payment_verification(
            "processing", "check_payment_status", for_reference=True
        )
        assert policy.conversation_state() == PAYMENT_VERIFICATION_PENDING
        plan = policy.plan_turn("UTR 123456789012 hai", None)
        assert "प्रोसेसिंग में है" in plan.scripted_reply
        assert "पुष्टि हो चुकी" not in plan.scripted_reply
        assert plan.close_after_reply

    def test_failed_outcome_records_follow_up(self):
        policy = self._captured()
        policy.record_payment_verification(
            "not_found", "check_payment_status", for_reference=True
        )
        assert policy.conversation_state() == PAYMENT_VERIFICATION_FAILED
        plan = policy.plan_turn("UTR 123456789012 hai", None)
        assert "पुष्टि नहीं हो" in plan.scripted_reply
        assert plan.close_after_reply
        assert policy.evaluate_completion() == (
            True, "reference_captured_follow_up_recorded"
        )

    def test_no_tool_means_honestly_pending(self):
        policy = self._captured()
        policy.record_payment_verification(None, None, for_reference=True)
        assert policy.verification_outcome == "unverified"
        plan = policy.plan_turn("UTR 123456789012 hai", None)
        assert "पुष्टि अभी" in plan.scripted_reply       # pending…
        assert "पुष्टि हो चुकी" not in plan.scripted_reply  # …never done
        # Digits are read back one by one for confirmation.
        assert "1 2 3 4 5 6 7 8 9 0 1 2" in plan.scripted_reply
        record = policy.payment_record()
        assert record["verification_status"] == "unverified"
        assert record["transaction_reference"] == "123456789012"

    def test_unknown_status_maps_to_pending_not_verified(self):
        policy = self._captured()
        policy.record_payment_verification(
            "weird_status", "check_payment_status", for_reference=True
        )
        assert policy.verification_outcome == "pending"


# ── 17–18: completion evaluator and action validation ───────────────────────

class TestActionValidationAndCompletion:
    def test_invalid_actions_rejected_while_awaiting_reference(self):
        policy = claimed_policy()
        for action in ("mark_payment_verified", "mark_payment_details_recorded",
                       "complete_call", "promise_account_update",
                       "generic_assistance_response"):
            assert not policy.validate_action(action), action
        for action in ("ask_transaction_reference",
                       "capture_transaction_reference",
                       "clarify_transaction_reference"):
            assert policy.validate_action(action), action

    def test_mark_verified_requires_tool_outcome(self):
        policy = claimed_policy()
        policy.observe_user("UTR 123456789012", None)
        assert not policy.validate_action("mark_payment_verified")
        policy.record_payment_verification(
            "completed", "check_payment_status", for_reference=True
        )
        assert policy.validate_action("mark_payment_verified")

    def test_generic_assistance_never_allowed(self):
        policy = make_policy()
        policy.verified = True
        assert not policy.validate_action("generic_assistance_response")

    def test_completion_rejected_while_reference_missing(self):
        policy = claimed_policy()
        complete, reason = policy.evaluate_completion()
        assert not complete
        assert reason == "transaction_reference_not_captured"
        assert not policy.validate_action("complete_call")

    def test_completion_rejected_before_identity_confirmation(self):
        policy = awaiting_identity_policy()
        complete, reason = policy.evaluate_completion()
        assert not complete and reason == "identity_not_confirmed"

    def test_missing_required_fields_reported(self):
        policy = awaiting_identity_policy()
        assert "identity_confirmation" in policy.missing_required_fields()
        policy = claimed_policy()
        assert "transaction_reference" in policy.missing_required_fields()
        policy.observe_user("UTR 123456789012", None)
        assert "payment_verification_result" in policy.missing_required_fields()


# ── brain level: capture → tool → outcome → persistence ─────────────────────

class TestBrainPaymentVerification:
    async def _claimed_brain(self, tool):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT], tool=tool)
        await verify_identity(brain)
        await turn(brain, "मैंने कल पेमेंट किया था और यूपीआई से किया था।")
        assert brain._policy.awaiting_reference
        assert "ट्रांजैक्शन" in bot_replies(brain)[-1]
        return brain

    async def test_reference_turn_runs_verification_tool(self):
        # The account-level check on the claim turn is inconclusive; the
        # reference verification afterwards confirms the payment.
        tool = _ToolStub({"payment_status": "pending"})
        brain = await self._claimed_brain(tool)
        tool.calls.clear()
        tool._payload = {"payment_status": "completed"}
        await turn(brain, "नंबर है 1234 5678 9012")
        # The verification tool ran with the CAPTURED normalized reference…
        verify_calls = [c for c in tool.calls
                        if c["args"].get("transaction_reference")]
        assert verify_calls
        assert verify_calls[-1]["args"]["transaction_reference"] == "123456789012"
        # …the outcome was spoken scripted, and the call closed approved.
        assert "पुष्टि हो चुकी है" in bot_replies(brain)[-1]
        assert brain._closing
        events = dict()
        for kind, data in brain._recorder.events:
            events.setdefault(kind, []).append(data)
        assert events["transaction_reference_captured"][-1]["reference"] == \
            "123456789012"
        assert events["payment_verification"][-1]["outcome"] == "verified"
        assert events["call_completed_by_policy"][-1]["completion_reason"] == \
            "payment_verified"

    async def test_haan_number_hai_never_closes_or_records(self):
        tool = _ToolStub({"payment_status": "pending"})
        brain = await self._claimed_brain(tool)
        await turn(brain, "हाँ हाँ नंबर है।")
        reply = bot_replies(brain)[-1]
        assert "ट्रांजैक्शन नंबर" in reply
        # No false claims, no close, nothing recorded as captured.
        assert "नोट" not in reply and "वेरिफाई" not in reply
        assert not brain._closing
        assert brain._policy.transaction_reference is None
        kinds = [k for k, _ in brain._recorder.events]
        assert "transaction_reference_captured" not in kinds
        assert "call_completed_by_policy" not in kinds

    async def test_captured_reference_persisted_in_call_state(self):
        tool = _ToolStub({"payment_status": "pending"})
        brain = await self._claimed_brain(tool)
        await turn(brain, "UTR number 123456789012")
        await brain.cleanup()
        record = brain._recorder.call_state["payment_verification"]
        assert record["payment_claimed"] is True
        assert record["transaction_reference"] == "123456789012"
        assert record["transaction_reference_confirmed"] is True
        assert record["payment_method"] == "UPI"
        assert record["verification_status"] == "pending"
        assert record["verification_source"] == "check_payment_status"

    async def test_no_tool_closes_with_pending_verification(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        brain = make_hybrid_brain(context=snapshot(), llm=llm, intents=[])
        await verify_identity(brain)
        await turn(brain, "मैंने कल पेमेंट किया था यूपीआई से")
        await turn(brain, "नंबर है 123456789012")
        reply = bot_replies(brain)[-1]
        assert "पुष्टि अभी" in reply
        assert "पुष्टि हो चुकी" not in reply
        assert brain._policy.verification_outcome == "unverified"

    async def test_deterministic_turns_skip_llm_classification(self):
        llm = _ClassifierLLMStub(CLASSIFIED_ALREADY_PAID)
        tool = _ToolStub({"payment_status": "pending"})
        brain = make_hybrid_brain(context=snapshot(), llm=llm,
                                  intents=[ALREADY_PAID_INTENT], tool=tool)
        await verify_identity(brain)
        await turn(brain, "मैंने कल पेमेंट किया था और यूपीआई से किया था।")
        classify_calls = len(llm.generate_calls)
        # The reference answer is consumed deterministically: no LLM
        # classification hop (~1.2–1.8s measured) and no LLM reply.
        await turn(brain, "हाँ हाँ नंबर है।")
        assert len(llm.generate_calls) == classify_calls


# ── 20: per-turn structured timing ───────────────────────────────────────────

class TestTurnTiming:
    def test_structured_timing_and_slowest_stage(self):
        tracker = TurnLatencyTracker(session_id="vs-1")
        tracker.conversation_id = "cv_test123"
        tracker.turn_id = 4
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.mark_final()
        tracker.mark_dispatched()
        tracker.mark_classified()
        tracker.mark_tool_start()
        tracker.mark_tool_done()
        tracker.mark_llm_request()
        tracker.mark_llm_first_token()
        tracker.mark_llm_completed()
        tracker.mark_tts_request()
        tracker.mark_tts_first_byte()
        tracker.mark_bot_started_speaking()
        timing = tracker.structured()
        assert timing["conversation_id"] == "cv_test123"
        assert timing["turn_id"] == 4
        for key in ("user_speech_end_at", "stt_final_at", "llm_started_at",
                    "llm_completed_at", "tool_started_at", "tool_completed_at",
                    "tts_started_at", "first_audio_generated_at",
                    "first_audio_sent_at"):
            assert timing[key] > 0, key
        assert "total_response_latency_ms" in timing
        assert timing["slowest_stage"] in (
            "stt_final", "endpoint", "classify", "tool", "llm_ttft",
            "tts_queue", "tts_ttfb", "playout",
        )

    def test_slowest_stage_attribution(self):
        tracker = TurnLatencyTracker()
        spans = {"endpoint": 620.0, "classify": 1844.0, "llm_ttft": 1126.0,
                 "tts_ttfb": 152.0, "response": 4027.0,
                 "llm_first_token": 2972.0}
        # Composite spans never win: the slowest SERIAL stage is named.
        assert tracker.slowest_stage(spans) == "classify"

    async def test_turn_timing_event_emitted_with_turn_ids(self):
        from pipecat.frames.frames import BotStartedSpeakingFrame
        from pipecat.processors.frame_processor import FrameDirection

        llm = _StreamingLLMStub()
        brain = make_brain(context=snapshot(), llm=llm)
        await verify_identity(brain)
        await brain.process_frame(
            BotStartedSpeakingFrame(), FrameDirection.DOWNSTREAM
        )
        timings = [d for k, d in brain._recorder.events if k == "turn_timing"]
        assert timings
        assert timings[-1]["turn_id"] == 1
        assert "slowest_stage" in timings[-1]
