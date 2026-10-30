"""Latest-intent priority over the recovery ladder + code-owned ladder state.

The transcript failure class: the customer clearly agrees to pay (or asks an
amount question, or reports a medical emergency) and the bot keeps running
the refusal-recovery ladder. The ladder now lives in CollectionCallPolicy —
one rung per genuine refusal, never repeated, always outranked by a clearer
intent — and the prompt no longer scans history for rung markers.
"""

from voice_runtime.call_policy import (
    CollectionCallPolicy,
    amount_query_type,
    detect_amount_query,
    detect_medical_emergency,
)

from tests.unit.test_brain_collection_policy import snapshot


def verified_policy(**overrides) -> CollectionCallPolicy:
    policy = CollectionCallPolicy(context=snapshot(**overrides))
    policy.verified = True
    policy.recording_notice_given = True
    return policy


def observe_and_plan(policy, text, signal):
    policy.observe_user(text, signal)
    return policy.plan_turn(text, signal)


class TestLadderState:
    def test_refusal_advances_one_rung_per_turn(self):
        policy = verified_policy(
            active_offers=({"label": "Bhim UPI cashback", "terms": "up to 2%"},),
        )
        plan = observe_and_plan(policy, "अभी नहीं करूंगा", "refusal")
        assert plan.action == "recovery_consequence"
        assert policy.consequence_used and not policy.offer_used

        plan = observe_and_plan(policy, "paise nahi hain bhai", "hardship")
        assert plan.action == "recovery_offer"
        assert policy.offer_used

        plan = observe_and_plan(policy, "नहीं होगा", "refusal")
        assert plan.action == "recovery_partial"
        assert policy.partial_used

        plan = observe_and_plan(policy, "nahi karunga", "refusal")
        assert plan.action == "recovery_self_resolution"

        plan = observe_and_plan(policy, "नहीं", "refusal")
        assert plan.action == "recovery_final_options"

        plan = observe_and_plan(policy, "नहीं चाहिए", "refusal")
        assert plan.action == "recovery_closed"
        assert plan.close_after_reply

    def test_unavailable_rungs_are_skipped(self):
        # No offers configured and partial payment not allowed: the ladder is
        # consequence → self-resolution.
        policy = verified_policy(partial_payment_allowed=False)
        observe_and_plan(policy, "नहीं करूंगा", "refusal")
        plan = observe_and_plan(policy, "नहीं होगा", "refusal")
        assert plan.action == "recovery_self_resolution"

    def test_used_rungs_listed_in_turn_instruction(self):
        policy = verified_policy()
        observe_and_plan(policy, "नहीं करूंगा", "refusal")
        instruction = policy.turn_instruction()
        assert "Recovery pitches already made" in instruction
        assert "consequence" in instruction


class TestPaymentReadyPriority:
    def test_agreement_after_refusal_leaves_the_ladder(self):
        policy = verified_policy()
        observe_and_plan(policy, "अभी नहीं करूंगा", "refusal")
        assert policy.consequence_used

        plan = observe_and_plan(policy, "ठीक है, payment कर दूंगा", "payment_intent")
        assert plan.action == "confirm_commitment"
        assert policy.promise_to_pay
        # The next rung must NOT have been consumed by the agreement turn.
        assert not policy.offer_used and not policy.partial_used
        instruction = policy.turn_instruction()
        assert "The customer agreed to pay" in instruction

    def test_how_do_i_pay_is_payment_flow_not_recovery(self):
        policy = verified_policy()
        plan = observe_and_plan(policy, "payment kaise karna hai mujhe",
                                "payment_intent")
        assert plan.action == "confirm_commitment"
        assert not policy.consequence_used

    def test_commitment_flow_asks_date_then_method_then_closes(self):
        policy = verified_policy()
        plan = observe_and_plan(policy, "main payment kar dunga", "payment_intent")
        assert plan.action == "confirm_commitment"
        assert "exact date" in policy.turn_instruction()

        plan = observe_and_plan(policy, "kal pakka kar dunga", "payment_intent")
        assert policy.promise_date_known
        assert "payment methods" in policy.turn_instruction()

        plan = observe_and_plan(policy, "UPI se kar dunga", "payment_intent")
        assert policy.payment_method_claimed == "UPI"
        assert plan.close_after_reply
        assert policy.evaluate_completion()[0]

    def test_proposed_amount_is_captured_from_spoken_words(self):
        policy = verified_policy()
        observe_and_plan(policy, "main do hazaar rupaye de dunga", "payment_intent")
        assert policy.proposed_amount == "2000"


class TestAmountQueryPriority:
    def test_amount_question_detected_and_typed(self):
        assert detect_amount_query("मुझे कुल कितना payment करना है?")
        assert amount_query_type("total kitna hai") == "total"
        assert amount_query_type("minimum kitna dena hoga") == "minimum"
        assert amount_query_type("कितना payment करना है?") == "ambiguous"
        assert detect_amount_query("how much do I need to pay")

    def test_amount_question_outranks_reference_ask(self):
        policy = verified_policy()
        observe_and_plan(policy, "payment to maine kar diya tha", "already_paid")
        assert policy.awaiting_reference
        plan = observe_and_plan(policy, "waise total kitna amount hai?", "question")
        assert plan.action == "answer_amount_question"
        # The reference is still awaited afterwards.
        assert policy.awaiting_reference
        assert policy.transaction_reference is None

    def test_amount_question_never_advances_the_ladder(self):
        policy = verified_policy()
        observe_and_plan(policy, "नहीं करूंगा अभी", "refusal")
        plan = observe_and_plan(policy, "total kitna hai?", "question")
        assert plan.action == "answer_amount_question"
        assert not policy.offer_used and not policy.partial_used

    def test_account_tool_triggers_real_refresh_once(self):
        policy = verified_policy()
        policy.account_tool_available = True
        plan = observe_and_plan(policy, "overdue amount kitna hai", "question")
        assert plan.refresh_account
        policy.record_account_refresh(
            {"totalOutstanding": "5400", "minimum_due": 1200}, "account_status"
        )
        assert policy.account_refreshed
        assert policy.context.total_outstanding == 5400.0
        assert policy.context.minimum_payable == 1200.0
        # Re-planning the same turn does not loop the tool.
        plan = policy.plan_turn("overdue amount kitna hai", "question")
        assert not plan.refresh_account
        assert "refreshed from the system THIS turn" in policy.turn_instruction()

    def test_failed_refresh_is_honest(self):
        policy = verified_policy()
        policy.account_tool_available = True
        observe_and_plan(policy, "total kitna hai?", "question")
        policy.record_account_refresh(None, "account_status")
        assert policy.account_refreshed
        # Loaded facts unchanged; instruction does not claim a fresh check.
        assert policy.context.total_outstanding == 5120.0
        assert "refreshed from the system" not in policy.turn_instruction()

    def test_successful_tool_without_amount_fields_is_not_a_figure_refresh(self):
        policy = verified_policy()
        policy.account_tool_available = True
        observe_and_plan(policy, "total kitna hai?", "question")
        policy.record_account_refresh({"status": "ok"}, "account_status")
        assert policy.account_refreshed
        assert not policy.account_refresh_succeeded
        assert policy.context.total_outstanding == 5120.0
        assert "refreshed from the system" not in policy.turn_instruction()


class TestMedicalEmergency:
    def test_detector(self):
        assert detect_medical_emergency("मेरी मम्मी hospital में admit हैं")
        assert detect_medical_emergency("papa ka accident ho gaya hai")
        assert not detect_medical_emergency("paise nahi hain is mahine")

    def test_medical_turn_pauses_the_ladder(self):
        policy = verified_policy()
        plan = observe_and_plan(
            policy, "मेरी मम्मी hospital में हैं, अभी paise नहीं दे सकता",
            "hardship",
        )
        assert plan.action == "acknowledge_hardship"
        # No rung consumed by the emergency turn.
        assert not policy.consequence_used
        step = policy.turn_instruction()
        assert "medical/family emergency" in step
        assert "do NOT mention CIBIL" in step

    def test_medical_without_hardship_signal_still_pauses(self):
        policy = verified_policy()
        plan = observe_and_plan(
            policy, "बाद में call करना, papa hospital में हैं", "callback"
        )
        assert plan.action == "acknowledge_hardship"
        assert not policy.consequence_used

    def test_plain_hardship_still_runs_the_ladder_with_empathy(self):
        policy = verified_policy()
        plan = observe_and_plan(policy, "paise nahi hain bhai", "hardship")
        assert plan.action == "recovery_consequence"
        assert "acknowledgment" in policy.turn_instruction()


class TestBrainAmountLookup:
    AMOUNT_INTENT = {
        "name": "amount_query",
        "route": "tool:account_status",
        "sample_phrases": [],
    }

    async def test_amount_question_runs_real_tool_and_grounds_reply(self):
        from tests.unit.test_brain_collection_policy import (
            bot_replies, snapshot, turn, verify_identity,
        )
        from tests.unit.test_brain_hybrid_intents import (
            _ToolStub, make_hybrid_brain,
        )

        tool = _ToolStub({"total_outstanding": 5400, "minimum_payable": 900})
        brain = make_hybrid_brain(
            context=snapshot(), intents=[self.AMOUNT_INTENT], tool=tool,
        )
        await verify_identity(brain)
        await turn(brain, "total kitna amount dena hai mujhe?")
        # The REAL lookup ran (not a pretend verification)...
        assert any(c["tool"] == "account_status" for c in tool.calls)
        # ...and the policy's facts now carry the returned figures.
        assert brain._policy.account_refresh_succeeded
        assert brain._policy.context.total_outstanding == 5400.0
        assert bot_replies(brain)  # a grounded reply was spoken

    async def test_failed_lookup_answers_from_loaded_facts(self):
        from tests.unit.test_brain_collection_policy import (
            bot_replies, snapshot, turn, verify_identity,
        )
        from tests.unit.test_brain_hybrid_intents import (
            _ToolStub, make_hybrid_brain,
        )

        tool = _ToolStub({}, ok=False)
        brain = make_hybrid_brain(
            context=snapshot(), intents=[self.AMOUNT_INTENT], tool=tool,
        )
        await verify_identity(brain)
        await turn(brain, "total kitna amount dena hai mujhe?")
        assert any(c["tool"] == "account_status" for c in tool.calls)
        assert brain._policy.account_refreshed
        assert not brain._policy.account_refresh_succeeded
        # Loaded snapshot values are untouched by the failure.
        assert brain._policy.context.total_outstanding == 5120.0
        assert bot_replies(brain)


class TestMultiTurnConversation:
    def test_refusal_agreement_reference_flow(self):
        """Refusal → rung; agreement → commitment; never back to the ladder."""
        policy = verified_policy()
        assert observe_and_plan(
            policy, "नहीं, अभी नहीं होगा", "refusal"
        ).action == "recovery_consequence"
        assert observe_and_plan(
            policy, "ठीक है, main payment kar dunga aaj", "payment_intent"
        ).action == "confirm_commitment"
        assert policy.promise_date_known  # "aaj" is a concrete time
        plan = observe_and_plan(policy, "UPI se kar dunga", "payment_intent")
        assert plan.close_after_reply
        # A polite goodbye never resurrects a rung.
        assert not policy.offer_used and not policy.partial_used
