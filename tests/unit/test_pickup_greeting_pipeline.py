"""Opening recovery through real TTS contexts, paced output, and serializers."""

import asyncio
import time

import pytest
from pipecat.frames.frames import EndFrame, InterruptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from shared.bot_config import ResolvedBotConfig
from tests.unit.test_filler_interruption_pipeline import InterruptSocket, until
from tests.unit.test_filler_websocket_cleanup import ObservedOutput
from tests.unit.test_latency_ack_pipeline import Provider, Recorder, Router, _run_worker
from tests.unit.test_pickup_greeting_recovery import GREETING, QUESTION
from tests.unit.test_turn_endpointing import transcript
from voice_runtime.brain import ConversationBrain
from voice_runtime.closing import ClosingPlaybackObserver
from voice_runtime.frames import GreetingPlayedFrame
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.services import EchoTTSService
from voice_runtime.telephony import FreeSwitchAudioForkSerializer, VaaniFrameSerializer


@pytest.mark.parametrize("kind", ["browser", "fork", "vaani"])
@pytest.mark.parametrize("phase", ["complete", "synthesis", "playback"])
@pytest.mark.parametrize("tts_kind", ["streaming", "segmented"])
async def test_opening_hello_through_real_audio_queues(kind, phase, tts_kind):
    client = InterruptSocket(browser=kind == "browser")
    serializer = {
        "browser": RawPCMSerializer,
        "fork": FreeSwitchAudioForkSerializer,
        "vaani": lambda: VaaniFrameSerializer(stream_sid="opening-test"),
    }[kind]()
    output = ObservedOutput(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000, audio_out_10ms_chunks=4,
        audio_out_end_silence_secs=0, serializer=serializer,
    ))
    config = ResolvedBotConfig(
        tenant_id="test", bot_id="test", bot_name="Test", version="v1", published=True,
        language="hi-IN", languages=["hi-IN"], stt={"provider": "sarvam"},
        system_prompt="Answer briefly.", greeting=GREETING,
        tts={"provider": "sarvam", "voice": "anushka"},
    )
    recorder = Recorder()
    brain = ConversationBrain(
        config=config, llm=None, recorder=recorder, finalize_grace=.01,
        complete_endpoint=.01, short_reply_endpoint=.01,
    )
    if tts_kind == "streaming":
        provider = Provider({"tts": .12})
        tts = Router(tts_config=config.tts, language=config.language, sample_rate=16000,
                     provider_factory=lambda settings: provider, recorder=recorder)
    else:
        class SegmentedProvider:
            name = "mock"
            output_sample_rate = 16000

            def __init__(self):
                self.requested = []

            async def stream_synthesize(self, text, **kwargs):
                self.requested.append((time.monotonic(), text))
                await asyncio.sleep(.12)
                yield (12000).to_bytes(2, "little") * 4000

        provider = SegmentedProvider()
        tts = EchoTTSService(provider, language=config.language, sample_rate=16000, recorder=recorder)
    worker = PipelineWorker(
        Pipeline([brain, tts, output, ClosingPlaybackObserver()]),
        params=PipelineParams(audio_in_sample_rate=8000, audio_out_sample_rate=8000),
        enable_rtvi=False, idle_timeout_secs=None,
    )

    async def feed():
        await until(lambda: bool(output._media_senders))
        await brain.speak_greeting()
        await until(lambda: bool(provider.requested))
        first_marker = brain._opening_greeting["marker"]
        if phase == "complete":
            await until(lambda: brain._opening_greeting["heard"])
        elif phase == "playback":
            await until(lambda: bool(client.packets))
            assert not brain._opening_greeting["heard"]
        else:
            assert not client.packets and not brain._opening_greeting["heard"]
        await worker.queue_frame(UserStartedSpeakingFrame())
        await worker.queue_frame(InterruptionFrame())
        await until(lambda: bool(client.interrupts))
        if phase != "complete":
            await worker.queue_frame(GreetingPlayedFrame(marker=first_marker))
        await worker.queue_frame(transcript("hello"))
        await worker.queue_frame(UserStoppedSpeakingFrame())
        expected = "pickup_hello_answered" if phase == "complete" else "opening_greeting_recovered"
        await until(lambda: expected in recorder.event_kinds())
        bots = [turn.text for turn in recorder.turns if turn.role == "bot"]
        assert bots == [GREETING, QUESTION if phase == "complete" else GREETING]
        assert not brain._identity.confirmed and not brain._identity.denied
        assert not brain._workflow_ever_routed and brain._heard_nodes == []
        assert "call_completed_by_workflow" not in recorder.event_kinds() and not brain._closing
        assert len([turn for turn in recorder.turns if turn.role == "user"]) == 1
        if phase != "complete":
            await until(lambda: brain._opening_greeting["heard"])
        await worker.queue_frame(EndFrame())

    runner = asyncio.create_task(_run_worker(worker))
    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(asyncio.gather(feeder, runner), timeout=10)
    finally:
        for task in (feeder, runner):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, runner, return_exceptions=True)
