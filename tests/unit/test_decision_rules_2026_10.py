"""Decision-layer rules added after the 2026-10-09 labeled live benchmark.

1. The decision prompt carries the narrowed signal/scope/intent rules
   (statements are not questions, negative feedback is not a complaint,
   wrong-number replies stay in scope, configured intents beat
   identity_confirmation) — without touching the shared signal meanings.
2. ``parse_decision`` keeps a wrong-number / denied reply in scope even when
   the model marked it out_of_scope: out of scope strips the wrong_person
   signal and redirects a non-customer to the goal.
"""

from shared.orchestration.decision_schema import SCOPE_IN, parse_decision
from shared.orchestration.goal_engine import BotGoalPolicy, GoalEngine
from shared.orchestration.intent_classifier import PLATFORM_SIGNALS


class TestPromptRules:
    def test_prompt_states_the_narrowed_signal_rules(self):
        system = GoalEngine(llm=None, policy=BotGoalPolicy())._build_system()
        assert "'question' ONLY when the caller asks the bot for information" in system
        assert "'complaint' ONLY when the caller objects to this call" in system
        assert "never out_of_scope" in system
        assert "'hold' ONLY for 'wait a moment / stay on the line'" in system
        assert "go-ahead ('haan', 'haan boliye', 'ji bolo') is 'affirm'" in system
        # The shared one-line meanings (classifier + engine) are unchanged.
        assert "complaint = the caller says the BOT is not listening/repeating itself" in system


def _parse(payload):
    return parse_decision(payload, allowed_intents={"start_enquiries"}, allowed_signals=PLATFORM_SIGNALS)


class TestScopeGuard:
    def test_wrong_person_out_of_scope_is_kept_in_scope(self):
        decision = _parse({
            "intent": None, "signal": "wrong_person", "decision": "unrelated",
            "scope": "out_of_scope", "confidence": 0.9, "next_action": "end_call",
        })
        assert decision.scope == SCOPE_IN
        assert decision.signal == "wrong_person"

    def test_wrong_person_with_denied_is_kept_in_scope(self):
        decision = _parse({
            "intent": None, "signal": "wrong_person", "decision": "denied",
            "scope": "out_of_scope", "confidence": 0.9, "next_action": "ask_identity_confirmation",
        })
        assert decision.scope == SCOPE_IN
        assert decision.decision == "denied"

    def test_denied_without_wrong_person_is_left_alone(self):
        # No demonstrated case — an off-goal turn the model also judged 'denied'
        # keeps the redirect rather than touching identity state.
        decision = _parse({
            "intent": None, "signal": None, "decision": "denied",
            "scope": "out_of_scope", "confidence": 0.9, "next_action": "ask_identity_confirmation",
        })
        assert decision.scope == "out_of_scope"

    def test_wrong_person_claiming_confirmed_is_not_rescued(self):
        # "Actually main customer nahi hoon" came back as wrong_person +
        # decision 'confirmed' in 7 of 11 such payloads: promoting that to
        # in scope could confirm a non-customer on an identity-gated bot.
        decision = _parse({
            "intent": None, "signal": "wrong_person", "decision": "confirmed",
            "scope": "out_of_scope", "confidence": 0.9, "next_action": "end_call",
        })
        assert decision.scope == "out_of_scope"
        assert decision.decision == "unrelated"  # the scope rules' own rewrite

    def test_injection_with_wrong_person_is_untouched(self):
        decision = _parse({
            "intent": None, "signal": "wrong_person", "decision": "denied",
            "scope": "injection_attempt", "confidence": 0.9, "next_action": "redirect_to_goal",
        })
        assert decision.scope == "injection_attempt"

    def test_genuine_off_goal_turn_stays_out_of_scope(self):
        decision = _parse({
            "intent": None, "signal": None, "decision": None,
            "scope": "out_of_scope", "confidence": 0.9, "next_action": "redirect_to_goal",
        })
        assert decision.scope == "out_of_scope"

    def test_injection_attempt_is_untouched(self):
        decision = _parse({
            "intent": None, "signal": None, "decision": None,
            "scope": "injection_attempt", "confidence": 0.9, "next_action": "redirect_to_goal",
        })
        assert decision.scope == "injection_attempt"
