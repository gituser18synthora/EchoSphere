"""Platform-critical deterministic commands: DNC, emergency, consent refusal.

These never go through the LLM — a caller revoking consent or reporting an
emergency must be handled identically every single time, in every language,
before any workflow or model sees the turn.
"""

from shared.orchestration.router import (
    RouteKind,
    TurnRouter,
    detect_consent_refusal,
    detect_do_not_call,
    detect_emergency,
)


class TestDoNotCall:
    def test_english_forms(self):
        for phrase in (
            "don't call me again",
            "do not call me",
            "stop calling me",
            "never call this number again",
            "remove my number from your list",
            "stop these calls",
        ):
            assert detect_do_not_call(phrase), phrase

    def test_hindi_hinglish_forms(self):
        for phrase in (
            "dobara call mat karna",
            "phir se phone mat karo",
            "aage se call mat karna mujhe",
            "दोबारा कॉल मत करना",
            "फिर फोन मत करो",
            "call mat karo",
        ):
            assert detect_do_not_call(phrase), phrase

    def test_non_dnc_not_matched(self):
        for phrase in (
            "call me tomorrow",        # a callback, not a revocation
            "please call my brother",
            "aap kal call karna",      # asking FOR a call
            "I was called yesterday",
        ):
            assert not detect_do_not_call(phrase), phrase

    def test_dnc_beats_active_workflow(self):
        router = TurnRouter(intents=[], has_knowledge_bases=False)
        decision = router.decide("dobara call mat karna",
                                 active_workflow="payment_collection")
        assert decision.kind == RouteKind.CALL_CONTROL
        assert decision.action == "do_not_call"


class TestEmergency:
    def test_emergency_forms(self):
        for phrase in (
            "this is an emergency, I need an ambulance",
            "ghar mein accident ho gaya hai",
            "heart attack aaya hai unko",
            "एम्बुलेंस बुलाओ जल्दी",
        ):
            assert detect_emergency(phrase), phrase

    def test_routes_to_human_before_anything(self):
        router = TurnRouter(intents=[], has_knowledge_bases=True)
        decision = router.decide("emergency hai, ambulance chahiye",
                                 active_workflow="payment_collection")
        assert decision.kind == RouteKind.HANDOFF
        assert decision.reason == "emergency"

    def test_ordinary_turns_not_emergency(self):
        for phrase in ("payment kar dunga kal", "kitna amount hai",
                       "mujhe details bhejo"):
            assert not detect_emergency(phrase), phrase


class TestConsentRefusal:
    def test_recording_refusals(self):
        for phrase in (
            "don't record this call",
            "stop recording",
            "recording band karo",
            "रिकॉर्डिंग मत करो",
        ):
            assert detect_consent_refusal(phrase), phrase

    def test_plain_mentions_pass(self):
        assert not detect_consent_refusal("is this call recorded?")


class TestHangupStillFirst:
    def test_hangup_ordering_unchanged(self):
        router = TurnRouter(intents=[], has_knowledge_bases=False)
        decision = router.decide("call कट कर दो", active_workflow="x")
        assert decision.kind == RouteKind.CALL_CONTROL
        assert decision.action == "hangup"
