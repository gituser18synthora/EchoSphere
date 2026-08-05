"""CollectionCallPolicy — the conversation-state engine for collection calls.

Covers the required behaviors end to end at policy level, in Hindi, English
and Hinglish: identity verification and gating of account facts, wrong party /
name mismatch, payment-already-made claims, account disputes, complaints,
callback requests, hardship, escalation confirmation, masking, dispositions
and the call-state write-back payload.
"""

from shared.customer_context import CustomerContextSnapshot
from voice_runtime.call_policy import (
    ACCOUNT_DISPUTE,
    CLOSING,
    CollectionCallPolicy,
    IDENTITY_VERIFICATION,
    PAYMENT_ALREADY_MADE,
    WRONG_PARTY,
)


def snapshot(**overrides) -> CustomerContextSnapshot:
    base = dict(
        context_id="cctx_test1", tenant_id="tn-x", bot_id="bot-x",
        customer_name="Ramesh Kumar", dcs_name="eDAS Recoveries",
        lender_name="eDAS Finance", loan_account_masked="XX8976",
        phone_masked="XXXXXX0001", phone_last4="0001",
        preferred_language="hi-IN", overdue_amount=4850.0,
        total_outstanding=5120.0, minimum_payable=2000.0, days_overdue=12,
        due_date="2026-07-23", partial_payment_allowed=True,
        payment_methods=("UPI", "Debit Card"),
        secure_payment_link_available=True,
        grievance_contact="grievance@example (1800-000-111)",
        payment_status="pending", customer_verified=False,
        recording_notice_required=True,
    )
    base.update(overrides)
    return CustomerContextSnapshot(**base)


def make_policy(*, verified: bool = False, **overrides) -> CollectionCallPolicy:
    policy = CollectionCallPolicy(context=snapshot(**overrides))
    # Identity is per-call: tests that need a verified customer mark the
    # policy explicitly, mirroring an in-call confirmation.
    policy.verified = verified
    return policy


IDENTITY_QUESTION = "क्या मेरी बात Ramesh Kumar से हो रही है?"


class TestIdentityVerification:
    def test_starts_unverified_and_asks_identity_first(self):
        policy = make_policy()
        assert policy.phase == IDENTITY_VERIFICATION
        plan = policy.plan_turn("हेलो", None)
        assert plan.force_llm  # no scripted pitch before verification
        assert "Ramesh Kumar" in plan.instruction
        assert "NOT confirmed" in plan.instruction

    def test_amounts_hidden_until_verified(self):
        policy = make_policy()
        instruction = policy.turn_instruction()
        assert "4,850" not in instruction
        assert "XX8976" not in instruction
        assert "due_date" not in instruction and "2026-07-23" not in instruction

    def test_affirm_to_identity_question_verifies(self):
        policy = make_policy()
        policy.observe_bot(IDENTITY_QUESTION)
        assert policy.awaiting_identity
        policy.observe_user("हाँ जी, बोल रहा हूँ", "affirm")
        assert policy.verified
        instruction = policy.turn_instruction()
        assert "₹4,850" in instruction
        assert "XX8976" in instruction
        assert "Days overdue: 12" in instruction
        assert "2026-07-23" in instruction

    def test_placeholder_amounts_gated_by_verification(self):
        policy = make_policy()
        assert "outstanding_amount" not in policy.placeholder_values()
        policy.observe_bot(IDENTITY_QUESTION)
        policy.observe_user("yes speaking", "affirm")
        assert policy.placeholder_values()["customer_name"] == "Ramesh Kumar"
        assert "outstanding_amount" in policy.placeholder_values()

    def test_full_phone_never_in_prompt(self):
        policy = make_policy()
        policy.observe_bot(IDENTITY_QUESTION)
        policy.observe_user("haan", "affirm")
        instruction = policy.turn_instruction()
        assert "9000000001" not in instruction  # never a full number
        assert "0 0 0 1" in instruction  # last four, digit by digit


class TestWrongParty:
    def test_name_mismatch_blocks_and_closes(self):
        policy = make_policy()
        policy.observe_bot(IDENTITY_QUESTION)
        policy.observe_user("नहीं, मेरा नाम तो सुरेश है", "refusal")
        assert policy.wrong_party and policy.identity_mismatch
        assert policy.phase == WRONG_PARTY
        plan = policy.plan_turn("नहीं, मेरा नाम तो सुरेश है", "refusal")
        assert plan.force_llm and plan.close_after_reply
        assert "do NOT reveal any account information" in plan.instruction
        assert policy.disposition() == "identity_mismatch"

    def test_wrong_number_english(self):
        policy = make_policy()
        policy.observe_user("You have the wrong number, I don't know any Ramesh",
                            "wrong_person")
        assert policy.wrong_party
        assert policy.disposition() == "wrong_number"
        # Facts must vanish even if identity had been confirmed earlier.
        policy.verified = True
        assert "₹4,850" not in policy.turn_instruction()

    def test_wrong_party_never_pushes_payment(self):
        policy = make_policy()
        policy.observe_user("galat number hai bhai", "wrong_person")
        plan = policy.plan_turn("galat number hai bhai", "wrong_person")
        assert "no payment" in plan.instruction.lower() or \
               "OPEN ISSUES" in plan.instruction


class TestPaymentAlreadyMade:
    def test_claim_recorded_then_one_followup_then_close(self):
        policy = make_policy(verified=True)
        policy.observe_user("मैंने पेमेंट कर दी है", "already_paid")
        assert policy.payment_claimed and policy.phase == PAYMENT_ALREADY_MADE
        plan = policy.plan_turn("मैंने पेमेंट कर दी है", "already_paid")
        assert plan.force_llm and not plan.close_after_reply
        assert "when did they pay" in plan.instruction

        policy.observe_user("UTR number 123456789 hai", None)
        assert policy.payment_claim_stage == 2
        plan = policy.plan_turn("UTR number 123456789 hai", None)
        assert plan.close_after_reply
        assert "team will verify" in plan.instruction
        assert policy.disposition() == "payment_claimed"

    def test_never_claims_live_verification(self):
        policy = make_policy(verified=True)
        policy.observe_user("already paid last week", "already_paid")
        plan = policy.plan_turn("already paid last week", "already_paid")
        assert "cannot check it on this call" in policy.turn_instruction() or \
               "never say you checked" in plan.instruction.lower()

    def test_claim_is_unverified_not_fact(self):
        policy = make_policy(verified=True)
        policy.observe_user("पैसा कट गया मेरा", "already_paid")
        instruction = policy.turn_instruction()
        assert "unverified claims" in instruction
        assert "पैसा कट गया मेरा" in instruction


class TestAccountDispute:
    def test_hindi_loan_denial_is_dispute_not_wrong_number(self):
        policy = make_policy(verified=True)
        policy.observe_user("मैंने कोई लोन लिया ही नहीं है", "wrong_person")
        assert policy.dispute_raised and not policy.wrong_party
        assert policy.phase == ACCOUNT_DISPUTE
        assert policy.disposition() == "account_disputed"

    def test_dispute_blocks_payment_persuasion(self):
        policy = make_policy(verified=True)
        policy.observe_user("yeh amount galat hai, dispute karna hai", None)
        assert policy.dispute_raised
        plan = policy.plan_turn("yeh amount galat hai, dispute karna hai", None)
        assert plan.force_llm
        assert "RECORDED" in plan.instruction
        assert "Do not push payment" in plan.instruction
        assert "OPEN ISSUES" in plan.instruction

    def test_dispute_write_back(self):
        policy = make_policy(verified=True)
        policy.observe_user("मैंने लोन नहीं लिया", "wrong_person")
        updates = policy.call_state_updates()
        assert updates["account_disputed"] is True
        assert updates["payment_status"] == "disputed"
        assert updates["last_disposition"] == "account_disputed"


class TestComplaintAndCallback:
    def test_conversation_complaint_recorded(self):
        policy = make_policy(verified=True)
        policy.observe_user("आप मेरी बात सुन ही नहीं रहे, बार बार वही बोल रहे हो",
                            "complaint")
        assert policy.complaint_raised
        plan = policy.plan_turn("आप मेरी बात सुन ही नहीं रहे", "complaint")
        assert plan.force_llm
        assert policy.call_state_updates()["complaint_pending"] is True

    def test_callback_with_time_confirms_and_closes(self):
        policy = make_policy(verified=True)
        policy.observe_user("main abhi busy hoon, shaam ko call karna", "callback")
        assert policy.callback_requested and policy.callback_time_known
        plan = policy.plan_turn("main abhi busy hoon, shaam ko call karna", "callback")
        assert plan.close_after_reply
        assert policy.disposition() == "callback_requested"

    def test_callback_without_time_asks_once(self):
        policy = make_policy(verified=True)
        policy.observe_user("I am in a meeting", "callback")
        plan = policy.plan_turn("I am in a meeting", "callback")
        assert plan.force_llm and not plan.close_after_reply
        assert "what time" in plan.instruction
        policy.observe_user("कल सुबह", None)
        plan = policy.plan_turn("कल सुबह", None)
        assert plan.close_after_reply


class TestEscalation:
    def test_agent_request_hands_off(self):
        policy = make_policy()
        policy.observe_user("mujhe agent se baat karni hai", "agent_request")
        plan = policy.plan_turn("mujhe agent se baat karni hai", "agent_request")
        assert plan.handoff
        assert policy.disposition() == "escalated"

    def test_affirm_to_agent_offer_hands_off(self):
        # The live-call bug: bot offers "agent se connect kar dun?", customer
        # says "जी", and the old flow re-asked a stale workflow question.
        policy = make_policy(verified=True)
        policy.observe_bot("क्या मैं आपको हमारे agent से connect कर दूँ?")
        policy.observe_user("जी।", "affirm")
        plan = policy.plan_turn("जी।", "affirm")
        assert plan.handoff


class TestGeneralBehavior:
    def test_question_answered_before_flow(self):
        policy = make_policy(verified=True)
        policy.observe_user("कितना अमाउंट बाकी है?", "question")
        plan = policy.plan_turn("कितना अमाउंट बाकी है?", "question")
        assert plan.force_llm
        assert "₹4,850" in plan.instruction  # answerable from verified facts

    def test_unknown_facts_are_declared_missing(self):
        policy = make_policy(verified=True, overdue_amount=None,
                             due_date=None, payment_methods=())
        instruction = policy.turn_instruction()
        assert "Not available on this call" in instruction
        assert "overdue amount" in instruction
        assert "NEVER guess" in instruction

    def test_no_context_never_invents(self):
        policy = CollectionCallPolicy(context=None)
        instruction = policy.turn_instruction()
        assert "No customer record is available" in instruction
        assert "Never guess or invent" in instruction

    def test_dispute_blocks_even_without_context(self):
        policy = CollectionCallPolicy(context=None)
        policy.observe_user("I never took this loan, this is a fraud", None)
        assert policy.dispute_raised
        assert policy.plan_turn("I never took this loan", None).force_llm

    def test_recording_notice_tracked(self):
        policy = make_policy()
        assert "Recording notice pending" in policy.turn_instruction()
        policy.observe_bot("यह कॉल quality के लिए record हो सकती है।")
        assert "Recording notice pending" not in policy.turn_instruction()

    def test_hardship_and_refusal_dispositions(self):
        policy = make_policy(verified=True)
        policy.observe_user("paise nahi hain, naukri chali gayi", "hardship")
        assert policy.disposition() == "hardship"
        policy.observe_user("नहीं करूंगा", "refusal")
        assert policy.disposition() == "hardship"  # hardship outranks refusal

    def test_promise_to_pay_disposition(self):
        policy = make_policy(verified=True)
        policy.observe_user("main kal payment kar dunga", "payment_intent")
        assert policy.promise_to_pay
        assert policy.disposition() == "promise_to_pay"

    def test_one_question_rule_always_present(self):
        policy = make_policy()
        assert "at most ONE question" in policy.turn_instruction()

    def test_never_claims_backend_check(self):
        instruction = make_policy().turn_instruction()
        assert "never say you checked" in instruction

    def test_interruption_written_back(self):
        policy = make_policy(verified=True)
        policy.interruption_detected = True
        updates = policy.call_state_updates()
        assert updates["interruption_detected"] is True
        assert updates["is_final_transcript"] is True

    def test_dispute_state_from_context_row(self):
        # A context already marked disputed starts the call blocked.
        policy = make_policy(account_disputed=True, customer_verified=True)
        assert policy.dispute_raised
        assert policy.plan_turn("hello", None).force_llm

    def test_closing_phase_after_callback_confirmed(self):
        policy = make_policy(verified=True)
        policy.observe_user("बाद में कॉल करना, शाम को", "callback")
        policy.plan_turn("बाद में कॉल करना, शाम को", "callback")
        assert policy.phase == CLOSING


class TestScriptedOpening:
    """The identity-confirmation turn is answered without an LLM round trip.

    Its content follows entirely from verified facts, so generating it buys
    nothing and costs ~1s on the one turn where the caller has just said a
    single word. The guard is that it only fires when nothing about the call
    requires judgement.
    """

    def _confirm(self, policy):
        policy.observe_bot(IDENTITY_QUESTION)
        policy.observe_user("हाँ जी बोल रहा हूँ", "affirm")
        return policy.plan_turn("हाँ जी बोल रहा हूँ", "affirm")

    def test_states_amount_days_and_asks_for_payment(self):
        policy = make_policy(recording_notice_required=False)
        reply = self._confirm(policy).scripted_reply

        assert reply, "the opener should be scripted"
        assert "चार हज़ार आठ सौ पचास रुपये" in reply   # amount as words
        assert "बारह" in reply                          # days overdue as words
        assert "?" in reply                             # ends on a direct ask
        # Digits never reach the TTS — that is what mispronounces.
        assert "4850" not in reply and "4,850" not in reply

    def test_follows_the_conversation_language(self):
        # preferred_language unset, so the call's language decides (a stored
        # preference outranks it — that rule is covered elsewhere).
        policy = CollectionCallPolicy(
            context=snapshot(recording_notice_required=False,
                             preferred_language=None),
            language="en-IN",
        )
        reply = self._confirm(policy).scripted_reply

        assert "4,850 rupees" in reply
        assert "12 days" in reply

    def test_amount_only_when_the_day_count_is_unknown(self):
        policy = make_policy(recording_notice_required=False,
                             days_overdue=None, due_date=None)
        reply = self._confirm(policy).scripted_reply

        assert reply
        assert "रुपये" in reply
        assert "दिनों" not in reply, "invented a day count"

    def test_no_script_without_a_verified_amount(self):
        policy = make_policy(recording_notice_required=False, overdue_amount=None)
        assert self._confirm(policy).scripted_reply == ""

    def test_no_script_while_the_recording_notice_is_pending(self):
        policy = make_policy(recording_notice_required=True)
        assert self._confirm(policy).scripted_reply == ""

    def test_no_script_when_a_promise_was_missed(self):
        # Raising a missed promise is a judgement call, not a template.
        policy = make_policy(recording_notice_required=False,
                             previous_promise_pending=True,
                             previous_promise_date="2026-08-03")
        assert self._confirm(policy).scripted_reply == ""

    def test_no_script_for_a_disputed_account(self):
        policy = make_policy(recording_notice_required=False,
                             account_disputed=True)
        assert self._confirm(policy).scripted_reply == ""

    def test_never_scripts_before_identity_is_confirmed(self):
        policy = make_policy(recording_notice_required=False)
        plan = policy.plan_turn("कौन बोल रहा है?", "question")
        assert plan.scripted_reply == ""
