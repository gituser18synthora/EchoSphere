"""Turn-router decisions: the KB-use decision layer."""

from shared.orchestration.router import RouteKind, TurnRouter


def make_router(**kwargs) -> TurnRouter:
    defaults = dict(has_knowledge_bases=True)
    defaults.update(kwargs)
    return TurnRouter(**defaults)


class TestSmalltalkSkipsKB:
    def test_greetings(self):
        router = make_router()
        for text in ("hello", "Hi!", "good morning", "thanks", "thank you", "okay", "bye"):
            decision = router.decide(text)
            assert decision.kind == RouteKind.CHAT, text
            assert decision.reason == "smalltalk"

    def test_confirmations(self):
        router = make_router()
        assert router.decide("yes").kind == RouteKind.CHAT
        assert router.decide("no").kind == RouteKind.CHAT


class TestCallControl:
    def test_hangup(self):
        decision = make_router().decide("please hang up the call")
        assert decision.kind == RouteKind.CALL_CONTROL
        assert decision.action == "hangup"

    def test_repeat(self):
        decision = make_router().decide("could you repeat that please")
        assert decision.kind == RouteKind.CALL_CONTROL
        assert decision.action == "repeat"

    def test_slower(self):
        assert make_router().decide("please speak more slowly").action == "slower"

    def test_transfer_is_handoff(self):
        decision = make_router().decide("transfer me to a human agent")
        assert decision.kind == RouteKind.HANDOFF
        assert decision.action == "transfer"

    def test_want_human(self):
        decision = make_router().decide("I want to talk to a supervisor now")
        assert decision.kind == RouteKind.HANDOFF


class TestKnowledgeDecision:
    def test_policy_question_uses_kb(self):
        decision = make_router().decide("What is the grace period for policy renewal?")
        assert decision.kind == RouteKind.KNOWLEDGE
        assert decision.considered_kb is True

    def test_no_kbs_configured_falls_to_chat(self):
        decision = make_router(has_knowledge_bases=False).decide(
            "What is the grace period for policy renewal?"
        )
        assert decision.kind == RouteKind.CHAT

    def test_extra_domain_keywords(self):
        router = make_router(kb_keywords=["emi"])
        assert router.decide("tell me about my emi schedule").kind == RouteKind.KNOWLEDGE

    def test_short_ambiguous_input_clarifies(self):
        decision = make_router().decide("account")
        assert decision.kind == RouteKind.CLARIFY


class TestWorkflowPriority:
    def test_active_workflow_consumes_turn(self):
        decision = make_router().decide(
            "What is the grace period?", active_workflow="appointment_booking"
        )
        assert decision.kind == RouteKind.WORKFLOW

    def test_call_control_escapes_workflow(self):
        decision = make_router().decide("hang up", active_workflow="appointment_booking")
        assert decision.kind == RouteKind.CALL_CONTROL

    def test_intent_routes_to_workflow(self):
        router = make_router(
            intents=[{
                "name": "book_appointment",
                "samples": ["book an appointment", "schedule a visit"],
                "route": "workflow:appointment_booking",
                "confidence_threshold": 0.3,
            }]
        )
        decision = router.decide("I would like to book an appointment")
        assert decision.kind == RouteKind.WORKFLOW
        assert decision.action == "appointment_booking"


class TestExplicitIntentRoutes:
    """Intent route values "knowledge" and "handoff" — used by locale-specific
    intents whose utterances the English KB/handoff heuristics can't detect."""

    def _router(self, route: str, **kwargs) -> TurnRouter:
        return make_router(
            intents=[{
                "name": "penalty_charges",
                "samples": ["penalty kitni lagegi", "late fee"],
                "route": route,
                "confidence_threshold": 0.3,
            }],
            **kwargs,
        )

    def test_knowledge_route_forces_retrieval(self):
        decision = self._router("knowledge").decide("mujhe batao penalty kitni lagegi")
        assert decision.kind == RouteKind.KNOWLEDGE
        assert decision.intent == "penalty_charges"
        assert decision.considered_kb is True

    def test_knowledge_route_without_kbs_degrades_to_intent(self):
        decision = self._router("knowledge", has_knowledge_bases=False).decide(
            "mujhe batao penalty kitni lagegi"
        )
        assert decision.kind == RouteKind.INTENT

    def test_handoff_route_transfers(self):
        router = make_router(
            intents=[{
                "name": "human_agent",
                "samples": ["agent se baat karni hai", "kisi insaan se baat"],
                "route": "handoff",
                "confidence_threshold": 0.3,
            }]
        )
        decision = router.decide("mujhe agent se baat karni hai abhi")
        assert decision.kind == RouteKind.HANDOFF
        assert decision.action == "transfer"
        assert decision.intent == "human_agent"


class TestSafety:
    def test_card_number_disclosure(self):
        decision = make_router().decide("my card number is 4111 1111 1111 1111")
        assert decision.kind == RouteKind.SAFETY

    def test_empty_input(self):
        assert make_router().decide("   ").kind == RouteKind.CLARIFY


class TestMultilingualHangup:
    """Deterministic hang-up detection: Hindi, Hinglish, English, and the
    transcription variants collections callers actually produce. Hang-up
    outranks everything — including an active workflow."""

    HANGUP_PHRASES = [
        "फोन कट करो",
        "Phone cut karo",
        "Call disconnect karo",
        "Call band karo",
        "Cut kar do",
        "बस, कॉल खत्म करो",
        "Hang up",
        "Disconnect the call",
        "cut karu",           # transcription variant
        "kaat do",
        "काट दो",
        "फ़ोन काट दो",
        "call khatam karo",
        "phone rakh do",
        "कॉल बंद करो",
        "please end this call",
    ]

    NOT_HANGUP = [
        "नहीं, मेरे पास अभी पैसा नहीं है",   # refusal, not a hang-up
        "phone mat kato",                     # negation
        "फोन मत काटो",
        "don't hang up",
        "काटना मत",
        "paise kat gaye account se",          # money deducted (past tense)
        "मेरे अकाउंट से पैसे कट गए",
        "EMI cut ho gayi",
        "SMS band karo",                      # stop the messages, not the call
        "kal payment kar dunga",
    ]

    def test_hangup_phrases(self):
        from shared.orchestration.router import detect_hangup

        router = make_router()
        for phrase in self.HANGUP_PHRASES:
            assert detect_hangup(phrase), phrase
            decision = router.decide(phrase)
            assert decision.kind == RouteKind.CALL_CONTROL, phrase
            assert decision.action == "hangup", phrase

    def test_non_hangup_phrases(self):
        from shared.orchestration.router import detect_hangup

        router = make_router()
        for phrase in self.NOT_HANGUP:
            assert not detect_hangup(phrase), phrase
            decision = router.decide(phrase)
            assert decision.action != "hangup", phrase

    def test_hangup_beats_active_workflow(self):
        # A caller asking to hang up mid-ladder must never get the next rung.
        decision = make_router().decide(
            "Phone cut karo", active_workflow="dpd_0_7_collection_call"
        )
        assert decision.kind == RouteKind.CALL_CONTROL
        assert decision.action == "hangup"

    def test_configured_hangup_intent_routes_to_call_control(self):
        router = make_router(
            intents=[{
                "name": "hangup_semantic",
                "samples": ["baat khatam", "rehne do"],
                "route": "hangup",
                "confidence_threshold": 0.3,
            }]
        )
        decision = router.decide("ab rehne do bhai")
        assert decision.kind == RouteKind.CALL_CONTROL
        assert decision.action == "hangup"
        assert decision.intent == "hangup_semantic"
