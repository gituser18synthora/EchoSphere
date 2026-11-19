"""Farewell cancellation through real TTS, websocket queues and serializers.

Only providers and the remote socket are faked; no services or DB are used.
"""

import asyncio
import time
from collections import defaultdict
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import InterruptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.decision_schema import ConversationDecision
from shared.orchestration.router import RouteDecision, RouteKind
from tests.unit.test_brain_workflow_routing import _LLMStub, _WorkflowStub, consumed_result
from tests.unit.test_filler_interruption_pipeline import InterruptSocket, until
from tests.unit.test_filler_websocket_cleanup import ObservedOutput
from tests.unit.test_latency_ack_pipeline import Provider, Recorder, Router, _run_worker
from tests.unit.test_turn_endpointing import transcript
from voice_runtime.brain import ConversationBrain
from voice_runtime.closing import ClosingPlaybackObserver
from voice_runtime.frames import WorkflowClosingPlayedFrame
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.services import EchoTTSService
from voice_runtime.telephony import FreeSwitchAudioForkSerializer, VaaniFrameSerializer


@pytest.mark.parametrize("kind", ["browser", "fork", "vaani"])
@pytest.mark.parametrize("phase", [None, "synthesis", "playback", "grace"])
@pytest.mark.parametrize("tts_kind", ["streaming", "segmented"])
async def test_workflow_farewell_through_real_output(kind, phase, tts_kind, monkeypatch):
    monkeypatch.setattr("voice_runtime.brain._WORKFLOW_CLOSE_GRACE_S", 0.08)
    client = InterruptSocket(browser=kind == "browser")
    serializer = {
        "browser": RawPCMSerializer,
        "fork": FreeSwitchAudioForkSerializer,
        "vaani": lambda: VaaniFrameSerializer(stream_sid="closing-test"),
    }[kind]()
    class ClosingOutput(ObservedOutput):
        sent_pcm_bytes = 0

        async def write_audio_frame(self, frame):
            sent = await super().write_audio_frame(frame)
            if sent:
                self.sent_pcm_bytes += len(frame.audio)
            return sent

    output = ClosingOutput(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=4, audio_out_end_silence_secs=0, serializer=serializer,
    ))
    config = ResolvedBotConfig(
        tenant_id="test", bot_id="test", bot_name="Test", version="v1", published=True,
        language="hi-IN", languages=["hi-IN"], stt={"provider": "sarvam"},
        system_prompt="Answer briefly.", tts={"provider": "sarvam", "voice": "anushka"},
    )
    workflow = _WorkflowStub(consumed_result("Dhanyavaad, aapka din shubh rahe.", done=True))
    recorder = Recorder()
    recorder.usage = defaultdict(int)
    llm = _LLMStub(reply="Visit ka exact samay abhi confirm nahi hai. Dhanyavaad.")
    brain = ConversationBrain(
        config=config, llm=llm, recorder=recorder, workflow_engine=workflow,
        finalize_grace=0.01, complete_endpoint=0.01, short_reply_endpoint=0.01,
    )
    brain._take_decision = AsyncMock(return_value=ConversationDecision(
        next_action="answer", signal="question", confidence=1,
    ))
    if tts_kind == "streaming":
        provider = Provider({"tts": 0.12})
        tts = Router(
            tts_config=config.tts, language=config.language, sample_rate=16000,
            provider_factory=lambda settings: provider, recorder=recorder,
        )
    else:
        class SegmentedProvider:
            name = "mock"
            output_sample_rate = 16000

            def __init__(self):
                self.requested = []

            async def stream_synthesize(self, text, **kwargs):
                self.requested.append(time.monotonic())
                await asyncio.sleep(0.12)
                # 250 ms leaves a partial 40 ms output chunk to flush.
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
        await brain._handle_workflow(
            RouteDecision(kind=RouteKind.WORKFLOW, action="wf_service"), "yes", 0,
        )
        token = brain._pending_workflow_close[0]
        assert not brain._closing
        if phase is None:
            await until(lambda: bool(client.packets))
            assert not brain._closing
            await until(lambda: "call_completed_by_workflow" in recorder.event_kinds())
            # Count PCM before Vaani's mu-law encoding, rather than treating
            # its 8-bit wire samples as the browser's 16-bit PCM.
            assert output.sent_pcm_bytes >= 3680  # >= 230 ms, native 8 kHz
            if tts_kind == "segmented":
                assert output.sent_pcm_bytes >= 4000  # partial tail survived
            return
        if phase == "synthesis":
            await until(lambda: bool(provider.requested))
            assert not client.packets  # farewell is still rendering
        elif phase == "playback":
            await until(lambda: bool(client.packets))
        else:
            await until(lambda: brain._workflow_close_task is not None)
        assert not brain._closing
        await worker.queue_frame(UserStartedSpeakingFrame())
        await worker.queue_frame(InterruptionFrame())
        await until(lambda: bool(client.interrupts))
        assert brain._pending_workflow_close is None and not brain._closing
        # A fence already dequeued at the instant of the interruption cannot
        # resurrect the old end signal while the caller is asking a question.
        await worker.queue_frame(WorkflowClosingPlayedFrame(token=token))
        await worker.queue_frame(transcript("कब तक होगा ये?"))
        await worker.queue_frame(UserStoppedSpeakingFrame())
        await until(lambda: "workflow_closing_followup" in recorder.event_kinds())
        assert "post_hangup_transcript_dropped" not in recorder.event_kinds()
        assert len(workflow.calls) == 1
        await until(lambda: "call_completed_by_workflow" in recorder.event_kinds())
        assert len(provider.requested) >= 2  # a new audible answer, then close
        assert recorder.event_kinds().count("call_completed_by_workflow") == 1
        assert any(t.role == "user" and "कब तक" in t.text for t in recorder.turns)

    runner = asyncio.create_task(_run_worker(worker))
    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(asyncio.gather(feeder, runner), timeout=10)
    finally:
        for task in (feeder, runner):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, runner, return_exceptions=True)
