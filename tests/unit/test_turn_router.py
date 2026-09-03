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

    def test_modifier_inside_sample_still_routes_to_workflow(self):
        router = make_router(
            intents=[{
                "name": "booking_confirmation",
                "samples": [
                    "is my booking confirmed",
                    "booking confirmation",
                    "confirm my booking",
                    "booking status",
                    "is my reservation confirmed",
                    "check my booking",
                ],
                "route": "workflow:oyo_booking_support_journey",
                "confidence_threshold": 0.05,
            }]
        )

        decision = router.decide("I want to confirm my upcoming booking.")

        assert decision.kind == RouteKind.WORKFLOW
        assert decision.intent == "booking_confirmation"
        assert decision.action == "oyo_booking_support_journey"

    def test_hindi_delivery_variation_routes_with_configured_sample(self):
        router = make_router(intents=[{
            "name": "order_information",
            "samples": ["मेरा डिलीवरी कहाँ है", "डिलीवरी कब होगी"],
            "route": "workflow:honasa_order_support",
            "confidence_threshold": 0.55,
        }])

        decision = router.decide("अह मेरा डिलीवरी कहाँ है आप बता सकते हो?")

        assert decision.kind == RouteKind.WORKFLOW
        assert decision.action == "honasa_order_support"

    def test_configured_yes_beats_generic_smalltalk(self):
        router = make_router(
            intents=[{
                "name": "call_opening_response",
                "samples": ["yes", "haan", "aage badho"],
                "route": "workflow:collection_call",
                "confidence_threshold": 0.3,
            }]
        )
        decision = router.decide("Yes")
        assert decision.kind == RouteKind.WORKFLOW
        assert decision.action == "collection_call"

    def test_bare_mixed_spoken_identifier_routes_to_unique_workflow(self):
        router = make_router(intents=[{
            "name": "order_information",
            "samples": ["where is my order"],
            "route": "workflow:order_support",
            "confidence_threshold": 0.55,
            "optional_entities": ["order_id", "registered_phone"],
        }])

        decision = router.decide("Seven 0 0 1 zero zero two")

        assert decision.kind == RouteKind.WORKFLOW
        assert decision.action == "order_support"
        assert decision.intent == "order_information"
        assert decision.reason == "identifier_workflow"

    def test_identifier_route_requires_one_unambiguous_workflow(self):
        router = make_router(intents=[
            {"name": "order", "samples": [], "route": "workflow:orders",
             "optional_entities": ["order_id"]},
            {"name": "booking", "samples": [], "route": "workflow:bookings",
             "optional_entities": ["booking_id"]},
        ])

        assert router.decide("7001002").kind != RouteKind.WORKFLOW

    def test_amount_or_short_digit_does_not_start_identifier_workflow(self):
        router = make_router(intents=[{
            "name": "payment", "samples": [], "route": "workflow:payment",
            "optional_entities": ["amount"],
        }])

        assert router.decide("2000").kind != RouteKind.WORKFLOW
        assert router.decide("12").kind != RouteKind.WORKFLOW


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


class TestUserSignalClassifier:
    """Semantic signals (Hindi / Hinglish / English) that gate workflow
    transitions: a hardship statement, complaint or refusal must be
    recognized BEFORE the workflow layer picks the next scripted step."""

    def _signal(self, text):
        from shared.orchestration.router import classify_user_signal
        return classify_user_signal(text)

    def test_financial_hardship_hindi_and_hinglish(self):
        for text in (
            "पर मेरे पास पैसे नहीं हैं।",
            "नहीं यार, मेरे पास पैसे अभी नहीं हैं।",
            "mere paas paise nahi hain",
            "main abhi payment nahi kar sakta",
            "पेमेंट नहीं कर पाऊंगा",
            "salary nahi aayi hai",
            "I have no money right now",
            "I do not have money right now",
            "मैं बीमार हूँ, अस्पताल में हूँ",
        ):
            assert self._signal(text) == "hardship", text

    def test_complaint_bot_not_listening(self):
        for text in (
            "aap meri baat sun nahi rahe ho",
            "आप सुन ही नहीं रहे",
            "you are not listening to me",
            "baar baar wahi baat bol rahe ho",
            "aap samajh nahi rahe",
        ):
            assert self._signal(text) == "complaint", text

    def test_refusal_is_not_payment_intent(self):
        # "karunga" alone is a commitment — negated it must NOT be.
        for text in ("नहीं करूंगा", "main payment nahi karunga", "abhi nahi", "नहीं"):
            assert self._signal(text) in ("refusal", "hardship"), text

    def test_positive_commitment(self):
        for text in ("payment kar dunga abhi", "haan upi se kar deta hun",
                     "अभी कर दूंगा", "ready to pay"):
            assert self._signal(text) in ("payment_intent",), text

    def test_callback_and_busy(self):
        for text in ("abhi busy hun, baad mein call karna", "kal karunga call",
                     "Please call me later", "बाद में कॉल करना",
                     "मीटिंग में हूँ", "abhi baat nahi kar sakta"):
            assert self._signal(text) == "callback", text

    def test_city_name_containing_baad_is_not_a_callback(self):
        """"अहमदाबाद में" contains "बाद में" — Python's \\b forms a boundary
        after the matra, so a caller naming their city used to be routed to
        the callback close (observed live: Frankfinn seminar bot)."""
        for text in ("मैं अहमदाबाद में रहता हूँ", "अभी तो अहमदाबाद में रहता हूँ।",
                     "ahmedabaad mein rehta hoon", "बिल्कुल कर दो"):
            assert self._signal(text) != "callback", text

    def test_devanagari_haanji_is_an_affirmation(self):
        assert self._signal("हांजी") == "affirm"
        assert self._signal("हाँजी जी") == "affirm"

    def test_neutral_text_has_no_signal(self):
        assert self._signal("mausam accha hai aaj") is None
        assert self._signal("पता नहीं") is None  # "don't know" ≠ refusal

    def test_repeated_affirmation_is_one_affirm(self):
        # Natural speech repeats confirmations; they must never become
        # "unknown" utterances that earn a canned clarification.
        for text in ("yes yes", "Yes yes.", "yeah yeah", "okay okay",
                     "haan haan", "हाँ हाँ", "theek hai theek hai",
                     "yes, yes please", "ok thanks"):
            assert self._signal(text) == "affirm", text

    def test_repeated_negation_is_one_refusal(self):
        for text in ("no no", "No no.", "No. No.", "no no no",
                     "nahi nahi", "नहीं नहीं", "no, no thanks"):
            assert self._signal(text) == "refusal", text

    def test_repetition_does_not_overmatch(self):
        assert self._signal("yes yesterday") is None
        assert self._signal("no news is good news") is None

    def test_repeated_short_confirmations_route_to_chat(self):
        router = make_router()
        for text in ("Yes yes.", "No no."):
            decision = router.decide(text)
            assert decision.kind == RouteKind.CHAT, text
            assert decision.reason == "short_signal", text

    def test_workflow_decision_carries_signal(self):
        router = make_router()
        decision = router.decide(
            "mere paas paise nahi hain", active_workflow="edas_collection_call"
        )
        assert decision.kind == RouteKind.WORKFLOW
        assert decision.signal == "hardship"

    def test_intent_workflow_entry_carries_signal(self):
        router = make_router(intents=[{
            "name": "payment_difficulty",
            "route": "workflow:edas_collection_call",
            "samples": ["paise nahi", "पैसे नहीं"],
            "confidence_threshold": 0.05,
        }])
        decision = router.decide("पर मेरे पास पैसे नहीं हैं।")
        assert decision.kind == RouteKind.WORKFLOW
        assert decision.action == "edas_collection_call"
        assert decision.signal == "hardship"


class TestIntentMatchStrength:
    """Confidence measures the QUALITY of a sample match (exact utterance >
    contained phrase > ordered words with gaps > lone word), gated by each
    intent's own threshold — so risk lives in configuration: a handoff or
    destructive intent demands phrase-level evidence, and a single common
    word can never transfer a call or restart a workflow."""

    HANDOFF_INTENTS = [
        {
            "name": "refund_status",
            "samples": ["refund", "money back", "refund status",
                        "when will i get my refund"],
            "route": "handoff",
            "confidence_threshold": 0.7,
        },
        {
            "name": "cancel_booking",
            "samples": ["cancel my booking", "cancellation", "want to cancel"],
            "route": "handoff",
            "confidence_threshold": 0.7,
        },
    ]

    def test_lone_common_word_cannot_reach_a_handoff_intent(self):
        router = make_router(intents=self.HANDOFF_INTENTS)
        # "refund" appears, but the utterance clearly is NOT a refund request.
        decision = router.decide(
            "I was told there is no refund involved, is my booking confirmed"
        )
        assert decision.kind != RouteKind.HANDOFF

    def test_clear_phrase_still_reaches_the_handoff_intent(self):
        router = make_router(intents=self.HANDOFF_INTENTS)
        decision = router.decide("I want to cancel my booking")
        assert decision.kind == RouteKind.HANDOFF
        assert decision.intent == "cancel_booking"

    def test_exact_configured_phrase_always_counts(self):
        router = make_router(intents=self.HANDOFF_INTENTS)
        decision = router.decide("Refund status")
        assert decision.kind == RouteKind.HANDOFF
        assert decision.confidence >= 0.9

    def test_samples_match_whole_words_never_substrings(self):
        router = make_router(intents=[{
            "name": "call_opening_response",
            "samples": ["yes", "हाँ", "hello"],
            "route": "workflow:booking_support",
            "confidence_threshold": 0.3,
        }])
        # "yes" inside "yesterday", "हाँ" inside "कहाँ" must not fire.
        assert router.decide("I arrived yesterday evening at the hotel").kind \
            != RouteKind.WORKFLOW
        assert router.decide("मेरा होटल कहाँ है बताइए ज़रा").kind \
            != RouteKind.WORKFLOW
        # The whole word still does.
        assert router.decide("हाँ").kind == RouteKind.WORKFLOW

    def test_more_samples_do_not_dilute_a_strong_match(self):
        """The old hits/len(samples) voting punished coverage: adding samples
        weakened every single-phrase match. Strength must not depend on how
        many OTHER samples the author added."""
        few = make_router(intents=[{
            "name": "booking_confirmation", "route": "workflow:journey",
            "samples": ["confirm my booking"], "confidence_threshold": 0.55,
        }])
        many = make_router(intents=[{
            "name": "booking_confirmation", "route": "workflow:journey",
            "samples": ["confirm my booking", "booking confirmation",
                        "booking status", "is my booking confirmed",
                        "check my booking", "is my reservation confirmed",
                        "confirm my upcoming booking", "upcoming booking"],
            "confidence_threshold": 0.55,
        }])
        for router in (few, many):
            decision = router.decide("can you confirm my booking please")
            assert decision.kind == RouteKind.WORKFLOW, "phrase must fire"

    def test_buried_phrase_defers_to_higher_layers_on_high_threshold(self):
        router = make_router(intents=self.HANDOFF_INTENTS)
        decision = router.decide(
            "so as I was saying earlier my cousin might possibly want to "
            "cancel some other reservation at some point next year maybe"
        )
        # "want to cancel" is present but buried — below the 0.7 bar the
        # deterministic router must not transfer; the LLM layer decides.
        assert decision.kind != RouteKind.HANDOFF
