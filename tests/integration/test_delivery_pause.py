"""Sentence-pause and delivery-speed behavior of the live TTS services.

Runs the StreamingTTSRouter (pause mode) and the segmented EchoTTSService in
real Pipecat pipelines against the mock provider servers, asserting:

- deterministic PCM silence of sample_rate * pause_ms / 1000 * 2 bytes
  BETWEEN sentences only — never before the first, never after the last;
- barge-in discards pending silence and queued sentences;
- per-language and fallback engines inherit the canonical delivery settings
  (speed mapped to Sarvam ``pace`` / ElevenLabs ``speed``) and the pauses;
- legacy tts_settings speed values are overridden by the canonical speed.
"""

import asyncio

import pytest
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TTSAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

import shared.providers.tts.elevenlabs_ws as elevenlabs_ws
import shared.providers.tts.sarvam_ws as sarvam_ws
from shared.audio.pcm import silence_pcm
from shared.bot_config import ResolvedBotConfig
from shared.providers.base import ProviderConfig
from shared.providers.factory import get_tts_provider
from voice_runtime.frames import SwitchVoiceLanguageFrame
from voice_runtime.recording import SessionRecorder
from voice_runtime.services import EchoTTSService
from voice_runtime.tts_router import StreamingTTSRouter
from tests.mock_tts_servers import API_KEY, PCM_CHUNK, MockElevenLabsServer, MockSarvamTTSServer

pytestmark = pytest.mark.integration

SAMPLE_RATE = 16000
PAUSE_MS = 350
PAUSE_BYTES = len(silence_pcm(SAMPLE_RATE, PAUSE_MS))  # 11200 @ 16 kHz


class AudioCollector(FrameProcessor):
    """Records every audio frame's payload for boundary/silence assertions."""

    def __init__(self):
        super().__init__()
        self.audio: list[bytes] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.audio.append(frame.audio)
        await self.push_frame(frame, direction)

    def silence_frames(self) -> list[bytes]:
        return [a for a in self.audio if a and a == b"\x00" * len(a)]

    def voiced_frames(self) -> list[bytes]:
        return [a for a in self.audio if a and a != b"\x00" * len(a)]


def make_recorder(name: str) -> SessionRecorder:
    config = ResolvedBotConfig(
        tenant_id="tn-001", bot_id="bot-101", bot_name="PauseTest",
        version="v1", published=True,
    )
    return SessionRecorder(name, config)


def sarvam_config(*, language_map=None, fallback=None, settings=None):
    return {
        "provider": "sarvam", "model": "bulbul:v3", "voice": "shubh",
        "settings": settings if settings is not None else {"min_buffer_size": 40},
        "api_key_reference": "env:TEST_SARVAM_KEY",
        "language_map": language_map or {},
        "fallback": fallback,
    }


def eleven_engine(ref="env:TEST_EL_KEY"):
    return {"provider": "elevenlabs", "model": "eleven_flash_v2_5",
            "voice": "elvoice1", "params": {}, "api_key_reference": ref}


async def run_router(router, collector, feeder, timeout=25):
    pipeline = Pipeline([router, collector])
    worker = PipelineWorker(
        pipeline, params=PipelineParams(enable_metrics=False,
                                        audio_out_sample_rate=SAMPLE_RATE),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    runner = WorkerRunner(handle_sigint=False)
    feed_task = asyncio.create_task(feeder(worker))
    await asyncio.wait_for(runner.run(worker), timeout=timeout)
    await feed_task


async def speak_turn(worker, sentences, settle=1.2):
    await worker.queue_frame(LLMFullResponseStartFrame())
    for sentence in sentences:
        await worker.queue_frame(TextFrame(sentence))
    await worker.queue_frame(LLMFullResponseEndFrame())
    await asyncio.sleep(settle)
    await worker.queue_frame(BotStoppedSpeakingFrame())
    await asyncio.sleep(0.1)


class TestRouterSentencePause:
    async def test_silence_between_sentences_only(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=2) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            recorder = make_recorder("vs_pause_happy")
            router = StreamingTTSRouter(
                tts_config=sarvam_config(), language="hi-IN",
                pause_ms=PAUSE_MS, sample_rate=SAMPLE_RATE, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, [
                    "First sentence here. ", "Second sentence now. ",
                    "And a third sentence. ",
                ])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        # Both providers received one dispatch per sentence, in order.
        assert [t.strip() for t in server.texts()] == [
            "First sentence here.", "Second sentence now.", "And a third sentence.",
        ]
        # Exactly sentences-1 gaps of the configured byte length.
        silences = collector.silence_frames()
        assert len(silences) == 2
        assert all(len(s) == PAUSE_BYTES for s in silences)
        # Never before the first sentence, never after the last one.
        assert collector.audio and collector.audio[0] != b"\x00" * len(collector.audio[0])
        assert collector.audio[-1] != b"\x00" * len(collector.audio[-1])
        # Voiced audio still arrived per sentence (2 chunks × 3 sentences).
        assert len(collector.voiced_frames()) == 6
        # One persistent provider connection despite per-sentence dispatch.
        assert server.connections == 1

    async def test_single_sentence_turn_has_no_silence(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=2) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            router = StreamingTTSRouter(
                tts_config=sarvam_config(), language="hi-IN",
                pause_ms=PAUSE_MS, sample_rate=SAMPLE_RATE,
                recorder=make_recorder("vs_pause_single"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["Only one sentence in this reply. "])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert collector.silence_frames() == []
        assert len(collector.voiced_frames()) == 2

    async def test_barge_in_cancels_pending_silence_and_sentences(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=2, first_chunk_delay=0.4) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            recorder = make_recorder("vs_pause_barge")
            router = StreamingTTSRouter(
                tts_config=sarvam_config(), language="hi-IN",
                pause_ms=PAUSE_MS, sample_rate=SAMPLE_RATE, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await worker.queue_frame(LLMFullResponseStartFrame())
                await worker.queue_frame(TextFrame("A first sentence to interrupt. "))
                await worker.queue_frame(TextFrame("A queued second sentence. "))
                await worker.queue_frame(LLMFullResponseEndFrame())
                await asyncio.sleep(0.15)  # before the delayed first chunk
                await worker.queue_frame(InterruptionFrame())
                await asyncio.sleep(0.8)   # anything late must be rejected
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        # No audio and — critically — no orphaned pause silence leaked out.
        assert collector.audio == []
        # The queued second sentence was never dispatched after the barge-in.
        assert all("queued second" not in t for t in server.texts()[1:])

    async def test_per_language_and_fallback_engines_get_pauses_and_speed(
        self, monkeypatch
    ):
        """The pause + canonical speed follow the generation onto whichever
        engine speaks: per-language ElevenLabs engine and (transient-failure)
        fallback both pause identically and receive the mapped speed."""
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=1) as sarvam_server, \
                MockElevenLabsServer(chunks=1) as eleven_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", eleven_server.url)
            recorder = make_recorder("vs_pause_multiengine")
            router = StreamingTTSRouter(
                tts_config=sarvam_config(language_map={"en-US": eleven_engine()}),
                language="hi-IN", speed=1.2, pause_ms=PAUSE_MS,
                sample_rate=SAMPLE_RATE, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते जी। ", "कैसे मदद करूँ? "])
                await worker.queue_frame(SwitchVoiceLanguageFrame(language="en-US"))
                await speak_turn(worker, ["Hello there. ", "How can I help you today? "])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        # One inter-sentence gap per two-sentence turn, on each engine.
        silences = collector.silence_frames()
        assert len(silences) == 2
        assert all(len(s) == PAUSE_BYTES for s in silences)
        # Canonical speed reached both engines in their native form.
        assert sarvam_server.configs and all(
            c.get("pace") == 1.2 for c in sarvam_server.configs
        )
        assert eleven_server.inits and all(
            i.get("voice_settings", {}).get("speed") == 1.2
            for i in eleven_server.inits
        )

    async def test_fallback_engine_keeps_pauses(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockSarvamTTSServer(behavior="error_message") as sarvam_server, \
                MockElevenLabsServer(chunks=2) as eleven_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", eleven_server.url)
            recorder = make_recorder("vs_pause_fallback")
            router = StreamingTTSRouter(
                tts_config=sarvam_config(fallback=eleven_engine()),
                language="hi-IN", pause_ms=PAUSE_MS,
                sample_rate=SAMPLE_RATE, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["Primary fails here. ", "Second sentence. "],
                                 settle=2.0)
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        fallback_events = [e for e in recorder.events if e["kind"] == "tts_fallback"]
        assert fallback_events and fallback_events[0]["to_provider"] == "elevenlabs"
        # Both sentences replayed on the fallback engine, one gap between them.
        joined = " ".join(eleven_server.texts())
        assert "Primary fails here." in joined and "Second sentence." in joined
        silences = collector.silence_frames()
        assert len(silences) == 1 and len(silences[0]) == PAUSE_BYTES
        assert len(collector.voiced_frames()) == 4

    async def test_legacy_settings_speed_is_overridden(self, monkeypatch):
        """A stale ttsSettings.pace must lose to the canonical Delivery speed."""
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=1) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            router = StreamingTTSRouter(
                tts_config=sarvam_config(settings={"pace": 0.6, "speed": 0.6}),
                language="hi-IN", speed=1.4, pause_ms=0,
                sample_rate=SAMPLE_RATE,
                recorder=make_recorder("vs_speed_override"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["Speed check sentence. "])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert server.configs and server.configs[0]["pace"] == 1.4
        assert "speed" not in server.configs[0]

    async def test_zero_pause_keeps_continuous_streaming(self, monkeypatch):
        """pause_ms=0 keeps the pre-existing single-generation flow (one flush
        per turn, no per-sentence finals) — nothing regresses for bots that
        disable the pause."""
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=2) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            router = StreamingTTSRouter(
                tts_config=sarvam_config(), language="hi-IN",
                pause_ms=0, sample_rate=SAMPLE_RATE,
                recorder=make_recorder("vs_pause_off"),
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["Sentence one. ", "Sentence two. "])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        flushes = [m for m in server.received if m.get("type") == "flush"]
        assert len(flushes) == 1
        assert collector.silence_frames() == []


class TestSegmentedServicePause:
    """EchoTTSService (mock/REST providers — the eleven_v3 and telephony REST
    path). Sentences synthesize strictly in sequence here, so the gap is
    prepended to every sentence after the first audible one."""

    async def _run_service(self, tts, collector, feeder):
        pipeline = Pipeline([tts, collector])
        worker = PipelineWorker(
            pipeline, params=PipelineParams(enable_metrics=False,
                                            audio_out_sample_rate=SAMPLE_RATE),
            enable_rtvi=False, idle_timeout_secs=None,
        )
        runner = WorkerRunner(handle_sigint=False)
        feed = asyncio.create_task(feeder(worker))
        await asyncio.wait_for(runner.run(worker), timeout=30)
        await feed

    async def test_multi_sentence_reply_gets_exact_gaps(self):
        tts = EchoTTSService(
            get_tts_provider(ProviderConfig(provider="mock")),
            sample_rate=SAMPLE_RATE, pause_ms=PAUSE_MS,
        )
        collector = AudioCollector()

        async def feeder(worker):
            await asyncio.sleep(0.2)
            await speak_turn(worker, [
                "First segmented sentence. ", "Second segmented sentence. ",
                "Third segmented sentence. ",
            ])
            await worker.queue_frame(EndWorkerFrame(reason="done"))

        await self._run_service(tts, collector, feeder)

        silences = collector.silence_frames()
        assert len(silences) == 2
        assert all(len(s) == PAUSE_BYTES for s in silences)
        # Never leading or trailing silence.
        assert collector.audio[0] != b"\x00" * len(collector.audio[0])
        assert collector.audio[-1] != b"\x00" * len(collector.audio[-1])

    async def test_interruption_resets_the_sentence_counter(self):
        tts = EchoTTSService(
            get_tts_provider(ProviderConfig(provider="mock")),
            sample_rate=SAMPLE_RATE, pause_ms=PAUSE_MS,
        )
        collector = AudioCollector()

        async def feeder(worker):
            await asyncio.sleep(0.2)
            await speak_turn(worker, ["Reply one sentence one. ",
                                      "Reply one sentence two. "])
            await worker.queue_frame(InterruptionFrame())
            await asyncio.sleep(0.2)
            # A fresh turn must not start with silence owed by the last one.
            await speak_turn(worker, ["Reply two only sentence. "])
            await worker.queue_frame(EndWorkerFrame(reason="done"))

        await self._run_service(tts, collector, feeder)

        # The second reply produced voiced audio with no leading silence: the
        # only silence frame is the gap inside the first (interrupted) reply.
        assert len(collector.silence_frames()) <= 1
        assert collector.audio[-1] != b"\x00" * len(collector.audio[-1])


class TestSilenceLengthContract:
    def test_pause_bytes_formula(self):
        assert PAUSE_BYTES == SAMPLE_RATE * PAUSE_MS // 1000 * 2
        assert len(PCM_CHUNK) % 2 == 0  # mock chunks stay sample-aligned
