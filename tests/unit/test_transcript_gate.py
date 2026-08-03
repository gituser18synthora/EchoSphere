"""Transcript quality gate: background noise, sub-word fragments and
unsupported-language hallucinations must be rejected BEFORE they become turns
(no history entry, no workflow/LLM run, no stored transcript), while Hindi,
English and Hinglish/code-switched speech — including legitimate short replies
like "haan"/"nahi"/"yes"/"no"/"ok" — always passes. Provider metadata
(detected language, language probability, transcript confidence, no-speech
probability, audio duration) is used wherever the configured STT exposes it;
absent metadata the gate falls back to script analysis and fails open."""

import asyncio
import math
import os
import types

from pipecat.frames.frames import (
    EndWorkerFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.audio.pcm import pcm_to_wav_bytes
from shared.bot_config import ResolvedBotConfig
from shared.providers.base import ProviderConfig, STTResult
from voice_runtime.brain import ConversationBrain
from voice_runtime.services import EchoSTTService
from voice_runtime.transcript_gate import (
    ALLOWED_STT_LANGUAGES,
    SegmentQuality,
    assess_transcript,
    meaningful_short_reply,
    resolve_allowed_languages,
    segment_quality,
)

os.environ.setdefault("FAKE_TEST_KEY", "test-key")
KEY_REF = "env:FAKE_TEST_KEY"


def q(**kwargs) -> SegmentQuality:
    return SegmentQuality(**kwargs)


class TestAllowedSpeech:
    def test_hindi_devanagari_accepted(self):
        verdict = assess_transcript(
            "मुझे अपना लोन का पेमेंट करना है",
            q(provider="sarvam", language="hi-IN", language_probability=0.94),
        )
        assert verdict.accepted and verdict.language == "hi"

    def test_english_accepted(self):
        verdict = assess_transcript(
            "I will make the payment tomorrow morning",
            q(provider="sarvam", language="en-IN", language_probability=0.91),
        )
        assert verdict.accepted and verdict.language == "en"

    def test_hinglish_latin_accepted(self):
        assert assess_transcript(
            "haan bhai kal payment kar dunga",
            q(language="hi-IN", language_probability=0.62),
        ).accepted

    def test_code_switched_mixed_script_accepted(self):
        # Devanagari + borrowed Latin words is everyday Hinglish, never noise.
        assert assess_transcript(
            "मेरा account status क्या है", q(language="hi-IN")
        ).accepted

    def test_no_metadata_fails_open(self):
        assert assess_transcript("please repeat that", q()).accepted
        assert assess_transcript("पेमेंट कब करना है", q()).accepted

    def test_low_language_probability_alone_never_rejects_allowed(self):
        # language_probability corroborates FOREIGN labels only; hi/en with a
        # shaky detection score is still hi/en.
        assert assess_transcript(
            "ठीक है कल बात करते हैं", q(language="hi-IN", language_probability=0.2)
        ).accepted


class TestShortReplies:
    SHORT_REPLIES = ("हाँ", "नहीं", "haan", "nahi", "yes", "no", "ok", "theek hai")

    def test_short_replies_survive_subword_duration(self):
        # Lexicon protection: no minimum-length/duration rule may drop these.
        for text in self.SHORT_REPLIES:
            verdict = assess_transcript(
                text, q(language="hi-IN", audio_seconds=0.18)
            )
            assert verdict.accepted, (text, verdict)

    def test_short_replies_survive_low_language_probability(self):
        for text in self.SHORT_REPLIES:
            assert assess_transcript(
                text, q(language="hi-IN", language_probability=0.3)
            ).accepted, text

    def test_short_reply_lexicon(self):
        assert meaningful_short_reply("haan ji")
        assert meaningful_short_reply("नहीं")
        assert meaningful_short_reply("phone kaat do")  # hang-up phrases too
        assert not meaningful_short_reply("झमकड़ा")


class TestNoiseRejection:
    def test_empty_rejected(self):
        assert assess_transcript("   ", q()).reason == "empty"

    def test_no_speech_probability_rejects(self):
        verdict = assess_transcript(
            "Thank you.", q(provider="openai-whisper", no_speech_prob=0.92)
        )
        assert not verdict.accepted and verdict.reason == "no_speech"

    def test_low_confidence_final_rejected(self):
        verdict = assess_transcript(
            "हम्म हाँ अच्छा ठीक", q(provider="deepgram", confidence=0.2)
        )
        assert not verdict.accepted and verdict.reason == "low_confidence"

    def test_healthy_confidence_accepted(self):
        assert assess_transcript(
            "क्या आप दोबारा बोल सकते हैं", q(confidence=0.9)
        ).accepted

    def test_subword_audio_fragment_rejected(self):
        verdict = assess_transcript("क", q(audio_seconds=0.15))
        assert not verdict.accepted and verdict.reason == "noise_duration"

    def test_impossible_speech_rate_rejected(self):
        # Eight words "spoken" in half a second is a hallucinated sentence.
        verdict = assess_transcript(
            "मैं कल सुबह पूरा पैसा जमा कर दूंगा", q(audio_seconds=0.5)
        )
        assert not verdict.accepted and verdict.reason == "impossible_rate"

    def test_normal_speech_rate_accepted(self):
        assert assess_transcript(
            "मैं कल सुबह पूरा पैसा जमा कर दूंगा", q(audio_seconds=3.1)
        ).accepted


class TestUnsupportedLanguage:
    def test_foreign_script_hallucinations_rejected(self):
        cases = {
            "ta": "ஒரு கேள்வி உள்ளது",
            "te": "నాకు అర్థం కాలేదు",
            "bn": "আমি বুঝতে পারিনি",
            "pa": "ਮੈਨੂੰ ਸਮਝ ਨਹੀਂ ਆਇਆ",
            "ur": "مجھے سمجھ نہیں آیا",
        }
        for label, text in cases.items():
            verdict = assess_transcript(text, q(language=f"{label}-IN"))
            assert not verdict.accepted, text
            assert verdict.reason == "unsupported_script", text

    def test_foreign_script_rejected_even_without_label(self):
        # Sarvam's pipecat adapter defaults unknown labels to hi-IN; the
        # script itself must be enough to reject.
        verdict = assess_transcript("ఇది ఒక పరీక్ష వాక్యం", q(language="hi-IN"))
        assert not verdict.accepted and verdict.reason == "unsupported_script"

    def test_confident_foreign_label_on_romanized_text_rejected(self):
        # translit/codemix STT modes write everything in Latin — only the
        # label + probability reveal the language.
        verdict = assess_transcript(
            "vanakkam neenga eppadi irukkinga sollunga",
            q(language="ta-IN", language_probability=0.93),
        )
        assert not verdict.accepted and verdict.reason == "unsupported_language"

    def test_foreign_label_with_hinglish_markers_kept(self):
        # A mislabelled Hinglish utterance must never be dropped.
        assert assess_transcript(
            "haan theek hai kal karta hoon",
            q(language="ta-IN", language_probability=0.95),
        ).accepted

    def test_foreign_label_on_clear_english_kept(self):
        # Label mistakes lose to script evidence (no probability reported).
        assert assess_transcript(
            "please tell me the outstanding amount",
            q(language="ta-IN"),
        ).accepted

    def test_foreign_label_on_devanagari_kept(self):
        assert assess_transcript(
            "मुझे कल तक का समय चाहिए", q(language="ta-IN", language_probability=0.9)
        ).accepted

    def test_short_reply_with_foreign_label_kept(self):
        assert assess_transcript(
            "ok", q(language="ta-IN", language_probability=0.95)
        ).accepted


class TestSegmentQualityExtraction:
    def test_sarvam_websocket_payload(self):
        frame = TranscriptionFrame(
            "ஒரு கேள்வி", "caller", "t",
            language="hi-IN",  # pipecat maps unknown codes to HI_IN — must not mask
            result={
                "type": "data",
                "data": {
                    "transcript": "ஒரு கேள்வி",
                    "language_code": "ta-IN",
                    "language_probability": 0.88,
                    "metrics": {"audio_duration": 0.6, "processing_latency": 0.2},
                },
            },
        )
        quality = segment_quality(frame, provider="sarvam")
        assert quality.language == "ta-IN"
        assert quality.language_probability == 0.88
        assert quality.audio_seconds == 0.6
        assert quality.provider == "sarvam"

    def test_rest_flat_payload(self):
        frame = TranscriptionFrame(
            "hello", "caller", "t", language="en",
            result={
                "provider": "deepgram",
                "language": "en",
                "confidence": 0.85,
                "language_probability": None,
                "no_speech_prob": None,
                "audio_seconds": 1.25,
            },
        )
        quality = segment_quality(frame)
        assert quality.provider == "deepgram"
        assert quality.language == "en"
        assert quality.confidence == 0.85
        assert quality.audio_seconds == 1.25

    def test_frame_language_used_when_no_result(self):
        frame = TranscriptionFrame(
            "नमस्ते", "caller", "t",
            language=types.SimpleNamespace(value="hi-IN"),
        )
        assert segment_quality(frame).language == "hi-IN"

    def test_junk_metadata_ignored(self):
        frame = TranscriptionFrame(
            "hello", "caller", "t", language=None,
            result={"confidence": "high", "audio_seconds": "fast", "language": ""},
        )
        quality = segment_quality(frame)
        assert quality.confidence is None
        assert quality.audio_seconds is None
        assert quality.language is None


class TestAllowedLanguageResolution:
    def test_platform_default_is_hindi_english(self):
        assert ALLOWED_STT_LANGUAGES == frozenset({"hi", "en"})
        assert resolve_allowed_languages(None) == frozenset({"hi", "en"})
        assert resolve_allowed_languages({}) == frozenset({"hi", "en"})

    def test_locale_codes_normalize(self):
        assert resolve_allowed_languages(
            {"allowed_languages": ["hi-IN", "en-IN"]}
        ) == frozenset({"hi", "en"})

    def test_override_narrows_or_widens(self):
        assert resolve_allowed_languages({"allowed_languages": ["ta"]}) == frozenset({"ta"})

    def test_junk_override_keeps_default(self):
        for junk in ("hi", 7, [], ["", None], {"nested": True}):
            assert resolve_allowed_languages(
                {"allowed_languages": junk}
            ) == ALLOWED_STT_LANGUAGES, junk


# ── brain integration ───────────────────────────────────────────────────────


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.session_id = "s-test"
        self.usage = {"kb_searches": 0, "llm_output_tokens": 0}
        self.turns = []
        self.language = "hi-IN"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_turn(self, turn):
        self.turns.append(turn)

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))

    def event_kinds(self):
        return [kind for kind, _ in self.events]

    def events_of(self, kind):
        return [data for k, data in self.events if k == kind]


GRACE = 0.05


def make_brain(language="hi-IN", languages=("hi-IN", "en-IN"), stt=None) -> ConversationBrain:
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language=language, languages=list(languages),
        stt=stt or {"provider": "sarvam"}, system_prompt="You are Test.",
    )
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(), finalize_grace=GRACE
    )
    brain._pushed = []
    brain._notified = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify

    def _create_task(coro, name=None):
        return asyncio.get_event_loop().create_task(coro)

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.create_task = _create_task
    brain.cancel_task = _cancel_task
    return brain


def stub_turn_handler(brain):
    handled = []

    async def _handle(text):
        handled.append(text)

    brain._handle_turn = _handle
    return handled


def transcript(text, language="hi-IN", result=None):
    return TranscriptionFrame(
        text=text, user_id="u", timestamp="t", language=language, result=result
    )


async def settle_turn():
    await asyncio.sleep(GRACE * 2)
    for _ in range(3):
        await asyncio.sleep(0)


class TestBrainGating:
    async def test_foreign_hallucination_never_becomes_a_turn(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(
            transcript("ஒரு கேள்வி உள்ளது", language="ta-IN"),
            FrameDirection.DOWNSTREAM,
        )
        await settle_turn()
        assert handled == []
        assert brain._pending_segments == []
        assert brain._history == []
        assert brain._recorder.turns == []
        rejected = brain._recorder.events_of("stt_segment_rejected")
        assert rejected and rejected[0]["reason"] == "unsupported_script"

    async def test_low_confidence_final_never_becomes_a_turn(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(
            transcript(
                "हम्म कुछ भी", language="hi-IN",
                result={"provider": "deepgram", "confidence": 0.15},
            ),
            FrameDirection.DOWNSTREAM,
        )
        await settle_turn()
        assert handled == []
        rejected = brain._recorder.events_of("stt_segment_rejected")
        assert rejected and rejected[0]["reason"] == "low_confidence"
        assert rejected[0]["confidence"] == 0.15

    async def test_repeated_foreign_rejections_notify_once(self):
        brain = make_brain()
        stub_turn_handler(brain)
        for text in ("ஒரு கேள்வி உள்ளது", "தயவுசெய்து உதவுங்கள்", "இன்னொரு கேள்வி"):
            await brain.process_frame(
                transcript(text, language="ta-IN"), FrameDirection.DOWNSTREAM
            )
        await settle_turn()
        notices = [
            n for n in brain._notified if n.get("name") == "language_unsupported"
        ]
        assert len(notices) == 1 and notices[0]["language"] == "ta"
        assert len(brain._recorder.events_of("language_unsupported")) == 1

    async def test_accepted_speech_resets_unsupported_streak(self):
        brain = make_brain()
        stub_turn_handler(brain)
        await brain.process_frame(
            transcript("ஒரு கேள்வி உள்ளது", language="ta-IN"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(
            transcript("हाँ बोलिए", language="hi-IN"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(
            transcript("தயவுசெய்து உதவுங்கள்", language="ta-IN"), FrameDirection.DOWNSTREAM
        )
        await settle_turn()
        # One rejection either side of real speech — streak broken, no notice.
        assert not [
            n for n in brain._notified if n.get("name") == "language_unsupported"
        ]

    async def test_rejected_segment_does_not_corrupt_buffered_turn(self):
        from pipecat.frames.frames import (
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
        )

        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("नहीं, मेरे पास"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(
            transcript("నాకు అర్థం కాలేదు", language="te-IN"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(
            transcript("अभी पैसा नहीं है"), FrameDirection.DOWNSTREAM
        )
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await settle_turn()
        assert handled == ["नहीं, मेरे पास अभी पैसा नहीं है"]

    async def test_valid_short_replies_still_flow(self):
        for text in ("हाँ", "nahi", "ok"):
            brain = make_brain()
            handled = stub_turn_handler(brain)
            await brain.process_frame(
                transcript(
                    text,
                    result={
                        "type": "data",
                        "data": {
                            "language_code": "hi-IN",
                            "language_probability": 0.4,
                            "metrics": {"audio_duration": 0.2},
                        },
                    },
                ),
                FrameDirection.DOWNSTREAM,
            )
            await settle_turn()
            assert handled == [text], text

    async def test_hangup_still_detected_through_gate(self):
        brain = make_brain()
        await brain.process_frame(
            transcript("फोन कट करो"), FrameDirection.DOWNSTREAM
        )
        for _ in range(20):
            await asyncio.sleep(0)
        assert brain._closing is True
        assert any(isinstance(f, EndWorkerFrame) for f in brain._pushed)

    async def test_hinglish_turn_flows_end_to_end(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(
            transcript("haan bhai kal payment kar dunga", language="hi-IN"),
            FrameDirection.DOWNSTREAM,
        )
        await settle_turn()
        assert handled == ["haan bhai kal payment kar dunga"]

    async def test_per_bot_allowed_language_override(self):
        brain = make_brain(
            stt={"provider": "sarvam", "settings": {"allowed_languages": ["ta-IN"]}}
        )
        handled = stub_turn_handler(brain)
        await brain.process_frame(
            transcript("ஒரு கேள்வி உள்ளது", language="ta-IN"), FrameDirection.DOWNSTREAM
        )
        await settle_turn()
        assert handled == ["ஒரு கேள்வி உள்ளது"]


class TestPartialTranscripts:
    def interim(self, text):
        return InterimTranscriptionFrame(
            text=text, user_id="u", timestamp="t", language="hi-IN"
        )

    async def test_partials_feed_ui_only(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(self.interim("मुझे अपना"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(self.interim("मुझे अपना लोन"), FrameDirection.DOWNSTREAM)
        await settle_turn()
        # UI saw live partials; no segment, turn, history or downstream frame.
        partials = [n for n in brain._notified if n["type"] == "partial_transcript"]
        assert [p["text"] for p in partials] == ["मुझे अपना", "मुझे अपना लोन"]
        assert handled == []
        assert brain._pending_segments == []
        assert brain._history == []
        assert brain._recorder.turns == []
        assert brain._pushed == []

    async def test_final_after_partials_is_the_only_turn(self):
        brain = make_brain()
        handled = stub_turn_handler(brain)
        await brain.process_frame(self.interim("मुझे अपना"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            transcript("मुझे अपना लोन चुकाना है"), FrameDirection.DOWNSTREAM
        )
        await settle_turn()
        assert handled == ["मुझे अपना लोन चुकाना है"]

    async def test_empty_and_post_hangup_partials_dropped(self):
        brain = make_brain()
        stub_turn_handler(brain)
        await brain.process_frame(self.interim("   "), FrameDirection.DOWNSTREAM)
        assert brain._notified == []
        brain._closing = True
        await brain.process_frame(self.interim("कुछ और"), FrameDirection.DOWNSTREAM)
        assert brain._notified == []


class TestEchoSTTServiceMetadata:
    async def test_rest_frames_carry_quality_metadata(self):
        class _StubProvider:
            name = "stub-stt"

            async def transcribe(self, audio, *, sample_rate=16000, language=None):
                return STTResult(
                    text="hello there", language="en", confidence=0.9,
                    no_speech_prob=0.05,
                )

        service = EchoSTTService(_StubProvider(), language="en")
        wav = pcm_to_wav_bytes(b"\x00\x00" * 8000, 16000)  # 0.5 s of silence
        frames = [frame async for frame in service.run_stt(wav)]
        assert len(frames) == 1
        frame = frames[0]
        assert frame.text == "hello there"
        assert frame.language == "en"
        assert frame.result["provider"] == "stub-stt"
        assert frame.result["confidence"] == 0.9
        assert frame.result["no_speech_prob"] == 0.05
        assert abs(frame.result["audio_seconds"] - 0.5) < 1e-6
        # And the gate reads it back identically.
        quality = segment_quality(frame)
        assert quality.confidence == 0.9 and quality.audio_seconds == 0.5


class TestWhisperQualityMetadata:
    def _provider(self, response, model):
        from shared.providers.stt.whisper import WhisperSTT

        provider = WhisperSTT(
            ProviderConfig(provider="openai-whisper", model=model,
                           api_key_reference=KEY_REF)
        )
        calls = {}

        class _Transcriptions:
            async def create(self, **kwargs):
                calls.update(kwargs)
                return response

        provider._client = types.SimpleNamespace(
            audio=types.SimpleNamespace(transcriptions=_Transcriptions())
        )
        return provider, calls

    async def test_whisper_requests_verbose_json_and_extracts_quality(self):
        response = types.SimpleNamespace(
            text="hello there",
            language="english",
            segments=[
                types.SimpleNamespace(no_speech_prob=0.04, avg_logprob=-0.25),
                types.SimpleNamespace(no_speech_prob=0.6, avg_logprob=-0.4),
            ],
        )
        provider, calls = self._provider(response, "whisper-1")
        result = await provider.transcribe(b"\x00\x00" * 1600)
        assert calls["response_format"] == "verbose_json"
        assert result.language == "en"
        assert result.no_speech_prob == 0.04  # min across segments
        assert abs(result.confidence - math.exp(-0.4)) < 1e-9

    async def test_noise_only_response_flags_no_speech(self):
        response = types.SimpleNamespace(
            text="Thank you.",
            language="english",
            segments=[types.SimpleNamespace(no_speech_prob=0.93, avg_logprob=-1.4)],
        )
        provider, _ = self._provider(response, "whisper-1")
        result = await provider.transcribe(b"\x00\x00" * 1600)
        assert result.no_speech_prob == 0.93
        assert not assess_transcript("Thank you.", q(
            provider="openai-whisper",
            no_speech_prob=result.no_speech_prob,
            confidence=result.confidence,
        )).accepted

    async def test_gpt4o_transcribe_keeps_plain_json(self):
        response = types.SimpleNamespace(text="hello there")
        provider, calls = self._provider(response, "gpt-4o-mini-transcribe")
        result = await provider.transcribe(b"\x00\x00" * 1600)
        assert "response_format" not in calls
        assert result.text == "hello there"
        assert result.confidence is None and result.no_speech_prob is None
