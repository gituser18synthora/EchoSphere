"""Six-turn acknowledgement regression through the real Pipecat output path.

Only external LLM/TTS providers and the audio device are replaced. No network,
database, or actual voice-provider credentials are used.
"""

import asyncio
import random
import time
from collections import defaultdict

import pytest
from pipecat.frames.frames import EndFrame, StartFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.workers.runner import WorkerRunner

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.naturalness import SpeechNaturalnessPlanner
from shared.providers.tts.streaming import TTSStreamEvent, TTSStreamSettings
from voice_runtime.brain import ConversationBrain
from voice_runtime.latency_filler import FillerClipLibrary
from voice_runtime.filler_transport import FillerOutputTransportMixin
from voice_runtime.pipeline import build_latency_filler
from voice_runtime.tts_router import StreamingTTSRouter
from voice_runtime.turn_metrics import TurnLatencyTracker
from tests.unit.test_latency_filler import _AcknowledgementCueStub
from tests.unit.test_naturalness_runtime import _RecorderStub
from tests.unit.test_tts_router_flush_finals import FakeProvider
from tests.unit.test_turn_endpointing import transcript


class Recorder(_RecorderStub):
    def __init__(self):
        super().__init__()
        self.usage = defaultdict(int)

    def add_tts_usage(self, **kwargs):
        pass


class LLM:
    last_stream_usage = None

    def __init__(self, state):
        self.state = state
        self.started = []

    async def stream(self, *args, **kwargs):
        self.started.append(time.monotonic())
        await asyncio.sleep(self.state["llm"])
        yield "आपकी बात समझ ली है, मैं पूरी जानकारी बता सकता हूँ।"


class Provider(FakeProvider):
    def __init__(self, state):
        super().__init__()
        self.state = state
        self.tasks = {}
        self.texts = {}
        self.requested = []

    async def synthesize_stream(self, text, *, generation_id):
        await super().synthesize_stream(text, generation_id=generation_id)
        self.texts[generation_id] = self.texts.get(generation_id, "") + text

    async def flush(self, generation_id):
        if generation_id not in self.tasks:
            self.requested.append(time.monotonic())
            self.tasks[generation_id] = asyncio.create_task(self.render(generation_id))

    async def finish(self, generation_id):
        await self.flush(generation_id)

    async def render(self, generation_id):
        await asyncio.sleep(self.state["tts"])
        await self._emit(TTSStreamEvent(
            kind="audio", generation_id=generation_id,
            audio=(12000).to_bytes(2, "little") * 3840,  # 240 ms at 16 kHz
        ))
        await self._emit(TTSStreamEvent(kind="final", generation_id=generation_id))

    async def cancel(self, generation_id):
        self._end_generation(generation_id)
        if generation_id in self.tasks:
            self.tasks[generation_id].cancel()

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


class Router(StreamingTTSRouter):
    def _stream_settings(self, engine, locale, *, speed=None):
        return TTSStreamSettings(
            provider="sarvam", model="bulbul:v3", voice="anushka",
            language=locale, sample_rate=16000,
        )


class Output(FillerOutputTransportMixin, BaseOutputTransport):
    def __init__(self):
        super().__init__(TransportParams(
            audio_out_enabled=True, audio_out_sample_rate=16000,
            audio_out_10ms_chunks=2, audio_out_end_silence_secs=0,
        ))
        self.writes = []

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame):
        values = memoryview(frame.audio).cast("h")
        label = "reply" if 12000 in values else "ack" if 7000 in values else "breath"
        self.writes.append((time.monotonic(), label))
        await asyncio.sleep(len(frame.audio) / 32000)
        return True


@pytest.mark.parametrize("acknowledgements", [False, True])
async def test_six_turns_never_queue_ack_tts_ahead_of_the_answer(acknowledgements):
    state = {"llm": 0.01, "tts": 0.03}
    recorder = Recorder()
    tracker = TurnLatencyTracker(session_id=recorder.session_id)
    naturalness = SpeechNaturalnessPlanner({
        "enabled": True, "acknowledgements": acknowledgements,
        "acknowledgement_probability": 1.0, "backchannels": False,
        "latency_filler_delay_ms": 500, "latency_filler_ladder": False,
        "sentence_breaths": False,
    }, rng=random.Random(7))
    filler = build_latency_filler(
        naturalness, sample_rate=16000, recorder=recorder,
        library=FillerClipLibrary(None), cue_library=_AcknowledgementCueStub(),
    )
    config = ResolvedBotConfig(
        tenant_id="audit", bot_id="audit", bot_name="Audit", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="Answer briefly.",
        tts={"provider": "sarvam", "model": "bulbul:v3", "voice": "anushka", "voice_gender": "female"},
    )
    llm, provider, output = LLM(state), Provider(state), Output()
    brain = ConversationBrain(
        config=config, llm=llm, recorder=recorder, latency=tracker,
        naturalness=naturalness, latency_filler=filler,
        finalize_grace=0.02, complete_endpoint=0.02, short_reply_endpoint=0.02,
    )
    tts = Router(
        tts_config=config.tts, language="hi-IN", sample_rate=16000,
        provider_factory=lambda settings: provider, recorder=recorder, latency=tracker,
    )
    worker = PipelineWorker(
        Pipeline([brain, tts, filler, output]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    observations = []

    async def feed():
        await asyncio.sleep(0.1)
        for index, (llm_delay, tts_delay) in enumerate([(0.01, 0.03), (0.7, 0.03), (0.01, 0.7)] * 2):
            state.update(llm=llm_delay, tts=tts_delay)
            tracker.mark_speech_started()
            await worker.queue_frame(UserStartedSpeakingFrame())
            await asyncio.sleep(0.03)
            tracker.mark_speech_stopped()
            stopped = time.monotonic()
            await worker.queue_frame(transcript(f"मुझे अपनी बात बतानी है और जानकारी चाहिए {chr(65 + index)}"))
            await worker.queue_frame(UserStoppedSpeakingFrame())
            await asyncio.sleep(llm_delay + tts_delay + 0.7)
            first = {}
            for at, label in output.writes:
                if at >= stopped:
                    first.setdefault(label, at - stopped)
            observations.append(first)
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(WorkerRunner(handle_sigint=False).run(worker), timeout=20)
        await feeder
    finally:
        if not feeder.done():
            feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)

    assert len(llm.started) == len(provider.requested) == 6
    assert all(len(text.split()) > 4 for text in provider.texts.values())
    for index, first in enumerate(observations):
        assert "reply" in first, observations
        if index % 3 == 0:
            assert set(first) == {"reply"} and first["reply"] < 0.3
        elif index % 3 == 1:
            expected = "ack" if acknowledgements else "breath"
            assert expected in first and first[expected] >= 0.5
            # The queued acknowledgement's full 600 ms never holds up the reply.
            assert first["reply"] < first[expected] + 0.6
        else:
            # Synthesis has started but audio is still pending: the same
            # latency threshold applies. The no-consecutive-ack policy can
            # select a breath after the preceding slow-LLM acknowledgement.
            waiting = [at for label, at in first.items() if label != "reply"]
            assert waiting and min(waiting) >= 0.5
            assert first["reply"] < 1.0
    for index, (requested, started) in enumerate(zip(provider.requested, llm.started)):
        expected_llm_delay = 0.7 if index % 3 == 1 else 0.01
        assert requested - started < expected_llm_delay + 0.15
