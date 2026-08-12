"""Goal Engine + decision schema — the domain-neutral agentic decision layer.

Covers the refactor's core guarantees at engine level:

- structured decisions are schema-validated and clamp/reject malformed model
  output instead of acting on it;
- a slot VALUE exists only when the caller actually provided one — "हाँ,
  नंबर है" (exists_claimed) never carries a value;
- out-of-scope / prompt-injection turns are forced onto a redirect with
  tools, slots and gate outcomes stripped — the caller cannot move the bot
  off its configured goal;
- the engine degrades to None (deterministic fallback) on provider failure,
  timeout, garbage output or when disabled — it never raises into a call;
- a loan bot and a healthcare bot run the SAME engine with different
  configured policies: their decision prompts, redirect instructions and
  identity behavior differ purely by configuration;
- bots without an authored goal configuration compile a safe derived default
  from their published prompt, intents and domain policy.
"""

import asyncio
import json

import pytest

from shared.orchestration.decision_schema import (
    ConversationDecision,
    parse_decision,
)
from shared.orchestration.goal_engine import (
    BotGoalPolicy,
    GoalEngine,
    GoalSession,
    GoalSpec,
    IdentityPolicy,
    SlotSpec,
    compile_goal_policy,
)

# ── fakes ────────────────────────────────────────────────────────────────────


class _DecisionLLM:
    """Answers generate() with scripted JSON (or misbehaves on demand)."""

    def __init__(self, reply, *, delay: float = 0.0, fail: bool = False):
        self._reply = reply if isinstance(reply, str) else json.dumps(reply)
        self._delay = delay
        self._fail = fail
        self.calls: list[dict] = []

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        self.calls.append({"messages": messages, "system": system,
                           "max_tokens": max_tokens})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("provider down")

        class _Result:
            text = self._reply
            input_tokens = 150
            output_tokens = 60

        return _Result()


LOAN_POLICY = BotGoalPolicy(
    role="loan recovery assistant",
    domain="loan collections",
    goals=[GoalSpec(id="recover", description="resolve the overdue payment",
                    completion="payment collected, verified, or a concrete "
                               "commitment captured")],
    allowed_topics=["overdue payment", "payment methods", "payment verification"],
    restricted_topics=["jokes", "unrelated chit-chat"],
    identity=IdentityPolicy(require_confirmation=True,
                            subject="the registered customer"),
    slots=[SlotSpec(name="transaction_reference",
                    description="transaction/UTR number of a claimed payment",
                    pattern=r"\d{6,}", required=False)],
    out_of_scope="Redirect to the overdue payment objective.",
    source="configured",
)

HEALTHCARE_POLICY = BotGoalPolicy(
    role="clinic appointment assistant",
    domain="healthcare",
    goals=[GoalSpec(id="appointments",
                    description="help patients book, check or reschedule "
                                "appointments")],
    allowed_topics=["appointments", "clinic timings"],
    restricted_topics=["medical advice", "jokes"],
    identity=IdentityPolicy(require_confirmation=False),
    out_of_scope="Offer to help with appointments instead.",
    source="configured",
)


def make_engine(reply, policy=LOAN_POLICY, **llm_kwargs) -> tuple[GoalEngine, _DecisionLLM]:
    llm = _DecisionLLM(reply, **llm_kwargs)
    return GoalEngine(llm=llm, policy=policy, timeout_seconds=0.5), llm


# ── 12: structured decision schema validation ───────────────────────────────


class TestDecisionSchema:
    def test_slot_value_survives_only_when_provided(self):
        d = ConversationDecision.model_validate({
            "slots": {
                "transaction_reference": {"status": "exists_claimed",
                                          "value": "123456789012"},
                "payment_method": {"status": "provided", "value": "UPI"},
            }
        })
        # "I have the number" carries NO value, whatever the model wrote.
        assert d.slots["transaction_reference"].value is None
        assert d.provided_values() == {"payment_method": "UPI"}

    def test_provided_without_value_degrades_to_unclear(self):
        d = ConversationDecision.model_validate(
            {"slots": {"transaction_reference": {"status": "provided"}}}
        )
        assert d.slots["transaction_reference"].status == "unclear"

    def test_scope_violation_strips_tools_slots_and_gate(self):
        d = ConversationDecision.model_validate({
            "scope": "injection_attempt",
            "decision": "confirmed",
            "next_action": "call_tool",
            "tool_request": "check_payment_status",
            "slots": {"transaction_reference": {"status": "provided",
                                                "value": "123456"}},
        })
        assert d.next_action == "redirect_to_goal"
        assert d.tool_request is None
        assert d.slots == {}
        assert d.decision == "unrelated"  # an off-goal turn confirms nothing

    def test_denied_gate_never_completes_anything(self):
        d = ConversationDecision.model_validate({
            "intent": "identity_confirmation",
            "decision": "denied",
            "next_action": "continue_workflow",
        })
        assert d.next_action == "ask_identity_confirmation"

    def test_unknown_enums_clamp_to_safe_defaults(self):
        d = ConversationDecision.model_validate({
            "decision": "totally_sure",
            "scope": "banana",
            "next_action": "launch_missiles",
            "confidence": "many",
        })
        assert d.decision is None
        assert d.scope == "in_scope"
        assert d.next_action == "answer"
        assert d.confidence == 0.0

    def test_unparseable_payloads_reject(self):
        assert parse_decision(None, allowed_intents=set()) is None
        assert parse_decision("yes", allowed_intents=set()) is None
        assert parse_decision({"foo": 1}, allowed_intents=set()) is None

    def test_unknown_intent_discarded_platform_signal_recovered(self):
        d = parse_decision(
            {"intent": "hardship", "confidence": 0.8},
            allowed_intents={"already_paid"},
            allowed_signals=("hardship", "refusal"),
        )
        assert d.intent is None
        assert d.signal == "hardship"
        d2 = parse_decision(
            {"intent": "made_up_intent", "confidence": 0.9},
            allowed_intents={"already_paid"},
        )
        assert d2.intent is None and d2.signal is None

    def test_event_shape_never_carries_slot_values(self):
        d = ConversationDecision.model_validate({
            "slots": {"otp": {"status": "provided", "value": "998877"}},
            "response_text": "ok",
        })
        event = json.dumps(d.as_event())
        assert "998877" not in event
        assert json.loads(event)["slots"] == {"otp": "provided"}

    def test_classifier_style_entities_fold_into_slots(self):
        d = parse_decision(
            {"intent": "already_paid", "signal": "already_paid",
             "confidence": 0.9, "entities": {"payment_method": "UPI",
                                             "transaction_reference": None}},
            allowed_intents={"already_paid"},
            allowed_signals=("already_paid",),
        )
        assert d.provided_values() == {"payment_method": "UPI"}


# ── engine decide(): validation, degradation, latency accounting ────────────


class TestGoalEngineDecide:
    async def test_valid_decision_parses_with_usage(self):
        engine, llm = make_engine({
            "intent": "identity_confirmation", "decision": "confirmed",
            "scope": "in_scope", "confidence": 0.94,
            "reason": "explicit self-identification",
            "next_action": "continue_workflow",
        })
        decision = await engine.decide(
            "हाँ, मैं ही बोल रहा हूँ", [],
            state={"pending_question": "identity confirmation"},
        )
        assert decision is not None
        assert decision.decision == "confirmed"
        assert decision.intent == "identity_confirmation"
        assert decision.source == "llm"
        assert decision.latency_ms > 0
        assert engine.last_usage == (150, 60)

    async def test_provider_failure_returns_none(self):
        engine, _ = make_engine({}, fail=True)
        assert await engine.decide("hello", []) is None
        assert engine.last_fallback_reason == "provider_error"

    async def test_timeout_returns_none(self):
        engine, _ = make_engine({"decision": "confirmed"}, delay=2.0)
        assert await engine.decide("hello", []) is None
        assert engine.last_fallback_reason == "timeout"

    async def test_garbage_output_returns_none(self):
        engine, _ = make_engine("confirmed, definitely them")
        assert await engine.decide("hello", []) is None
        assert engine.last_fallback_reason == "unparseable_output"

    async def test_disabled_engine_returns_none_without_calling(self):
        llm = _DecisionLLM({"decision": "confirmed"})
        engine = GoalEngine(llm=llm, policy=LOAN_POLICY, enabled=False)
        assert await engine.decide("hello", []) is None
        assert engine.last_fallback_reason == "engine_disabled"
        assert llm.calls == []

    async def test_three_consecutive_timeouts_disable_the_engine(self):
        engine, llm = make_engine({"scope": "in_scope"}, delay=2.0)
        for _ in range(3):
            assert await engine.decide("hello", []) is None
            assert engine.last_fallback_reason == "timeout"
        # Disabled for the remainder: immediate None, no model call.
        assert await engine.decide("hello", []) is None
        assert engine.last_fallback_reason == "disabled_after_timeouts"
        assert len(llm.calls) == 3

    async def test_a_successful_decision_resets_the_timeout_streak(self):
        engine, llm = make_engine({"scope": "in_scope"}, delay=2.0)
        for _ in range(2):
            assert await engine.decide("hello", []) is None
            assert engine.last_fallback_reason == "timeout"
        llm._delay = 0.0
        assert await engine.decide("hello", []) is not None
        # The streak restarted: two more timeouts still leave the engine on.
        llm._delay = 2.0
        for _ in range(2):
            assert await engine.decide("hello", []) is None
            assert engine.last_fallback_reason == "timeout"
        llm._delay = 0.0
        decision = await engine.decide("hello", [])
        assert decision is not None
        assert engine.last_fallback_reason is None

    async def test_live_state_reaches_the_user_message(self):
        engine, llm = make_engine({"decision": "ambiguous"})
        await engine.decide("क्या?", [], state={
            "pending_question": "identity confirmation",
            "missing_slots": ["transaction_reference"],
            "identity_state": "unconfirmed",
        })
        user = llm.calls[-1]["messages"][-1]["content"]
        assert "identity confirmation" in user
        assert "transaction_reference" in user
        assert "क्या?" in user

    async def test_voice_gender_reaches_stage_a_response_generation(self):
        engine, llm = make_engine({
            "scope": "in_scope",
            "response_text": "मैं समझ सकती हूँ।",
        })

        await engine.decide("मेरे पास पैसे नहीं हैं", [], state={
            "language": "hi-IN",
            "assistant_voice_name": "Catalog Female",
            "assistant_voice_gender": "female",
        })

        user = llm.calls[-1]["messages"][-1]["content"]
        system = llm.calls[-1]["system"]
        assert "Assistant voice name (catalog metadata): Catalog Female" in user
        assert "Assistant voice gender (authoritative): female" in user
        assert "सकती हूँ" in system
        assert "caller" in system


# ── latency budget: bounded input, bounded output, hard deadline ─────────────


class TestLatencyBudget:
    def test_default_budget_is_tight(self):
        engine = GoalEngine(llm=_DecisionLLM({}), policy=LOAN_POLICY)
        assert engine._timeout == 1.2
        assert engine._max_tokens == 200

    def test_configured_values_are_clamped_to_safe_ranges(self):
        engine = GoalEngine(llm=_DecisionLLM({}), policy=LOAN_POLICY,
                            timeout_seconds=60, max_tokens=4096)
        assert engine._timeout == 5.0
        assert engine._max_tokens == 340
        engine = GoalEngine(llm=_DecisionLLM({}), policy=LOAN_POLICY,
                            timeout_seconds=0.05, max_tokens=8)
        assert engine._timeout == 0.5
        assert engine._max_tokens == 64
        engine = GoalEngine(llm=_DecisionLLM({}), policy=LOAN_POLICY,
                            timeout_seconds="junk", max_tokens=None)
        assert engine._timeout == 1.2
        assert engine._max_tokens == 200

    async def test_max_tokens_reaches_the_model_call(self):
        engine, llm = make_engine({"scope": "in_scope"})
        await engine.decide("hello", [])
        assert llm.calls[-1]["max_tokens"] == 200

    async def test_history_is_trimmed_to_two_capped_turns(self):
        engine, llm = make_engine({"scope": "in_scope"})
        history = [
            {"role": "user", "content": f"OLD-{i} " + "x" * 500}
            for i in range(6)
        ]
        history += [
            {"role": "assistant", "content": "RECENT-BOT " + "y" * 500},
            {"role": "user", "content": "RECENT-USER " + "z" * 500},
        ]
        await engine.decide("अभी बताइए", history)
        user = llm.calls[-1]["messages"][-1]["content"]
        assert "RECENT-BOT" in user and "RECENT-USER" in user
        assert "OLD-5" not in user  # only the last two turns travel
        for line in user.splitlines():
            if line.startswith(("Bot:", "Caller:")):
                assert len(line) <= 240 + len("Caller: ")

    def test_derived_prompt_excerpt_is_capped(self):
        policy = compile_goal_policy(
            None, bot_name="X", system_prompt="p" * 10_000,
            intents=[], domain_policy="generic",
        )
        assert len(policy.prompt_excerpt) <= 1200

    def test_full_decision_output_fits_the_token_budget(self):
        """The output cap must never truncate a schema-complete decision.

        A worst-case realistic decision (every field populated, a two-short-
        sentence Hindi response_text, a 12-word reason) is measured with the
        real tokenizer of the default orchestration model family. If this
        fails, raise the default budget (max 240) rather than truncating.
        """
        import tiktoken

        worst_case = {
            "intent": "identity_confirmation",
            "signal": "already_paid",
            "decision": "needs_clarification",
            "scope": "in_scope",
            "confidence": 0.85,
            "reason": "caller claims payment done but gave no usable "
                      "transaction reference yet",
            "slots": {
                "transaction_reference": {"status": "exists_claimed",
                                          "value": None},
                "payment_method": {"status": "provided", "value": "UPI"},
                "payment_date": {"status": "provided", "value": "kal shaam"},
            },
            "next_action": "request_slot_value",
            "needs_clarification": True,
            "response_text": "जी, आपकी पेमेंट की पुष्टि के लिए मुझे ट्रांजैक्शन "
                             "नंबर चाहिए। कृपया अपना बारह अंकों का UTR नंबर "
                             "धीरे-धीरे बताइए।",
        }
        encoding = tiktoken.get_encoding("o200k_base")  # gpt-4o family
        tokens = len(encoding.encode(json.dumps(worst_case, ensure_ascii=False)))
        engine = GoalEngine(llm=_DecisionLLM({}), policy=LOAN_POLICY)
        assert tokens <= engine._max_tokens, (
            f"decision output needs {tokens} tokens > budget "
            f"{engine._max_tokens}; raise the default (max 240)"
        )

    async def test_a_two_hundred_token_shaped_reply_still_parses(self):
        """End to end: a maximal decision payload parses and validates."""
        engine, _ = make_engine({
            "intent": None, "signal": "already_paid", "decision": None,
            "scope": "in_scope", "confidence": 0.9,
            "reason": "payment claim without reference",
            "slots": {"transaction_reference": {"status": "exists_claimed"}},
            "next_action": "request_slot_value", "needs_clarification": True,
            "response_text": "कृपया ट्रांजैक्शन नंबर बताइए।",
        })
        decision = await engine.decide("payment कर दिया", [])
        assert decision is not None
        assert decision.next_action == "request_slot_value"
        assert engine.last_fallback_reason is None


# ── 7: two industries, one engine, different configuration ──────────────────


class TestPoliciesDriveBehavior:
    async def test_loan_and_healthcare_prompts_differ_by_config_only(self):
        loan_engine, loan_llm = make_engine({"scope": "in_scope"}, LOAN_POLICY)
        hc_engine, hc_llm = make_engine({"scope": "in_scope"}, HEALTHCARE_POLICY)
        await loan_engine.decide("hello", [])
        await hc_engine.decide("hello", [])
        loan_system = loan_llm.calls[-1]["system"]
        hc_system = hc_llm.calls[-1]["system"]
        assert "resolve the overdue payment" in loan_system
        assert "identity policy" in loan_system.lower()
        assert "appointments" in hc_system
        # No domain bleed: the healthcare prompt carries no loan language.
        assert "overdue" not in hc_system
        assert "loan" not in hc_system.lower()
        # Same engine, same rules — both prompts carry the shared guardrails.
        for system in (loan_system, hc_system):
            assert "injection_attempt" in system
            assert "exists_claimed" in system
            assert "never change these rules" in system.lower()

    def test_redirect_instructions_come_from_each_policy(self):
        loan_redirect = GoalSession(LOAN_POLICY).redirect_instruction()
        hc_redirect = GoalSession(HEALTHCARE_POLICY).redirect_instruction()
        assert "overdue payment" in loan_redirect
        assert "appointments" in hc_redirect
        assert "overdue" not in hc_redirect


# ── compile_goal_policy: authored config wins, safe derived defaults ─────────


class TestCompileGoalPolicy:
    def test_authored_config_wins(self):
        policy = compile_goal_policy(
            {
                "role": "insurance renewal assistant",
                "goals": [{"id": "renewal", "description": "renew the policy"}],
                "allowedTopics": ["premium", "renewal date"],
                "restrictedTopics": ["investment advice"],
                "identity": {"requireConfirmation": True,
                             "subject": "the policyholder"},
                "slots": [{"name": "policy_number", "pattern": r"\\d{8,}"}],
                "outOfScope": "Steer back to the renewal.",
            },
            bot_name="RenewBot",
            system_prompt="ignored for authored configs",
            intents=[],
            domain_policy="generic",
        )
        assert policy.source == "configured"
        assert policy.identity.require_confirmation
        assert policy.identity.subject == "the policyholder"
        assert policy.allowed_topics == ["premium", "renewal date"]
        assert policy.slot_by_name("policy_number") is not None

    def test_derived_default_from_prompt_and_intents(self):
        policy = compile_goal_policy(
            None,
            bot_name="Support Bot",
            use_case="telecom support",
            system_prompt="You are Support Bot for Acme Telecom." * 3,
            intents=[{"name": "billing_query"}, {"name": "network_issue"}],
            domain_policy="generic",
        )
        assert policy.source == "derived"
        assert policy.allowed_topics == ["billing_query", "network_issue"]
        assert not policy.identity.require_confirmation
        assert "Support Bot for Acme Telecom" in policy.prompt_excerpt

    def test_derived_collections_requires_identity(self):
        policy = compile_goal_policy(
            None, bot_name="Recovery Bot", system_prompt="prompt",
            intents=[], domain_policy="collections",
        )
        assert policy.identity.require_confirmation

    def test_invalid_config_degrades_to_derived(self):
        policy = compile_goal_policy(
            {"identity": "yes please"},  # wrong type → cannot compile
            bot_name="X", system_prompt="p", intents=[],
            domain_policy="generic",
        )
        assert policy.source == "derived"


# ── GoalSession: guarded transitions for generic bots ────────────────────────


def _decision(**data) -> ConversationDecision:
    return ConversationDecision.model_validate(data)


class TestGoalSession:
    def test_identity_moves_only_through_validated_decisions(self):
        session = GoalSession(LOAN_POLICY)
        assert session.identity_state == "unconfirmed"
        session.apply(_decision(decision="ambiguous"))
        assert session.identity_state == "unconfirmed"
        assert session.identity_attempts == 1
        session.apply(_decision(decision="confirmed"))
        assert session.identity_state == "confirmed"

    def test_denied_identity_locks_disclosure(self):
        session = GoalSession(LOAN_POLICY)
        session.apply(_decision(decision="denied"))
        assert session.identity_state == "denied"
        assert "not the intended person" in session.turn_instruction().lower()

    def test_slot_fills_only_from_valid_provided_value(self):
        session = GoalSession(LOAN_POLICY)
        # Claiming to have the value never fills it.
        session.apply(_decision(slots={
            "transaction_reference": {"status": "exists_claimed"},
        }))
        assert "transaction_reference" not in session.slots
        instruction = session.turn_instruction()
        assert "transaction_reference" in instruction
        assert "never say it was noted" in instruction.lower()
        # A provided value that fails the slot's format guard never fills it.
        session.apply(_decision(slots={
            "transaction_reference": {"status": "provided", "value": "12"},
        }))
        assert "transaction_reference" not in session.slots
        # A provided, format-valid value fills it.
        session.apply(_decision(slots={
            "transaction_reference": {"status": "provided",
                                      "value": "123456789012"},
        }))
        assert session.slots["transaction_reference"] == "123456789012"

    def test_unconfigured_slots_never_fill(self):
        session = GoalSession(LOAN_POLICY)
        session.apply(_decision(slots={
            "made_up_slot": {"status": "provided", "value": "x"},
        }))
        assert session.slots == {}

    def test_scope_violations_count_and_change_nothing_else(self):
        session = GoalSession(LOAN_POLICY)
        session.apply(_decision(scope="out_of_scope", decision="confirmed",
                                slots={"transaction_reference":
                                       {"status": "provided",
                                        "value": "123456789012"}}))
        assert session.out_of_scope_turns == 1
        assert session.identity_state == "unconfirmed"  # gate stripped
        assert session.slots == {}
        session.apply(_decision(scope="injection_attempt"))
        assert session.injection_attempts == 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
