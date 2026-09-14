"""Barge-in through real Pipecat queues, websocket output and serializers.

Providers/device playback are local fakes; socket ordering and emitted PCM
are real. Browser scheduler interruption is covered in voiceClient.test.ts.
"""

import asyncio
import json
import time
from unittest.mock import patch

import pytest
from pipecat.frames.frames import (
    EndFrame, InterruptionFrame, TranscriptionFrame, TTSAudioRawFrame,
    TTSStartedFrame, TTSStoppedFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams
from pipecat.workers.runner import WorkerRunner

from tests.unit.test_filler_websocket_cleanup import ObservedOutput, SocketPlayback
from tests.unit.test_latency_filler import _AcknowledgementCueStub, _ShortLibrary
from voice_runtime.frames import FillerAudioOwner, FillerAudioRawFrame
from voice_runtime.latency_filler import LatencyFillerProcessor
from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.telephony import FreeSwitchAudioForkSerializer, VaaniFrameSerializer


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_interruption_retires_filler_during_async_serialization():
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowSerializer(RawPCMSerializer):
        async def serialize(self, frame):
            payload = await super().serialize(frame)
            if isinstance(frame, FillerAudioRawFrame):
                entered.set()
                await release.wait()
            return payload

    client = InterruptSocket(browser=True)
    output = ObservedOutput(client, client, FastAPIWebsocketParams(serializer=SlowSerializer()))
    owner = FillerAudioOwner(turn_id=1)
    frame = FillerAudioRawFrame(audio=b"\x58\x1b" * 160, sample_rate=8000, num_channels=1, owner=owner)
    # Keep the actual ingress ownership path while bypassing an unstarted
    # processor's queue (this test controls the in-flight writer directly).
    with patch.object(FrameProcessor, "queue_frame"):
        await output.queue_frame(frame)
        sending = asyncio.create_task(output.write_audio_frame(frame))
        await entered.wait()
        await output.queue_frame(InterruptionFrame())
        assert owner.cancelled
        release.set()
        assert await sending is False
    assert client.packets == []


@pytest.mark.parametrize("kind", ["fork", "stream", "vaani"])
async def test_telephony_interruption_clears_partial_packet_only_in_its_call(kind):
    from voice_runtime.telephony import FreeSwitchAudioStreamSerializer
    from tests.unit.test_telephony_filler_ownership import audio_from_wire, pcm
    factory = {"fork": FreeSwitchAudioForkSerializer, "stream": FreeSwitchAudioStreamSerializer,
               "vaani": lambda: VaaniFrameSerializer(stream_sid="local-call")}[kind]
    call, other = factory(), factory()
    partial = TTSAudioRawFrame(audio=pcm(3000, 80), sample_rate=8000, num_channels=1)
    assert await call.serialize(partial) is None
    assert await other.serialize(partial) is None
    await call.serialize(InterruptionFrame())
    assert call._pending_audio == b"" and call._ramp_step == 0
    assert other._pending_audio == partial.audio
    owner = FillerAudioOwner(turn_id=2)
    fresh = FillerAudioRawFrame(audio=pcm(7000), sample_rate=8000, num_channels=1, owner=owner)
    assert audio_from_wire(await call.serialize(fresh)) == fresh.audio
    reply = TTSAudioRawFrame(audio=pcm(12000, 320), sample_rate=8000, num_channels=1)
    assert audio_from_wire(await call.serialize(reply)) == reply.audio


class InterruptSocket(SocketPlayback):
    def __init__(self, browser):
        super().__init__(browser)
        self.interrupts = []
        self.wire = []
        self.payload_owners = {}

    async def send(self, payload):
        owner = self.payload_owners.get(payload)
        self.wire.append((time.monotonic(), payload, owner))
        if isinstance(payload, str):
            message = json.loads(payload)
            if message.get("name") == "interruption" or message.get("type") == "killAudio" or message.get("event") == "clear":
                now = time.monotonic()
                self.interrupts.append(now)
                self.playhead = now
                for packet in self.packets:
                    packet["end"] = min(packet["end"], now)
                return
        await super().send(payload)


class RecoveryOutput(ObservedOutput):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user_ends = 0
        self.transcripts = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStoppedSpeakingFrame):
            self.user_ends += 1
        if isinstance(frame, TranscriptionFrame):
            self.transcripts.append(frame.text)


async def run_interruption_scenarios(kind, acknowledgements):
    client = InterruptSocket(browser=kind == "browser")
    serializer_type = {"browser": RawPCMSerializer, "fork": FreeSwitchAudioForkSerializer,
                       "vaani": VaaniFrameSerializer}[kind]

    class TracedSerializer(serializer_type):
        async def serialize(self, frame):
            payload = await super().serialize(frame)
            if payload and isinstance(frame, FillerAudioRawFrame):
                client.payload_owners[payload] = frame.owner
            return payload

    serializer = TracedSerializer(**({"stream_sid": "local-call"} if kind == "vaani" else {}))
    output = RecoveryOutput(client, client, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=8000,
        audio_out_10ms_chunks=4, audio_out_end_silence_secs=0, serializer=serializer,
    ))
    filler = LatencyFillerProcessor(
        delay_ms=500, sample_rate=8000, library=_ShortLibrary(clip_ms=600),
        cue_library=_AcknowledgementCueStub(), emit_flush_marker=kind != "browser",
    )
    worker = PipelineWorker(Pipeline([filler, output]), params=PipelineParams(
        audio_in_sample_rate=8000, audio_out_sample_rate=8000,
    ), enable_rtvi=False, idle_timeout_secs=None)
    rows, retired = [], []

    def reply(ms=280):
        return TTSAudioRawFrame(audio=(12000).to_bytes(2, "little") * (ms * 8),
                                sample_rate=8000, num_channels=1)

    async def feed():
        await until(lambda: bool(output._media_senders))
        for turn, scenario in enumerate(["filler", "tts", "transition"] * 2, 1):
            speech_end = time.monotonic()
            await filler.arm(turn_id=turn, gender="female", speech_stopped_at=speech_end,
                             acknowledgement={"text": "जी…"} if acknowledgements else None)
            owner = filler._armed.owner
            await filler.queue_frame(TTSStartedFrame())
            await until(lambda: any(o is owner for _, _, o in client.wire))
            first_filler = next(at for at, _, o in client.wire if o is owner)
            assert first_filler - speech_end >= 0.49
            if scenario == "filler":
                await asyncio.sleep(0.06)
            else:
                # Provider output enters at the TTS/filler boundary, rather
                # than the worker's input queue (which is not a TTS service
                # and can release synthetic old data after an interruption).
                await filler.queue_frame(reply())
                if scenario == "tts":
                    await until(lambda: any(p["reply"] and p["sent"] >= speech_end for p in client.packets))
                    await asyncio.sleep(0.04)
                # Transition: put interruption into the actual pipeline while
                # response PCM and the filler clear are crossing its queues.
            requested = time.monotonic()
            await worker.queue_frame(InterruptionFrame())
            await worker.queue_frame(UserStartedSpeakingFrame())
            text = f"user turn {turn}"
            await worker.queue_frame(TranscriptionFrame(text=text, user_id="caller", timestamp=""))
            await worker.queue_frame(UserStoppedSpeakingFrame())
            await until(lambda: len(client.interrupts) == turn and output.user_ends == turn and text in output.transcripts)
            assert owner.cancelled
            retired.append(owner)
            wire_interrupt = client.interrupts[-1]
            # Force late old chunks back through the transport ingress after
            # interruption, then immediately begin the next user turn.
            for old in retired:
                await output.queue_frame(FillerAudioRawFrame(
                    audio=b"\x58\x1b" * 160, sample_rate=8000, num_channels=1, owner=old,
                ))
            await asyncio.sleep(0.005)
            assert not any(o in retired for at, _, o in client.wire if at > wire_interrupt)
            assert output._media_senders[None]._audio_buffer == b""
            assert output._audio_send_buffer == b""
            if hasattr(serializer, "_pending_audio"):
                assert serializer._pending_audio == b""
            rows.append({"kind": kind, "ack": acknowledgements, "scenario": scenario,
                         "turn": turn, "filler_start_ms": (first_filler - speech_end) * 1000,
                         "interrupt_to_wire_ms": (wire_interrupt - requested) * 1000,
                         "stale_filler_packets": 0})

        # A full slow response after the sixth interruption must still work.
        await filler.arm(turn_id=7, gender="female", acknowledgement={"text": "जी…"} if acknowledgements else None)
        fresh = filler._armed.owner
        await filler.queue_frame(TTSStartedFrame())
        await until(lambda: any(o is fresh for _, _, o in client.wire))
        ready = time.monotonic()
        await filler.queue_frame(reply())
        await filler.queue_frame(TTSStoppedFrame())
        await until(lambda: sum(p["reply_duration_ms"] for p in client.packets if p["sent"] >= ready) == 280)
        assert not any(p["mixed"] for p in client.packets)
        assert not any(o in retired for at, _, o in client.wire if at >= ready)
        assert fresh.cancelled
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    runner = asyncio.create_task(WorkerRunner(handle_sigint=False).run(worker))
    try:
        await asyncio.wait_for(asyncio.gather(feeder, runner), 20)
    finally:
        for task in (feeder, runner):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, runner, return_exceptions=True)
    return rows


@pytest.mark.parametrize("kind", ["browser", "fork", "vaani"])
@pytest.mark.parametrize("ack", [False, True])
async def test_six_interruptions_and_slow_response_recovery(kind, ack, tmp_path):
    rows = await run_interruption_scenarios(kind, ack)
    assert len(rows) == 6
    assert all(row["interrupt_to_wire_ms"] < 50 for row in rows), rows
    (tmp_path / "interruption_timings.json").write_text(json.dumps(rows, indent=2))


@pytest.mark.parametrize("during", ["filler", "tts"])
@pytest.mark.parametrize("ack", [False, True])
async def test_real_brain_and_tts_router_process_new_speech_after_interruption(during, ack):
    import random
    from shared.bot_config import ResolvedBotConfig
    from shared.orchestration.naturalness import SpeechNaturalnessPlanner
    from voice_runtime.brain import ConversationBrain
    from voice_runtime.pipeline import build_latency_filler
    from voice_runtime.turn_metrics import TurnLatencyTracker
    from tests.unit.test_latency_ack_pipeline import LLM, Output, Provider, Recorder, Router
    from tests.unit.test_turn_endpointing import transcript

    class CaptureOutput(Output):
        def __init__(self):
            super().__init__()
            self.owners = []
            self.interrupts = 0

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InterruptionFrame):
                self.interrupts += 1
            if isinstance(frame, FillerAudioRawFrame) and frame.owner not in self.owners:
                self.owners.append(frame.owner)

    state = {"llm": 0.01, "tts": 1.5 if during == "filler" else 0.6}
    recorder = Recorder()
    tracker = TurnLatencyTracker(session_id=recorder.session_id)
    planner = SpeechNaturalnessPlanner({
        "enabled": True, "acknowledgements": ack, "acknowledgement_probability": 1.0,
        "backchannels": False, "latency_filler_delay_ms": 500,
        "latency_filler_ladder": False, "sentence_breaths": False,
    }, rng=random.Random(7))
    filler = build_latency_filler(planner, sample_rate=16000, recorder=recorder,
                                 library=_ShortLibrary(clip_ms=600), cue_library=_AcknowledgementCueStub())
    config = ResolvedBotConfig(
        tenant_id="audit", bot_id="audit", bot_name="Audit", version="v1", published=True,
        language="hi-IN", languages=["hi-IN"], stt={"provider": "sarvam"},
        system_prompt="Answer briefly.",
        tts={"provider": "sarvam", "voice": "anushka", "voice_gender": "female"},
    )
    llm, provider, output = LLM(state), Provider(state), CaptureOutput()
    brain = ConversationBrain(config=config, llm=llm, recorder=recorder, latency=tracker,
                              naturalness=planner, latency_filler=filler, finalize_grace=0.02,
                              complete_endpoint=0.02, short_reply_endpoint=0.02)
    tts = Router(tts_config=config.tts, language="hi-IN", sample_rate=16000,
                 provider_factory=lambda settings: provider, recorder=recorder, latency=tracker)
    worker = PipelineWorker(Pipeline([brain, tts, filler, output]), params=PipelineParams(
        audio_in_sample_rate=16000, audio_out_sample_rate=16000,
    ), enable_rtvi=False, idle_timeout_secs=None)

    async def finish_user_turn(suffix):
        await asyncio.sleep(0.03)
        tracker.mark_speech_stopped()
        await worker.queue_frame(transcript(f"मुझे अपनी बात बतानी है और जानकारी चाहिए {suffix}"))
        await worker.queue_frame(UserStoppedSpeakingFrame())

    async def feed():
        await until(lambda: bool(output._media_senders))
        tracker.mark_speech_started()
        await worker.queue_frame(UserStartedSpeakingFrame())
        await finish_user_turn("A")
        await until(lambda: bool(output.owners))
        old = output.owners[0]
        if during == "tts":
            await until(lambda: any(label == "reply" for _, label in output.writes))
        state["tts"] = 0.7
        await worker.queue_frame(InterruptionFrame())
        tracker.mark_speech_started()
        await worker.queue_frame(UserStartedSpeakingFrame())
        await until(lambda: output.interrupts == 1)
        assert old.cancelled
        await finish_user_turn("B")
        await until(lambda: len(output.owners) == 2)
        fresh = output.owners[1]
        assert fresh is not old and fresh.token != old.token
        new_filler = next(at for at, label in reversed(output.writes) if label != "reply")
        await until(lambda: any(at > new_filler and label == "reply" for at, label in output.writes))
        assert len(llm.started) == len(provider.requested) == 2
        assert fresh.cancelled
        await asyncio.sleep(0.3)
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    runner = asyncio.create_task(WorkerRunner(handle_sigint=False).run(worker))
    try:
        await asyncio.wait_for(asyncio.gather(feeder, runner), 12)
    finally:
        for task in (feeder, runner):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, runner, return_exceptions=True)
