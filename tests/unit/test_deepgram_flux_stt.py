"""Deepgram Flux STT adapter — protocol normalization, config, and safety.

Pins the contract the voice pipeline depends on:

- bot config with ``stt.provider = "deepgram"`` builds the Flux service on
  ``/v2/listen`` with the model, thresholds and language hints resolved from
  configuration (nothing hardcoded, key from the secret reference);
- Flux TurnInfo events normalize onto the internal event contract:
  EndOfTurn → finalized TranscriptionFrame (+ detected language),
  EagerEndOfTurn → STTEagerEndOfTurnFrame, TurnResumed → STTTurnResumedFrame;
- word-confirmed barge-in: StartOfTurn while the bot is audibly speaking
  never interrupts by itself — the transcript must reach the word gate;
- input audio is coalesced to ~80 ms sends; billing counts exactly the PCM
  actually sent;
- a replayed EndOfTurn (same request_id + turn_index) is recognized as a
  duplicate by the shared final-event identity;
- TurnInfo quality metadata (per-word confidence, languages, audio window)
  reaches the transcript gate.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.services.deepgram.flux.stt import (
    DeepgramFluxSTTService,
    DeepgramFluxSTTSettings,
)
from pipecat.transcriptions.language import Language

from shared.bot_config import ResolvedBotConfig
from shared.providers.base import ProviderError
from voice_runtime.deepgram_stt import EchoDeepgramFluxSTTService
from voice_runtime.frames import STTEagerEndOfTurnFrame, STTTurnResumedFrame
from voice_runtime.pipeline import build_stt_service
from voice_runtime.stt_events import final_event_key
from voice_runtime.transcript_gate import segment_quality
from voice_runtime.turn_metrics import TurnLatencyTracker


class _RecorderStub:
    def __init__(self):
        self.events = []
        self.stt_usage = []
        self.session_id = "s-flux"

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def add_stt_usage(self, **kwargs):
        self.stt_usage.append(kwargs)


def make_service(**overrides) -> EchoDeepgramFluxSTTService:
    kwargs = dict(
        api_key="test-key",
        sample_rate=8000,
        settings=DeepgramFluxSTTSettings(
            model="flux-general-multi",
            language=None,
            eot_threshold=0.7,
            eager_eot_threshold=0.6,
            eot_timeout_ms=3000,
        ),
        recorder=_RecorderStub(),
        latency=TurnLatencyTracker(session_id="s-flux"),
        barge_in_min_words=2,
    )
    kwargs.update(overrides)
    service = EchoDeepgramFluxSTTService(**kwargs)
    service._pushed = []
    service._broadcasts = []

    async def _push(frame, direction=None):
        service._pushed.append(frame)

    async def _broadcast_frame(frame_cls, **kw):
        service._broadcasts.append(getattr(frame_cls, "__name__", str(frame_cls)))

    async def _broadcast_interruption():
        service._broadcasts.append("InterruptionFrame")

    async def _noop(*args, **kw):
        return None

    service.push_frame = _push
    service.broadcast_frame = _broadcast_frame
    service.broadcast_interruption = _broadcast_interruption
    service.start_metrics = _noop
    service.stop_processing_metrics = _noop
    return service


def end_of_turn(
    transcript="Yes, I am speaking.",
    languages=("en",),
    turn_index=3,
    request_id="req-1",
):
    words = []
    start = 10.0
    for token in transcript.split():
        words.append({
            "word": token, "confidence": 0.95,
            "start": round(start, 2), "end": round(start + 0.2, 2),
        })
        start += 0.25
    return {
        "type": "TurnInfo", "event": "EndOfTurn",
        "request_id": request_id, "sequence_id": 17, "turn_index": turn_index,
        "audio_window_start": 10.0, "audio_window_end": round(start, 2),
        "transcript": transcript, "words": words,
        "languages": list(languages), "end_of_turn_confidence": 0.9,
    }


class TestServiceCreation:
    def _config(self, settings=None, model="flux-general-multi"):
        return ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
            published=True, language="hi-IN", languages=["en-IN", "hi-IN"],
            stt={
                "provider": "deepgram", "model": model, "language": "",
                "settings": settings or {},
                "api_key_reference": "env:DEEPGRAM_API_KEY",
            },
        )

    def test_selected_provider_builds_flux_service(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
        service = build_stt_service(self._config(), sample_rate=8000)
        assert isinstance(service, EchoDeepgramFluxSTTService)
        assert service._url == "wss://api.deepgram.com/v2/listen"
        assert service._settings.model == "flux-general-multi"

    def test_v2_listen_query_carries_model_thresholds_and_hints(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
        service = build_stt_service(
            self._config(settings={
                "eot_threshold": 0.8,
                "eager_eot_threshold": 0.5,
                "eot_timeout_ms": 2000,
            }),
            sample_rate=8000,
        )
        service._sample_rate = 8000  # normally resolved at StartFrame
        query = service._build_query_string()
        assert "model=flux-general-multi" in query
        assert "sample_rate=8000" in query
        assert "encoding=linear16" in query
        assert "eot_threshold=0.8" in query
        assert "eager_eot_threshold=0.5" in query
        assert "eot_timeout_ms=2000" in query
        # Hints derived from the bot's configured languages (en-IN, hi-IN).
        assert "language_hint=en" in query
        assert "language_hint=hi" in query

    def test_explicit_language_hints_override_bot_languages(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
        service = build_stt_service(
            self._config(settings={"language_hints": ["hi"]}), sample_rate=8000,
        )
        assert service._settings.language_hints == [Language.HI]

    def test_missing_credentials_fail_closed(self, monkeypatch):
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        with pytest.raises(ProviderError) as exc:
            build_stt_service(self._config(), sample_rate=8000)
        assert exc.value.category == "auth"

    def test_junk_threshold_values_fall_back_and_clamp(self, monkeypatch):
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
        service = build_stt_service(
            self._config(settings={
                "eot_threshold": "fast",     # junk → default
                "eager_eot_threshold": 5.0,  # out of range → clamped
                "eot_timeout_ms": 100,       # below floor → clamped
            }),
            sample_rate=8000,
        )
        assert service._settings.eot_threshold == 0.7
        assert service._settings.eager_eot_threshold == 0.9
        assert service._settings.eot_timeout_ms == 500

    def test_sarvam_path_is_untouched(self, monkeypatch):
        # Regression guard: the deepgram branch must not affect Sarvam bots.
        from voice_runtime.sarvam_stt import EndpointedSarvamSTTService

        monkeypatch.setenv("SARVAM_API_KEY", "sv-test")
        config = ResolvedBotConfig(
            tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
            published=True, language="hi-IN", languages=["hi-IN"],
            stt={"provider": "sarvam", "model": "saaras:v3", "language": "",
                 "settings": {}, "api_key_reference": "env:SARVAM_API_KEY"},
        )
        service = build_stt_service(config, sample_rate=8000)
        assert isinstance(service, EndpointedSarvamSTTService)


class TestTurnInfoNormalization:
    async def test_end_of_turn_pushes_finalized_transcription_with_language(self):
        service = make_service()
        data = end_of_turn("Yes, I am speaking.", languages=("en",))
        await service._handle_message(data)
        finals = [f for f in service._pushed if isinstance(f, TranscriptionFrame)]
        assert len(finals) == 1
        assert finals[0].text == "Yes, I am speaking."
        assert finals[0].finalized is True
        assert finals[0].language == Language.EN
        assert finals[0].result is data
        assert "UserStoppedSpeakingFrame" in service._broadcasts

    async def test_hindi_turn_reports_hindi(self):
        service = make_service()
        await service._handle_message(end_of_turn("नहीं मैं नहीं करूँगा", languages=("hi",)))
        final = next(f for f in service._pushed if isinstance(f, TranscriptionFrame))
        assert final.language == Language.HI

    async def test_eager_end_of_turn_is_speculative_only(self):
        service = make_service()
        await service._handle_message({
            "type": "TurnInfo", "event": "EagerEndOfTurn",
            "request_id": "req-1", "turn_index": 3,
            "transcript": "Yes, I am speaking.",
            "words": [], "languages": ["en"],
        })
        eager = [f for f in service._pushed if isinstance(f, STTEagerEndOfTurnFrame)]
        assert len(eager) == 1
        assert eager[0].text == "Yes, I am speaking."
        assert eager[0].language == "en"
        # Interim for UI parity — but never a finalized transcription.
        assert any(isinstance(f, InterimTranscriptionFrame) for f in service._pushed)
        assert not any(isinstance(f, TranscriptionFrame) for f in service._pushed)

    async def test_turn_resumed_emits_cancellation_frame(self):
        service = make_service()
        await service._handle_message({"type": "TurnInfo", "event": "TurnResumed"})
        assert any(isinstance(f, STTTurnResumedFrame) for f in service._pushed)

    async def test_speech_end_backdated_from_word_timings(self):
        service = make_service()
        service._bytes_sent_total = int(11.0 * 8000 * 2)  # audio clock at 11 s
        data = end_of_turn("ठीक है", languages=("hi",))
        last_word_end = data["words"][-1]["end"]
        await service._handle_message(data)
        tracker = service._latency
        assert tracker.speech_stopped_at is not None
        # Backdated by (audio clock − last word end), so the endpoint span
        # includes Flux's own decision window.
        import time as _time

        lookback = 11.0 - last_word_end
        assert (_time.monotonic() - tracker.speech_stopped_at) == pytest.approx(
            lookback, abs=0.2
        )

    async def test_malformed_turn_info_is_ignored(self):
        service = make_service()
        await service._handle_message({"type": "TurnInfo", "event": 42})
        await service._handle_message({"no_type": True})
        await service._handle_message({"type": "TurnInfo", "event": "Unknown"})
        assert service._pushed == []

    async def test_fatal_error_is_recorded_for_diagnostics(self):
        service = make_service()
        with pytest.raises(Exception):
            await service._handle_message({
                "type": "Error", "error": "auth failure", "code": "401",
            })
        assert ("stt_provider_error", {
            "provider": "deepgram", "error": "auth failure", "code": "401",
        }) in service._recorder.events


class TestWordConfirmedBargeIn:
    async def test_start_of_turn_interrupts_immediately_when_bot_quiet(self):
        service = make_service()
        service._bot_audible = False
        await service._handle_start_of_turn("")
        assert "UserStartedSpeakingFrame" in service._broadcasts
        assert "InterruptionFrame" in service._broadcasts

    async def test_start_of_turn_is_held_while_bot_speaks(self):
        service = make_service()
        service._bot_audible = True
        await service._handle_start_of_turn("")
        assert service._broadcasts == []

    async def test_update_with_enough_words_confirms_the_interruption(self):
        service = make_service()
        service._bot_audible = True
        await service._handle_start_of_turn("")
        await service._handle_update("एक")
        assert service._broadcasts == []  # one word is a backchannel
        await service._handle_update("एक मिनट रुकिए")
        assert "UserStartedSpeakingFrame" in service._broadcasts
        assert "InterruptionFrame" in service._broadcasts

    async def test_single_word_backchannel_never_interrupts(self):
        service = make_service()
        service._bot_audible = True
        await service._handle_start_of_turn("")
        await service._handle_message(end_of_turn("हाँ", languages=("hi",)))
        assert "InterruptionFrame" not in service._broadcasts
        # The transcription still flows (the brain holds it until bot stops).
        assert any(isinstance(f, TranscriptionFrame) for f in service._pushed)

    async def test_full_turn_while_bot_speaks_interrupts_at_end_of_turn(self):
        service = make_service()
        service._bot_audible = True
        await service._handle_start_of_turn("")
        await service._handle_message(
            end_of_turn("मुझे कुछ पूछना है आपसे", languages=("hi",))
        )
        assert "InterruptionFrame" in service._broadcasts


class TestAudioCoalescingAndBilling:
    async def test_input_audio_is_coalesced_to_80ms_sends(self, monkeypatch):
        sent = []

        async def _parent_run(self, audio):
            sent.append(audio)
            yield None

        monkeypatch.setattr(DeepgramFluxSTTService, "run_stt", _parent_run)
        service = make_service()
        frame_20ms = b"\x01\x02" * 160  # 320 bytes @ 8 kHz
        for _ in range(3):
            async for _ in service.run_stt(frame_20ms):
                pass
        assert sent == []  # below the 80 ms chunk target (1280 bytes)
        async for _ in service.run_stt(frame_20ms):
            pass
        assert len(sent) == 1 and len(sent[0]) == 1280

    async def test_billing_counts_exactly_the_pcm_sent(self, monkeypatch):
        async def _parent_run(self, audio):
            yield None

        monkeypatch.setattr(DeepgramFluxSTTService, "run_stt", _parent_run)
        service = make_service()
        chunk_80ms = b"\x00\x01" * 640  # 1280 bytes = exactly one send
        for _ in range(5):
            async for _ in service.run_stt(chunk_80ms):
                pass
        await service._handle_message(end_of_turn())
        usage = service._recorder.stt_usage
        assert len(usage) == 1
        assert usage[0]["seconds"] == pytest.approx(0.4)  # 5 × 80 ms
        assert usage[0]["basis"] == "pcm"

    async def test_unsent_buffered_audio_is_never_billed(self, monkeypatch):
        async def _parent_run(self, audio):
            yield None

        monkeypatch.setattr(DeepgramFluxSTTService, "run_stt", _parent_run)
        service = make_service()
        async for _ in service.run_stt(b"\x00\x01" * 160):  # 20 ms, buffered
            pass
        await service._handle_message(end_of_turn())
        assert service._recorder.stt_usage == []


class TestDuplicateFinalIdentity:
    def test_replayed_end_of_turn_shares_its_identity(self):
        data = end_of_turn("नहीं नहीं करूँगा ना बोल दिया", turn_index=7)
        frame_a = TranscriptionFrame("नहीं नहीं करूँगा ना बोल दिया", "u", "t1",
                                     result=data)
        frame_b = TranscriptionFrame("नहीं नहीं करूँगा ना बोल दिया", "u", "t2",
                                     result=dict(data))
        key_a = final_event_key(frame_a, frame_a.text)
        key_b = final_event_key(frame_b, frame_b.text)
        assert key_a is not None and key_a == key_b

    def test_distinct_turns_have_distinct_identities(self):
        frame_a = TranscriptionFrame("हाँ", "u", "t1", result=end_of_turn("हाँ", turn_index=1))
        frame_b = TranscriptionFrame("हाँ", "u", "t2", result=end_of_turn("हाँ", turn_index=2))
        assert final_event_key(frame_a, "हाँ") != final_event_key(frame_b, "हाँ")


class TestFluxQualityMetadata:
    def test_turn_info_quality_reaches_the_gate(self):
        data = end_of_turn("Yes, I am speaking.", languages=("en",))
        frame = TranscriptionFrame("Yes, I am speaking.", "u", "t", result=data)
        quality = segment_quality(frame, provider="deepgram")
        assert quality.language == "en"
        assert quality.confidence == pytest.approx(0.95)
        assert quality.audio_seconds == pytest.approx(
            data["audio_window_end"] - data["audio_window_start"]
        )
