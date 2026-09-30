"""Opening-confirmation workflow entry + unsupported-language re-transcription.

Regressions from Zepto demo calls (2026-09-02):

- cv_3c7483dddf2d / cv_110f4027b4ef: "Yes baby, I am speaking" / "हाँ कर रहे
  हो" answered the greeting, the classifier saw only `affirm`, and the
  configured workflow never started (chat LLM improvised filler instead).
- cv_4bf867831e01: Sarvam auto-detect labelled Hindi speech as Gujarati
  ("ઘાટ કો સભા થતો"); the gate dropped it and the caller's turn was lost.
"""

import asyncio

import pytest

from shared.orchestration.router import (
    RouteDecision,
    RouteKind,
    TurnRouter,
    leading_affirmation,
)
from shared.bot_config import ResolvedBotConfig
from shared.orchestration.intent_classifier import IntentClassification
from voice_runtime.brain import ConversationBrain

from tests.unit.test_identifier_capture_runtime import (
    _GateStub,
    _RecorderStub,
    stub_turn_handler,
    transcript,
)


OPENING_INTENTS = [
    {"name": "start_enquiries", "route": "workflow:wf_main",
     "confidence_threshold": 0.4,
     "samples": ["haan", "yes", "haan bol raha hoon", "shuru karo"]},
    {"name": "mdnd_concern", "route": "workflow:wf_main",
     "confidence_threshold": 0.5,
     "samples": ["mdnd issue", "mark delivered but not delivered"]},
    {"name": "human_handoff", "route": "handoff", "confidence_threshold": 0.7,
     "samples": ["talk to a human", "agent se baat karao"]},
]


class TestLeadingAffirmation:
    def test_natural_confirmations(self):
        for text in (
            "Yes baby, I am speaking.", "Yes, I am speaking", "हाँ कर रहे हो",
            "हाँ हाँ, मैं बोल रहा हूँ।", "haan ji boliye", "ji haan main hi hoon",
            "ok", "theek hai",
        ):
            assert leading_affirmation(text), text

    def test_contrary_or_unrelated(self):
        for text in (
            "no", "nahi", "haan nahi", "yes but I don't want this",
            "Yo baby, I am speaking.", "hello", "mera paisa kat gaya",
            "yes I want to talk to an agent",
            "Yes, I know, I know that. Can you confirm why I deduct my ₹500?",
            "ok thank you bye", "theek hai baad mein baat karte hain", "हाँ ठीक है बाय",
            "haan main kal subah dus baje customer ke ghar gaya tha aur usne "
            "mujhe bola ki guard ko de do",
        ):
            assert not leading_affirmation(text), text


class TestAffirmEntryRouting:
    def test_router_derives_the_opening_intent_from_bare_affirm_samples(self):
        router = TurnRouter(intents=OPENING_INTENTS, has_knowledge_bases=False)
        assert router.affirm_entry == ("start_enquiries", "wf_main")

    def test_no_opening_intent_without_bare_affirm_samples(self):
        router = TurnRouter(intents=OPENING_INTENTS[1:], has_knowledge_bases=False)
        assert router.affirm_entry is None
        assert router.decide("Yes, I am speaking").kind == RouteKind.CHAT

    def test_ambiguous_opening_intents_disable_the_entry(self):
        intents = OPENING_INTENTS + [{
            "name": "other_opening", "route": "workflow:wf_other",
            "confidence_threshold": 0.4, "samples": ["ok", "theek hai"],
        }]
        assert TurnRouter(intents=intents).affirm_entry is None

    def test_natural_confirmation_starts_the_configured_workflow(self):
        router = TurnRouter(intents=OPENING_INTENTS, has_knowledge_bases=False)
        for text in ("Yes baby, I am speaking.", "हाँ कर रहे हो", "हाँ हाँ, मैं बोल रहा हूँ।"):
            decision = router.decide(text)
            assert decision.kind == RouteKind.WORKFLOW, text
            assert decision.action == "wf_main"
            assert decision.intent == "start_enquiries"
            assert decision.reason == "affirm_entry_workflow"
            assert decision.signal == "affirm"

    def test_configured_samples_and_escapes_still_win(self):
        router = TurnRouter(intents=OPENING_INTENTS, has_knowledge_bases=False)
        assert router.decide("haan").reason == "intent_workflow"
        assert router.decide("yes I want to talk to a human").kind == RouteKind.HANDOFF
        assert router.decide("no").kind == RouteKind.CHAT

    def test_entry_is_withheld_once_a_workflow_has_run(self):
        router = TurnRouter(intents=OPENING_INTENTS, has_knowledge_bases=False)
        decision = router.decide("theek hai, yes", allow_affirm_entry=False)
        assert decision.kind == RouteKind.CHAT

    def test_active_workflow_keeps_consuming_the_turn(self):
        router = TurnRouter(intents=OPENING_INTENTS, has_knowledge_bases=False)
        decision = router.decide("Yes, I am speaking", active_workflow="wf_main")
        assert decision.reason == "active_workflow:wf_main"


def make_brain(*, gate=None, batch_transcriber=None, intents=None):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN", "en-IN"],
        stt={"provider": "sarvam", "settings": {}},
        system_prompt="You are Test.", intents=intents or [],
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        finalize_grace=0.05, finalize_settle=0.02,
        complete_endpoint=0.05, short_reply_endpoint=0.05,
        audio_gate=gate, batch_transcriber=batch_transcriber,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel_task
    return brain


class TestBrainAffirmEntry:
    def test_llm_affirm_without_intent_enters_the_opening_workflow(self):
        brain = make_brain(intents=OPENING_INTENTS)
        chat = RouteDecision(kind=RouteKind.CHAT, confidence=0.6, reason="default_chat")
        classification = IntentClassification(
            intent=None, signal="affirm", confidence=0.9, source="llm",
        )
        decision = brain._apply_classification(chat, classification)
        assert decision.kind == RouteKind.WORKFLOW
        assert decision.action == "wf_main"
        assert decision.reason == "llm_affirm_entry_workflow"

    def test_llm_affirm_after_a_workflow_ran_stays_chat(self):
        brain = make_brain(intents=OPENING_INTENTS)
        brain._workflow_ever_routed = True
        chat = RouteDecision(kind=RouteKind.CHAT, confidence=0.6, reason="default_chat")
        classification = IntentClassification(
            intent=None, signal="affirm", confidence=0.9, source="llm",
        )
        assert brain._apply_classification(chat, classification) is chat

    def test_llm_affirm_without_opening_intent_stays_chat(self):
        brain = make_brain(intents=OPENING_INTENTS[1:])
        chat = RouteDecision(kind=RouteKind.CHAT, confidence=0.6, reason="default_chat")
        classification = IntentClassification(
            intent=None, signal="affirm", confidence=0.9, source="llm",
        )
        assert brain._apply_classification(chat, classification) is chat


GUJARATI_MISLABEL = "ઘાટ કો સભા થતો."
HINDI_RECOVERED = "गार्ड को सौंप दिया था।"


async def _frame_in(brain, frame):
    await brain._on_transcription(frame)
    await asyncio.sleep(0.15)


class TestUnsupportedLanguageRescue:
    def test_retention_is_armed_only_with_a_batch_transcriber(self):
        async def _batch(pcm, rate, language):
            return ""

        assert make_brain(gate=_GateStub(), batch_transcriber=_batch)._audio_gate.retention_enabled
        assert not make_brain(gate=_GateStub())._audio_gate.retention_enabled

    @pytest.mark.asyncio
    async def test_mislabelled_hindi_is_retranscribed_and_dispatched(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append((len(pcm), rate, language))
            return HINDI_RECOVERED

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)  # 1 s of audio

        await _frame_in(brain, transcript(
            GUJARATI_MISLABEL, language="gu-IN", language_code="gu-IN",
            language_probability=0.36,
        ))

        assert calls == [(32000, 16000, "hi-IN")]
        assert handled == [HINDI_RECOVERED]
        kinds = brain._recorder.event_kinds()
        assert "unsupported_language_retranscribed" in kinds
        assert "stt_segment_rejected" not in kinds
        rescued = brain._recorder.events_of("unsupported_language_retranscribed")[0]
        assert rescued["detected"] == "gu-IN"
        assert rescued["recovered"] == HINDI_RECOVERED

    @pytest.mark.asyncio
    async def test_rescue_that_fails_the_gate_still_rejects(self):
        async def _batch(pcm, rate, language):
            return "ਓਕੇ ਜੀ ਹਾਂ ਬਿਲਕੁਲ ਠੀਕ ਹੈ ਜੀ"  # still not hi/en

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)

        await _frame_in(brain, transcript(
            GUJARATI_MISLABEL, language="gu-IN", language_code="gu-IN",
            language_probability=0.36,
        ))

        assert handled == []
        kinds = brain._recorder.event_kinds()
        assert "unsupported_language_retranscribe_failed" in kinds
        assert "stt_segment_rejected" in kinds

    @pytest.mark.asyncio
    async def test_no_rescue_without_retained_audio_or_transcriber(self):
        gate = _GateStub()
        brain = make_brain(gate=gate)
        handled = stub_turn_handler(brain)
        await _frame_in(brain, transcript(
            GUJARATI_MISLABEL, language="gu-IN", language_code="gu-IN",
            language_probability=0.36,
        ))
        assert handled == []
        assert "stt_segment_rejected" in brain._recorder.event_kinds()
        assert "unsupported_language_retranscribe_attempted" not in brain._recorder.event_kinds()

    @pytest.mark.asyncio
    async def test_provider_failure_falls_back_to_rejection(self):
        async def _batch(pcm, rate, language):
            raise RuntimeError("boom")

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript(
            GUJARATI_MISLABEL, language="gu-IN", language_code="gu-IN",
            language_probability=0.36,
        ))
        assert handled == []
        failed = brain._recorder.events_of("unsupported_language_retranscribe_failed")
        assert failed and failed[0]["reason"] == "provider_error"


# ── genuine unsupported language vs a repeated misdetection ─────────────────
# Live 2026-09-17: a Kannada caller (vs_fWRbAKI1UBg7Usl0B-5GS6Pk) was labelled
# kn-IN three times in a row; a Hindi caller's misdetections hop between
# labels (pa → gu → bn in vs__3YgHMb2sJ0-Nc-ukF3ipkq2). Once the rescue has
# fired for a label, a second confident segment with the SAME label is the
# caller's real language: no rescue, and the caller is TOLD which languages
# the bot speaks instead of hearing silence.

KANNADA_YES = "ಹೌದು."


def _spoken(brain):
    spoken = []

    async def _say(text, **kwargs):
        spoken.append(text)
        return None

    brain._say = _say
    return spoken


class TestRepeatedUnsupportedLabel:
    @pytest.mark.asyncio
    async def test_second_confident_same_label_is_not_rescued_and_notice_is_spoken(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return "हाँ हाँ।"

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        handled = stub_turn_handler(brain)
        spoken = _spoken(brain)

        gate.retained = (b"\x00\x01" * 8000, 8000)
        await _frame_in(brain, transcript(KANNADA_YES, language="kn-IN",
                                          language_code="kn-IN", language_probability=0.69))
        assert calls == ["hi-IN"] and handled == ["हाँ हाँ।"]
        assert brain._unsupported_streak == {"kn": 1}

        gate.retained = (b"\x00\x01" * 8000, 8000)
        await _frame_in(brain, transcript(KANNADA_YES, language="kn-IN",
                                          language_code="kn-IN", language_probability=0.63))
        kinds = brain._recorder.event_kinds()
        assert calls == ["hi-IN"], "no second rescue for the same confident label"
        assert "unsupported_language_rescue_skipped" in kinds
        assert "stt_segment_rejected" in kinds
        assert "language_unsupported" in kinds
        assert "language_unsupported_notice_spoken" in kinds
        assert len(spoken) == 1
        assert "हिंदी या अंग्रेज़ी" in spoken[0]
        assert "{languages}" not in spoken[0]
        assert handled == ["हाँ हाँ।"]

        # Spoken once per call, even if the caller keeps going.
        gate.retained = (b"\x00\x01" * 8000, 8000)
        await _frame_in(brain, transcript(KANNADA_YES, language="kn-IN",
                                          language_code="kn-IN", language_probability=0.9))
        assert len(spoken) == 1

    @pytest.mark.asyncio
    async def test_alternating_labels_keep_being_rescued(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return HINDI_RECOVERED

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        handled = stub_turn_handler(brain)
        spoken = _spoken(brain)
        for label, text in (("gu-IN", GUJARATI_MISLABEL), ("pa-IN", "ਹਾਂਜੀ ਹੋ ਰਹੀ ਹੈ।"),
                            ("bn-IN", "যাও তাহলে।")):
            gate.retained = (b"\x00\x01" * 8000, 8000)
            await _frame_in(brain, transcript(text, language=label, language_code=label,
                                              language_probability=0.85))
        assert calls == ["hi-IN"] * 3
        assert handled == [HINDI_RECOVERED] * 3
        assert spoken == []
        assert "language_unsupported" not in brain._recorder.event_kinds()

    @pytest.mark.asyncio
    async def test_low_probability_repeat_is_still_rescued(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return HINDI_RECOVERED

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        stub_turn_handler(brain)
        spoken = _spoken(brain)
        for _ in range(2):
            gate.retained = (b"\x00\x01" * 8000, 8000)
            await _frame_in(brain, transcript(GUJARATI_MISLABEL, language="gu-IN",
                                              language_code="gu-IN", language_probability=0.25))
        assert calls == ["hi-IN", "hi-IN"]
        assert spoken == []

    @pytest.mark.asyncio
    async def test_supported_speech_clears_the_label_memory(self):
        async def _batch(pcm, rate, language):
            return HINDI_RECOVERED

        gate = _GateStub()
        brain = make_brain(gate=gate, batch_transcriber=_batch)
        stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 8000, 8000)
        await _frame_in(brain, transcript(KANNADA_YES, language="kn-IN",
                                          language_code="kn-IN", language_probability=0.7))
        assert brain._unsupported_streak == {"kn": 1}
        await _frame_in(brain, transcript("हाँ जी बोल रहा हूँ", language="hi-IN",
                                          language_code="hi-IN"))
        assert brain._unsupported_streak == {}

    def test_language_names_follow_the_conversation_language(self):
        brain = make_brain()
        assert brain._supported_language_names() == "हिंदी या अंग्रेज़ी"
        brain._conversation_language = "en-IN"
        assert brain._supported_language_names() == "Hindi or English"


# ── rescue language: Indic-script mislabels re-transcribe in the bot's own
# Indic language, never a blind mapping to Hindi ─────────────────────────────
# Live bot_aba8f9101217 (Haier, default en-IN, languages en-IN + hi-IN,
# 2026-09-30): Sarvam labelled short Hindi turns pa-IN/od-IN/ta-IN and the
# rescue re-transcribed them pinned to the CONVERSATION language en-IN —
# "ਨਹੀਂ ਮੈਂ ਰੋਹਨ ਨਹੀਂ ਮੈਂ" → "Name and one name". Evidence order under test:
# Indic conversation language > caller's prior detected language > single
# configured language of the same script family > sole Indic language for a
# short cross-family interjection > conversation language (unchanged).

GURMUKHI_MISLABEL = "ਨਹੀਂ ਮੈਂ ਰੋਹਨ ਨਹੀਂ ਮੈਂ।"
GURMUKHI_LONG = "ਨਹੀਂ ਨਹੀਂ ਮੈਂ ਤਾਂ ਰੋਹਨ ਨਹੀਂ ਹਾਂ ਜੀ ਗਲਤ ਨੰਬਰ ਲੱਗ ਗਿਆ ਹੈ ਤੁਹਾਡਾ"
TAMIL_SHORT = "ஆ."
TAMIL_LONG = "ஆமாம் நான் ரோஹன் பேசுகிறேன் சார் நீங்கள் யார் பேசுறீங்க இப்போ"
KANNADA_SHORT = "ಹೌದು ಹೌದು"
HINDI_RESCUED = "नहीं मैं रोहन नहीं हूँ।"
ENGLISH_RESCUED = "No, I am not Rohan."


def make_brain_with_language(
    *, language, languages, gate=None, batch_transcriber=None, stt_settings=None,
):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=languages,
        stt={"provider": "sarvam", "settings": stt_settings or {}},
        system_prompt="You are Test.", intents=[],
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        finalize_grace=0.05, finalize_settle=0.02,
        complete_endpoint=0.05, short_reply_endpoint=0.05,
        audio_gate=gate, batch_transcriber=batch_transcriber,
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel_task
    return brain


def _recording_batch(calls, reply):
    async def _batch(pcm, rate, language):
        calls.append(language)
        return reply
    return _batch


class TestIndicScriptFamilies:
    def test_script_family_detection(self):
        from voice_runtime.transcript_gate import indic_script_family

        assert indic_script_family(GURMUKHI_MISLABEL) == "northern"
        assert indic_script_family("হ্যাঁ।") == "northern"          # Bengali
        assert indic_script_family("ଆଖିରେ ଦେଖିଲେ।") == "northern"   # Odia
        assert indic_script_family("हाँ ok") == "northern"          # Devanagari-dominant
        assert indic_script_family(TAMIL_SHORT) == "dravidian"
        assert indic_script_family(KANNADA_SHORT) == "dravidian"
        assert indic_script_family("ఉమ్") == "dravidian"             # Telugu
        assert indic_script_family("Yes I am Rohan") is None
        assert indic_script_family("haan ji bol raha hoon") is None  # romanized
        assert indic_script_family("") is None
        assert indic_script_family("نہیں") is None                  # Urdu: no evidence

    def test_language_families(self):
        from voice_runtime.transcript_gate import indic_language, language_family

        assert indic_language("hi-IN") and indic_language("ta-IN") and indic_language("mr")
        assert not indic_language("en-IN") and not indic_language("ur-IN")
        assert not indic_language(None)
        assert language_family("hi-IN") == "northern"
        assert language_family("bn-IN") == "northern"
        assert language_family("ml-IN") == "dravidian"
        assert language_family("en-IN") is None


class TestRescueLanguage:
    def test_hindi_english_bot(self):
        bot = make_brain_with_language(language="en-IN", languages=["en-IN", "hi-IN"])
        # Same script family as Hindi → the configured Hindi, any length.
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("hi-IN", "script_family")
        assert bot._rescue_language(GURMUKHI_LONG) == ("hi-IN", "script_family")
        # Cross-family: only a short interjection on the sole Indic language.
        assert bot._rescue_language(TAMIL_SHORT) == ("hi-IN", "sole_indic_short")
        assert bot._rescue_language(KANNADA_SHORT) == ("hi-IN", "sole_indic_short")
        assert bot._rescue_language(TAMIL_LONG) == ("en-IN", "conversation")
        # Latin text keeps the conversation language: nothing is guessed.
        assert bot._rescue_language("hello there") == ("en-IN", "conversation")

    def test_hindi_conversation_is_kept(self):
        bot = make_brain_with_language(language="hi-IN", languages=["hi-IN", "en-IN"])
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("hi-IN", "conversation")
        assert bot._rescue_language(TAMIL_LONG) == ("hi-IN", "conversation")

    def test_english_only_bot_is_unchanged(self):
        bot = make_brain_with_language(language="en-IN", languages=["en-IN"])
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("en-IN", "conversation")
        assert bot._rescue_language(TAMIL_SHORT) == ("en-IN", "conversation")

    def test_multilingual_bot_never_forces_hindi(self):
        # Tamil established by the caller (conversation switched): kept.
        tamil_call = make_brain_with_language(
            language="ta-IN", languages=["en-IN", "hi-IN", "ta-IN"],
        )
        assert tamil_call._rescue_language(KANNADA_SHORT) == ("ta-IN", "conversation")
        assert tamil_call._rescue_language(GURMUKHI_MISLABEL) == ("ta-IN", "conversation")
        # Malayalam bot: Malayalam conversation kept, Tamil-script mislabel of
        # a Malayalam caller goes to Malayalam (same family), never Hindi.
        ml_bot = make_brain_with_language(language="ml-IN", languages=["ml-IN", "en-IN"])
        assert ml_bot._rescue_language(TAMIL_LONG) == ("ml-IN", "conversation")
        ml_bot_en = make_brain_with_language(language="en-IN", languages=["en-IN", "ml-IN"])
        assert ml_bot_en._rescue_language(TAMIL_LONG) == ("ml-IN", "script_family")
        assert ml_bot_en._rescue_language(GURMUKHI_MISLABEL) == ("ml-IN", "sole_indic_short")
        assert ml_bot_en._rescue_language(GURMUKHI_LONG) == ("en-IN", "conversation")
        # Marathi bot: a Gurmukhi mislabel goes to Marathi, not Hindi.
        mr_bot = make_brain_with_language(language="en-IN", languages=["en-IN", "mr-IN"])
        assert mr_bot._rescue_language(GURMUKHI_MISLABEL) == ("mr-IN", "script_family")

    def test_trilingual_bot_uses_family_then_prior_turn(self):
        bot = make_brain_with_language(
            language="en-IN", languages=["en-IN", "hi-IN", "ta-IN"],
        )
        # Northern script → Hindi is the only northern language configured.
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("hi-IN", "script_family")
        # Dravidian script → Tamil is the only Dravidian language configured.
        assert bot._rescue_language(KANNADA_SHORT) == ("ta-IN", "script_family")
        # The caller's last dispatched turn was detected as Tamil: that wins.
        bot._last_turn_detected_language = "ta-IN"
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("ta-IN", "prior_turn")
        bot._last_turn_detected_language = "hi-IN"
        assert bot._rescue_language(KANNADA_SHORT) == ("hi-IN", "prior_turn")

    def test_ambiguous_family_keeps_the_conversation_language(self):
        # Hindi AND Bengali configured: a Gurmukhi mislabel fits both.
        bot = make_brain_with_language(
            language="en-IN", languages=["en-IN", "hi-IN", "bn-IN"],
        )
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("en-IN", "conversation")
        bot._last_turn_detected_language = "bn-IN"
        assert bot._rescue_language(GURMUKHI_MISLABEL) == ("bn-IN", "prior_turn")

    @pytest.mark.asyncio
    async def test_english_default_bot_rescues_gurmukhi_in_hindi(self):
        calls = []
        gate = _GateStub()
        brain = make_brain_with_language(
            language="en-IN", languages=["en-IN", "hi-IN"], gate=gate,
            batch_transcriber=_recording_batch(calls, HINDI_RESCUED),
        )
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 8000, 8000)  # 1 s of telephony audio

        await _frame_in(brain, transcript(
            GURMUKHI_MISLABEL, language="pa-IN", language_code="pa-IN",
            language_probability=0.59,
        ))

        assert calls == ["hi-IN"]
        assert handled == [HINDI_RESCUED]
        attempted = brain._recorder.events_of("unsupported_language_retranscribe_attempted")[0]
        assert attempted["language"] == "hi-IN"
        assert attempted["basis"] == "script_family"
        rescued = brain._recorder.events_of("unsupported_language_retranscribed")[0]
        assert rescued["language"] == "hi-IN"
        assert rescued["recovered"] == HINDI_RESCUED
        assert "stt_segment_rejected" not in brain._recorder.event_kinds()

    @pytest.mark.asyncio
    async def test_long_tamil_on_a_hindi_bot_keeps_the_old_path(self):
        calls = []
        gate = _GateStub()
        brain = make_brain_with_language(
            language="en-IN", languages=["en-IN", "hi-IN"], gate=gate,
            batch_transcriber=_recording_batch(calls, ENGLISH_RESCUED),
        )
        stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 8000, 8000)

        await _frame_in(brain, transcript(
            TAMIL_LONG, language="ta-IN", language_code="ta-IN",
            language_probability=0.9,
        ))

        assert calls == ["en-IN"]
        attempted = brain._recorder.events_of("unsupported_language_retranscribe_attempted")[0]
        assert attempted["basis"] == "conversation"

    @pytest.mark.asyncio
    async def test_english_only_bot_keeps_the_conversation_language(self):
        calls = []
        gate = _GateStub()
        brain = make_brain_with_language(
            language="en-IN", languages=["en-IN"], gate=gate,
            batch_transcriber=_recording_batch(calls, ENGLISH_RESCUED),
        )
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 8000, 8000)

        await _frame_in(brain, transcript(
            GURMUKHI_MISLABEL, language="pa-IN", language_code="pa-IN",
            language_probability=0.59,
        ))

        assert calls == ["en-IN"]
        assert handled == [ENGLISH_RESCUED]

    @pytest.mark.asyncio
    async def test_tamil_conversation_on_multilingual_bot_is_rescued_in_tamil(self):
        calls = []
        gate = _GateStub()
        brain = make_brain_with_language(
            language="ta-IN", languages=["en-IN", "hi-IN", "ta-IN"], gate=gate,
            batch_transcriber=_recording_batch(calls, "ஆமாம் நான் தான் பேசுறேன்"),
        )
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 8000, 8000)

        await _frame_in(brain, transcript(
            KANNADA_SHORT, language="kn-IN", language_code="kn-IN",
            language_probability=0.7,
        ))

        assert calls == ["ta-IN"]
        assert handled == ["ஆமாம் நான் தான் பேசுறேன்"]

    @pytest.mark.asyncio
    async def test_hindi_conversation_is_unchanged(self):
        calls = []
        gate = _GateStub()
        brain = make_brain_with_language(
            language="hi-IN", languages=["hi-IN", "en-IN"], gate=gate,
            batch_transcriber=_recording_batch(calls, HINDI_RESCUED),
        )
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 8000, 8000)

        await _frame_in(brain, transcript(
            GURMUKHI_MISLABEL, language="pa-IN", language_code="pa-IN",
            language_probability=0.59,
        ))

        assert calls == ["hi-IN"]
        assert handled == [HINDI_RESCUED]
