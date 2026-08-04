"""Usage capture for costing: Sarvam STT billed from the provider's own
``metrics.audio_duration`` (final responses only, request_id-deduplicated,
Decimal-precise, marked fallback when genuinely unavailable), LLM reasoning
token passthrough, and TTS character billing that never double-charges
retries/fallbacks and never charges failed generations — while interrupted
generations bill exactly the text already sent to the provider."""

import asyncio
from decimal import Decimal

from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain
from voice_runtime.recording import SessionRecorder, TurnRecord
from voice_runtime.services import EchoSTTService


def make_config(**overrides) -> ResolvedBotConfig:
    values = dict(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN", "en-IN"],
        stt={"provider": "sarvam", "model": "saaras:v3"},
        llm={"provider": "openai", "model": "gpt-4o-mini"},
        system_prompt="You are Test.",
    )
    values.update(overrides)
    return ResolvedBotConfig(**values)


def make_recorder() -> SessionRecorder:
    return SessionRecorder("vs_test", make_config())


def make_brain(recorder: SessionRecorder) -> ConversationBrain:
    brain = ConversationBrain(config=recorder.config, llm=None, recorder=recorder,
                              finalize_grace=0.01)
    brain._notified = []

    async def _push(frame, direction=None):
        pass

    async def _notify(payload):
        brain._notified.append(payload)

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)

    async def _cancel(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.cancel_task = _cancel
    return brain


def sarvam_final(text, *, duration=1.1, request_id="req_1", language="hi-IN",
                 is_final=True):
    """A TranscriptionFrame shaped like pipecat's Sarvam WS output."""
    return TranscriptionFrame(
        text=text, user_id="caller", timestamp="t", language=language,
        result={
            "type": "data",
            "data": {
                "request_id": request_id,
                "transcript": text,
                "language_code": language,
                "is_final": is_final,
                "metrics": {"audio_duration": duration, "processing_latency": 0.4},
            },
        },
    )


# ── Sarvam STT: metrics.audio_duration is the billing source of truth ───────


class TestSarvamSttCapture:
    async def test_final_audio_duration_recorded(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        await brain.process_frame(
            sarvam_final("हाँ बोलिए", duration=1.1, request_id="req_a"),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal("1.1")
        assert recorder.usage["stt_seconds"] == 1.1
        assert recorder.usage["stt_requests"] == 1
        assert recorder._stt_basis == "provider_metrics"

    async def test_duplicate_final_billed_once(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        frame = sarvam_final("हाँ", duration=0.8, request_id="req_dup")
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        await brain.process_frame(
            sarvam_final("हाँ", duration=0.8, request_id="req_dup"),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal("0.8")
        assert recorder.usage["stt_requests"] == 1

    async def test_distinct_finals_accumulate_decimal_exact(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        for i, duration in enumerate((1.1, 2.3, 0.4)):
            await brain.process_frame(
                sarvam_final(f"segment {i}", duration=duration, request_id=f"req_{i}"),
                FrameDirection.DOWNSTREAM,
            )
        # Decimal accumulation: 1.1 + 2.3 + 0.4 is EXACTLY 3.8 (floats drift).
        assert recorder._stt_seconds == Decimal("3.8")

    async def test_finals_sharing_one_connection_id_all_bill(self):
        """THE under-billing regression.

        Sarvam's ``request_id`` identifies the socket CONNECTION, not the
        utterance — one live call showed three consecutive finals
        ('ಆಯ್ತು', 'ಖಂಡಿತ.', 'मुझे धर्मेश से बात करना है।') under a single id.
        Keying dedup on it billed the FIRST final and silently discarded the
        rest, so a 17-turn call recorded stt_requests=1 and 1.728s of audio.
        The tests above all passed because they invented a unique id per final,
        which is not what the provider does.
        """
        recorder = make_recorder()
        brain = make_brain(recorder)
        for text, duration in (("पहला", 1.5), ("दूसरा", 2.0), ("तीसरा", 0.5)):
            await brain.process_frame(
                sarvam_final(text, duration=duration, request_id="one-connection"),
                FrameDirection.DOWNSTREAM,
            )
        assert recorder._stt_seconds == Decimal("4.0")
        assert recorder.usage["stt_requests"] == 3

    async def test_reconnect_mid_call_keeps_billing_every_segment(self):
        # A socket reconnect issues a NEW connection id; audio either side of
        # it is all billable.
        recorder = make_recorder()
        brain = make_brain(recorder)
        await brain.process_frame(
            sarvam_final("before", duration=1.0, request_id="conn-a"),
            FrameDirection.DOWNSTREAM,
        )
        await brain.process_frame(
            sarvam_final("after", duration=2.0, request_id="conn-b"),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal("3.0")

    async def test_identical_replayed_payload_still_bills_once(self):
        # Dedup must survive the fix: a re-delivered message has the same
        # connection id, text AND metrics, unlike a distinct utterance.
        recorder = make_recorder()
        brain = make_brain(recorder)
        for _ in range(3):
            await brain.process_frame(
                sarvam_final("हाँ", duration=0.9, request_id="conn-x"),
                FrameDirection.DOWNSTREAM,
            )
        assert recorder._stt_seconds == Decimal("0.9")
        assert recorder.usage["stt_requests"] == 1

    async def test_same_words_at_a_different_moment_are_billed_separately(self):
        # A caller genuinely repeating themselves is not a replay: the
        # provider's own per-segment measurements differ.
        recorder = make_recorder()
        brain = make_brain(recorder)
        await brain.process_frame(
            sarvam_final("हाँ", duration=0.9, request_id="conn-y"),
            FrameDirection.DOWNSTREAM,
        )
        frame = sarvam_final("हाँ", duration=1.1, request_id="conn-y")
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert recorder._stt_seconds == Decimal("2.0")

    async def test_non_final_response_not_billed(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        await brain.process_frame(
            sarvam_final("आधा", duration=0.5, request_id="req_p", is_final=False),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal(0)

    async def test_interim_frames_never_billed(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        await brain.process_frame(
            InterimTranscriptionFrame(text="मुझे", user_id="u", timestamp="t",
                                      language="hi-IN"),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal(0)

    async def test_gate_rejected_final_still_billed(self):
        # The quality gate drops a foreign-language hallucination from the
        # conversation, but Sarvam processed the audio — it is still billable.
        recorder = make_recorder()
        brain = make_brain(recorder)
        await brain.process_frame(
            sarvam_final("ஒரு கேள்வி உள்ளது", duration=1.6,
                         request_id="req_t", language="ta-IN"),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal("1.6")
        assert brain._pending_segments == []  # rejected from the conversation

    async def test_post_hangup_final_still_billed(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        brain._closing = True
        await brain.process_frame(
            sarvam_final("ठीक है", duration=0.9, request_id="req_h"),
            FrameDirection.DOWNSTREAM,
        )
        assert recorder._stt_seconds == Decimal("0.9")

    async def test_missing_metrics_not_counted(self):
        recorder = make_recorder()
        brain = make_brain(recorder)
        frame = TranscriptionFrame(
            text="हाँ", user_id="u", timestamp="t", language="hi-IN",
            result={"type": "data", "data": {"request_id": "req_x",
                                             "transcript": "हाँ"}},
        )
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert recorder._stt_seconds == Decimal(0)  # finalize applies fallback

    async def test_flat_rest_result_not_double_billed_by_brain(self):
        # EchoSTTService bills the PCM itself and attaches a FLAT result dict;
        # the brain must ignore that shape or the segment is billed twice.
        recorder = make_recorder()
        brain = make_brain(recorder)
        frame = TranscriptionFrame(
            text="hello", user_id="u", timestamp="t", language="en",
            result={"provider": "openai-whisper", "language": "en",
                    "confidence": 0.9, "audio_seconds": 1.5},
        )
        await brain.process_frame(frame, FrameDirection.DOWNSTREAM)
        assert recorder._stt_seconds == Decimal(0)


class TestEchoSttServicePcmCapture:
    async def test_rest_segments_bill_exact_pcm_duration(self):
        from shared.audio.pcm import pcm_to_wav_bytes
        from shared.providers.base import STTResult

        class _StubProvider:
            name = "openai-whisper"

            async def transcribe(self, audio, *, sample_rate=16000, language=None):
                return STTResult(text="hello there", language="en")

        recorder = make_recorder()
        service = EchoSTTService(_StubProvider(), language="en", recorder=recorder)
        wav = pcm_to_wav_bytes(b"\x00\x00" * 8000, 16000)  # exactly 0.5 s
        [frame async for frame in service.run_stt(wav)]
        assert recorder._stt_seconds == Decimal("0.5")
        assert recorder._stt_basis == "pcm"


# ── finalize: provider seconds vs marked fallback ────────────────────────────


class TestFinalizeSttEvent:
    def _captured_events(self, recorder, duration=120):
        captured = []

        def fake_record(db, **kwargs):
            captured.append(kwargs)
            return None

        recorder._record_usage_events(object(), fake_record, duration)
        return captured

    def test_provider_metrics_billed_with_basis(self):
        recorder = make_recorder()
        recorder.add_stt_usage(seconds="1.1", request_id="r1")
        recorder.add_stt_usage(seconds="2.345", request_id="r2")
        events = self._captured_events(recorder)
        stt = next(e for e in events if e["capability"] == "stt")
        assert stt["audio_seconds"] == Decimal("3.445")
        assert stt["usage_source"] == "provider"
        assert stt["usage_metadata"] == {"basis": "provider_metrics"}
        assert stt["model_code"] == "saaras:v3"

    def test_fallback_marked_estimated_when_no_duration_available(self):
        recorder = make_recorder()
        recorder.add_turn(TurnRecord(role="user", text="हाँ"))
        events = self._captured_events(recorder, duration=95)
        stt = next(e for e in events if e["capability"] == "stt")
        assert stt["audio_seconds"] == Decimal("95.000")
        assert stt["usage_source"] == "estimated"
        assert stt["usage_metadata"] == {"basis": "connection_duration"}

    def test_no_user_speech_no_stt_event(self):
        recorder = make_recorder()
        recorder.add_turn(TurnRecord(role="bot", text="नमस्ते"))
        events = self._captured_events(recorder)
        assert not [e for e in events if e["capability"] == "stt"]

    def test_llm_event_carries_reasoning_tokens(self):
        recorder = make_recorder()
        recorder.usage["llm_requests"] = 2
        recorder.usage["llm_input_tokens"] = 3000
        recorder.usage["llm_output_tokens"] = 400
        recorder.usage["llm_cached_tokens"] = 1000
        recorder.usage["llm_reasoning_tokens"] = 120
        events = self._captured_events(recorder)
        llm = next(e for e in events if e["capability"] == "llm")
        assert llm["input_tokens"] == 3000
        assert llm["cached_tokens"] == 1000
        assert llm["reasoning_tokens"] == 120
        assert llm["usage_source"] == "provider"


# ── TTS: dispatched-character billing (failed / interrupted / fallback) ─────


class _DummyTTSProvider:
    def __init__(self):
        self.sent: list[str] = []
        self.cancelled: list[str] = []

    async def synthesize_stream(self, text, *, generation_id):
        self.sent.append(text)

    async def flush(self, generation_id):
        pass

    async def finish(self, generation_id):
        pass

    async def cancel(self, generation_id):
        self.cancelled.append(generation_id)

    async def close(self):
        pass


class _RouterRecorder:
    session_id = "vs_test"

    def __init__(self):
        self.billed: list[dict] = []
        self.events: list[tuple] = []

    def add_tts_usage(self, **kwargs):
        self.billed.append(kwargs)

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    async def flush_event(self, kind, **data):
        self.events.append((kind, data))


def make_router(recorder, *, fallback=False, pause_ms=0):
    from voice_runtime.tts_router import StreamingTTSRouter

    config = {"provider": "sarvam", "model": "bulbul:v3", "voice": "anushka"}
    if fallback:
        config["fallback"] = {"provider": "elevenlabs", "model": "eleven_flash_v2_5",
                              "voice": "monika", "params": {},
                              "api_key_reference": ""}
    return StreamingTTSRouter(
        tts_config=config, language="hi-IN", pause_ms=pause_ms, recorder=recorder,
    )


def seeded_generation(router, provider, context_id="ctx-1", texts=(), dispatched=None):
    from voice_runtime.tts_router import _Generation

    state = _Generation(engine=dict(router._default_engine), provider=provider)
    for text in texts:
        state.texts.append(text)
    state.dispatched_chars = (
        sum(len(t) for t in texts) if dispatched is None else dispatched
    )
    router._generations[context_id] = state
    return state


class TestTtsDispatchBilling:
    async def test_completed_generation_bills_dispatched_characters(self):
        recorder = _RouterRecorder()
        router = make_router(recorder)
        seeded_generation(router, _DummyTTSProvider(), texts=("नमस्ते जी", "कैसे हैं"))
        await router._finalize_generation("ctx-1", failed=False)
        assert len(recorder.billed) == 1
        assert recorder.billed[0]["characters"] == len("नमस्ते जी") + len("कैसे हैं")
        assert recorder.billed[0]["provider"] == "sarvam"
        assert recorder.billed[0]["model"] == "bulbul:v3"

    async def test_failed_generation_never_billed(self):
        recorder = _RouterRecorder()
        router = make_router(recorder)
        seeded_generation(router, _DummyTTSProvider(), texts=("नमस्ते जी",))
        await router._finalize_generation("ctx-1", failed=True)
        assert recorder.billed == []
        assert ("tts_provider_used", {"provider": "sarvam", "voice": "anushka",
                                      "fallback_used": False, "failed": True}) \
            in recorder.events

    async def test_interrupted_generation_bills_only_dispatched_text(self):
        recorder = _RouterRecorder()
        router = make_router(recorder, pause_ms=400)
        provider = _DummyTTSProvider()
        state = seeded_generation(
            router, provider, texts=("पहला वाक्य", "दूसरा वाक्य"), dispatched=len("पहला वाक्य"),
        )
        state.pending.append(object())  # second sentence still queued locally
        await router.on_audio_context_interrupted("ctx-1")
        assert len(recorder.billed) == 1
        assert recorder.billed[0]["characters"] == len("पहला वाक्य")
        assert ("tts_generation_interrupted",
                {"provider": "sarvam", "voice": "anushka"}) in recorder.events
        assert "ctx-1" not in router._generations

    async def test_interrupted_before_any_dispatch_bills_nothing(self):
        recorder = _RouterRecorder()
        router = make_router(recorder)
        seeded_generation(router, _DummyTTSProvider(), texts=(), dispatched=0)
        await router.on_audio_context_interrupted("ctx-1")
        assert recorder.billed == []

    async def test_fallback_bills_final_engine_exactly_once(self):
        from shared.providers.base import ProviderError

        recorder = _RouterRecorder()
        router = make_router(recorder, fallback=True)
        primary = _DummyTTSProvider()
        state = seeded_generation(
            router, primary, texts=("नमस्ते जी",), dispatched=len("नमस्ते जी"),
        )
        fallback_provider = _DummyTTSProvider()

        async def fake_get_provider(engine, language):
            return fallback_provider

        router._get_provider = fake_get_provider
        handled = await router._try_fallback(
            "ctx-1", ProviderError("sarvam-tts", "timeout", "no audio")
        )
        assert handled is True
        assert fallback_provider.sent == ["नमस्ते जी"]  # replayed on new engine
        assert state.dispatched_chars == len("नमस्ते जी")  # reset + recounted once
        await router._finalize_generation("ctx-1", failed=False)
        assert len(recorder.billed) == 1
        assert recorder.billed[0]["provider"] == "elevenlabs"
        assert recorder.billed[0]["characters"] == len("नमस्ते जी")

    async def test_teardown_bills_inflight_dispatched_text(self):
        recorder = _RouterRecorder()
        router = make_router(recorder)
        seeded_generation(router, _DummyTTSProvider(), texts=("अलविदा",))
        await router._shutdown_providers()
        assert len(recorder.billed) == 1
        assert recorder.billed[0]["characters"] == len("अलविदा")
        assert router._generations == {}
