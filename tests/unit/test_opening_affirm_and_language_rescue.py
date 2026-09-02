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
