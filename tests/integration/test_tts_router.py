"""StreamingTTSRouter behavior in a live Pipecat pipeline against mock servers.

Covers: sentence-ordered streaming, barge-in cancellation with late-audio
rejection, transient-failure fallback (and NO fallback on auth errors),
per-language provider switching, and provider-usage recording.
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
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

import shared.providers.tts.elevenlabs_ws as elevenlabs_ws
import shared.providers.tts.sarvam_ws as sarvam_ws
from shared.bot_config import ResolvedBotConfig
from voice_runtime.frames import SwitchVoiceLanguageFrame
from voice_runtime.recording import SessionRecorder
from voice_runtime.tts_router import StreamingTTSRouter
from tests.mock_tts_servers import API_KEY, MockElevenLabsServer, MockSarvamTTSServer

pytestmark = pytest.mark.integration


class AudioCollector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.events: list[tuple[str, object]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.events.append(("audio", frame.context_id))
        elif isinstance(frame, TTSStoppedFrame):
            self.events.append(("stopped", frame.context_id))
        elif isinstance(frame, TextFrame):
            self.events.append(("text", frame.text))
        await self.push_frame(frame, direction)

    def audio_count(self) -> int:
        return sum(1 for kind, _ in self.events if kind == "audio")


def make_recorder(name: str) -> SessionRecorder:
    config = ResolvedBotConfig(
        tenant_id="tn-001", bot_id="bot-101", bot_name="RouterTest",
        version="v1", published=True,
    )
    return SessionRecorder(name, config)


def tts_config(sarvam_ref="env:TEST_SARVAM_KEY", *, language_map=None, fallback=None):
    return {
        "provider": "sarvam", "model": "bulbul:v3", "voice": "shubh",
        "settings": {"pace": 1.0, "min_buffer_size": 40},
        "api_key_reference": sarvam_ref,
        "language_map": language_map or {},
        "fallback": fallback,
    }


def eleven_engine(ref="env:TEST_EL_KEY"):
    return {"provider": "elevenlabs", "model": "eleven_flash_v2_5",
            "voice": "elvoice1", "params": {}, "api_key_reference": ref}


async def run_router(router, collector, feeder, timeout=20):
    pipeline = Pipeline([router, collector])
    worker = PipelineWorker(
        pipeline, params=PipelineParams(enable_metrics=False, audio_out_sample_rate=16000),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    runner = WorkerRunner(handle_sigint=False)
    feed_task = asyncio.create_task(feeder(worker))
    await asyncio.wait_for(runner.run(worker), timeout=timeout)
    await feed_task


async def speak_turn(worker, sentences, settle=0.6):
    await worker.queue_frame(LLMFullResponseStartFrame())
    for sentence in sentences:
        await worker.queue_frame(TextFrame(sentence))
    await worker.queue_frame(LLMFullResponseEndFrame())
    await asyncio.sleep(settle)
    # The output transport emits this once playout finishes; the TTS service
    # resumes processing input frames on it (pause_frame_processing=True).
    await worker.queue_frame(BotStoppedSpeakingFrame())
    await asyncio.sleep(0.1)


class TestStreamingRouter:
    async def test_turn_streams_audio_in_order_and_stops(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=2) as server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", server.url)
            recorder = make_recorder("vs_router_happy")
            router = StreamingTTSRouter(
                tts_config=tts_config(), language="hi-IN",
                sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["First sentence here. ", "Second sentence now."])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert collector.audio_count() == 2
        # Sentence order preserved into the provider.
        assert server.texts()[0].startswith("First sentence")
        assert server.texts()[1].startswith("Second sentence")
        # One persistent connection, one flush at end of turn.
        assert server.connections == 1
        assert ("stopped", None) not in collector.events
        used = [e for e in recorder.events if e["kind"] == "tts_provider_used"]
        assert used and used[0]["provider"] == "sarvam" and not used[0]["fallback_used"]

    async def test_barge_in_cancels_and_rejects_late_audio(self, monkeypatch):
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockElevenLabsServer(behavior="late_after_close", chunks=3,
                                        first_chunk_delay=0.4) as server:
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", server.url)
            recorder = make_recorder("vs_router_barge")
            router = StreamingTTSRouter(
                tts_config={"provider": "elevenlabs", "model": "eleven_flash_v2_5",
                            "voice": "elvoice1", "settings": {},
                            "api_key_reference": "env:TEST_EL_KEY",
                            "language_map": {}, "fallback": None},
                language="en-US", sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await worker.queue_frame(LLMFullResponseStartFrame())
                await worker.queue_frame(TextFrame("A long reply that will be interrupted. "))
                await worker.queue_frame(LLMFullResponseEndFrame())
                await asyncio.sleep(0.15)  # before the delayed first chunk arrives
                await worker.queue_frame(InterruptionFrame())
                await asyncio.sleep(0.8)   # server emits late chunks post-close
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        # The server context was closed by the barge-in and any late audio
        # chunks it kept sending were rejected — nothing reached the transport.
        assert server.closed_contexts, "close_context was not sent on barge-in"
        assert collector.audio_count() == 0

    async def test_fallback_on_transient_error(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockSarvamTTSServer(behavior="error_message") as sarvam_server, \
                MockElevenLabsServer(chunks=2) as eleven_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", eleven_server.url)
            recorder = make_recorder("vs_router_fallback")
            router = StreamingTTSRouter(
                tts_config=tts_config(fallback=eleven_engine()),
                language="hi-IN", sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["Primary provider will fail here. "], settle=1.5)
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert collector.audio_count() == 2  # audio produced by the fallback
        assert "Primary provider will fail here." in " ".join(eleven_server.texts())
        fallback_events = [e for e in recorder.events if e["kind"] == "tts_fallback"]
        assert fallback_events and fallback_events[0]["to_provider"] == "elevenlabs"
        # Transient failures are recoverable — they must never end the call.
        assert not [e for e in recorder.events if e["kind"] == "tts_fatal"]
        used = [e for e in recorder.events if e["kind"] == "tts_provider_used"]
        assert used and used[-1]["provider"] == "elevenlabs" and used[-1]["fallback_used"]

    async def test_no_fallback_on_auth_error(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", "wrong-key")
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockSarvamTTSServer() as sarvam_server, \
                MockElevenLabsServer(chunks=2) as eleven_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", eleven_server.url)
            recorder = make_recorder("vs_router_auth")
            router = StreamingTTSRouter(
                tts_config=tts_config(fallback=eleven_engine()),
                language="hi-IN", sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["This will hit an auth failure. "], settle=1.0)
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        # Auth problems must surface, never silently fall back.
        assert collector.audio_count() == 0
        assert eleven_server.connections == 0
        assert not [e for e in recorder.events if e["kind"] == "tts_fallback"]
        # …and the call must END instead of sitting in dead air until the
        # caller hangs up (observed live: 12.7 s of silence, dialer close).
        fatal = [e for e in recorder.events if e["kind"] == "tts_fatal"]
        assert fatal and fatal[0]["category"] == "auth"

    async def test_fatal_auth_failure_ends_the_call_without_external_stop(
        self, monkeypatch
    ):
        """The router's own EndWorkerFrame must stop the pipeline — the test
        would time out if the dead-air call were left running."""
        monkeypatch.setenv("TEST_SARVAM_KEY", "wrong-key")
        async with MockSarvamTTSServer() as sarvam_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            recorder = make_recorder("vs_router_auth_ends")
            router = StreamingTTSRouter(
                tts_config=tts_config(), language="hi-IN",
                sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["Dead air must not linger. "], settle=1.0)
                # No EndWorkerFrame queued here on purpose.

            await run_router(router, collector, feeder, timeout=10)

        assert collector.audio_count() == 0
        fatal = [e for e in recorder.events if e["kind"] == "tts_fatal"]
        assert fatal and fatal[0]["category"] == "auth"

    async def test_no_fallback_on_invalid_config_error(self, monkeypatch):
        """Sarvam 422 config rejections (bad language/speaker payload) are
        configuration errors: they must surface as invalid_input and never
        silently switch to the fallback engine."""
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockSarvamTTSServer(behavior="invalid_config") as sarvam_server, \
                MockElevenLabsServer(chunks=2) as eleven_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", eleven_server.url)
            recorder = make_recorder("vs_router_invalid_config")
            router = StreamingTTSRouter(
                tts_config=tts_config(fallback=eleven_engine()),
                language="hi-IN", sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["This config will be rejected. "], settle=1.0)
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert collector.audio_count() == 0
        assert eleven_server.connections == 0  # config errors never fall back
        assert not [e for e in recorder.events if e["kind"] == "tts_fallback"]
        used = [e for e in recorder.events if e["kind"] == "tts_provider_used"]
        assert used and used[-1]["failed"] is True and used[-1]["provider"] == "sarvam"
        fatal = [e for e in recorder.events if e["kind"] == "tts_fatal"]
        assert fatal and fatal[0]["category"] == "invalid_input"

    async def test_per_language_provider_switch_reuses_connections(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_KEY", API_KEY)
        monkeypatch.setenv("TEST_EL_KEY", API_KEY)
        async with MockSarvamTTSServer(chunks=1) as sarvam_server, \
                MockElevenLabsServer(chunks=1) as eleven_server:
            monkeypatch.setattr(sarvam_ws, "_WS_URL", sarvam_server.url)
            monkeypatch.setattr(elevenlabs_ws, "_WS_BASE", eleven_server.url)
            recorder = make_recorder("vs_router_langswitch")
            router = StreamingTTSRouter(
                tts_config=tts_config(language_map={
                    "hi-IN": {"provider": "sarvam", "model": "bulbul:v3",
                              "voice": "shubh", "params": {},
                              "api_key_reference": "env:TEST_SARVAM_KEY"},
                    "en-US": eleven_engine(),
                }),
                language="hi-IN", sample_rate=16000, recorder=recorder,
            )
            collector = AudioCollector()

            async def feeder(worker):
                await asyncio.sleep(0.2)
                await speak_turn(worker, ["नमस्ते, मैं मदद कर सकता हूँ। "])
                await worker.queue_frame(SwitchVoiceLanguageFrame(language="en-US"))
                await speak_turn(worker, ["Sure, switching to English now. "])
                # Switch back — the Sarvam connection must be reused, not reopened.
                await worker.queue_frame(SwitchVoiceLanguageFrame(language="hi-IN"))
                await speak_turn(worker, ["वापस हिंदी में। "])
                await worker.queue_frame(EndWorkerFrame(reason="done"))

            await run_router(router, collector, feeder)

        assert any("नमस्ते" in t for t in sarvam_server.texts())
        assert any("English" in t for t in eleven_server.texts())
        assert any("हिंदी" in t for t in sarvam_server.texts())
        assert sarvam_server.connections == 1  # reused across the switch-back
        assert eleven_server.connections == 1
        assert collector.audio_count() == 3
        switches = [e for e in recorder.events if e["kind"] == "tts_language_switched"]
        assert [s["language"] for s in switches] == ["en-US", "hi-IN"]
