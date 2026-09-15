"""Playable-audio readiness through the real brain, router and output queue.

External providers and the audio device are local deterministic stand-ins.
The same runner is used to capture before/after wall-clock timing artifacts.
"""

import asyncio
import random
import time

import pytest
from pipecat.frames.frames import (
    EndFrame, OutputAudioRawFrame, TTSAudioRawFrame, TTSStartedFrame,
    UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from shared.bot_config import ResolvedBotConfig
from shared.orchestration.naturalness import SpeechNaturalnessPlanner
from shared.providers.tts.streaming import TTSStreamEvent
from voice_runtime.brain import ConversationBrain
from voice_runtime.latency_filler import FillerClipLibrary
from voice_runtime.pipeline import build_latency_filler
from voice_runtime.turn_metrics import TurnLatencyTracker
from tests.unit.test_latency_ack_pipeline import LLM, Output, Provider, Recorder, Router
from tests.unit.test_latency_filler import _AcknowledgementCueStub
from tests.unit.test_turn_endpointing import transcript


class TimedRecorder(Recorder):
    def __init__(self):
        super().__init__()
        self.timed_events = []

    def add_event(self, kind, **data):
        self.timed_events.append((time.monotonic(), kind, data))
        super().add_event(kind, **data)


class AudioProbe(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.frames = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSStartedFrame):
                self.frames.append((time.monotonic(), "tts_started"))
            elif isinstance(frame, OutputAudioRawFrame) and frame.audio:
                label = "reply" if isinstance(frame, TTSAudioRawFrame) else "filler"
                self.frames.append((time.monotonic(), label))
        await self.push_frame(frame, direction)


class BoundaryProvider(Provider):
    async def render(self, generation_id):
        # Boundary cases use the speech-end clock, avoiding variation in
        # endpointing/LLM overhead when targeting either side of the deadline.
        release_at = self.state.get("release_at")
        if release_at is None:
            await super().render(generation_id)
            return
        await asyncio.sleep(max(0, release_at - time.monotonic()))
        await self._emit(TTSStreamEvent(
            kind="audio", generation_id=generation_id,
            audio=(12000).to_bytes(2, "little") * 3840,
        ))
        await self._emit(TTSStreamEvent(kind="final", generation_id=generation_id))


class TimedOutput(Output):
    def __init__(self):
        super().__init__()
        self.intervals = []
        self.mixed_audio_frames = 0

    async def write_audio_frame(self, frame):
        values = memoryview(frame.audio).cast("h")
        if 12000 in values and any(value != 12000 for value in values):
            self.mixed_audio_frames += 1
        started = time.monotonic()
        result = await super().write_audio_frame(frame)
        self.intervals.append((started, time.monotonic()))
        return result


async def run_readiness_scenarios(acknowledgements, *, threshold_ms=500, adaptive=False, cue_library=None):
    threshold = threshold_ms / 1000
    slow = threshold + 0.2
    scenarios = [
        ("fast_llm_fast_tts", 0.01, 0.03, None),
        ("slow_llm_fast_tts", slow, 0.03, None),
        ("slow_llm_slow_tts", slow, slow, None),
        *[(f"slow_tts_turn_{i + 1}", 0.01, slow, None) for i in range(6)],
        ("audio_before_threshold", 0.01, 0.03, threshold - 0.04),
        ("audio_after_filler_start", 0.01, 0.03, threshold + 0.025),
    ]
    state = {"llm": 0.01, "tts": 0.03}
    recorder = TimedRecorder()
    tracker = TurnLatencyTracker(session_id=recorder.session_id)
    naturalness = SpeechNaturalnessPlanner({
        "enabled": True, "acknowledgements": acknowledgements,
        "acknowledgement_probability": 1.0, "backchannels": False,
        "latency_filler_delay_ms": threshold_ms, "latency_filler_ladder": adaptive,
        "adaptive_latency_cues": adaptive, "latency_cue_probability": 1.0,
        "sentence_breaths": False,
    }, rng=random.Random(7))
    filler = build_latency_filler(
        naturalness, sample_rate=16000, recorder=recorder,
        library=FillerClipLibrary(None), cue_library=cue_library or _AcknowledgementCueStub(),
    )
    config = ResolvedBotConfig(
        tenant_id="audit", bot_id="audit", bot_name="Audit", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="Answer briefly.",
        tts={"provider": "sarvam", "model": "bulbul:v3", "voice": "anushka", "voice_gender": "female"},
    )
    llm, provider, output = LLM(state), BoundaryProvider(state), TimedOutput()
    brain = ConversationBrain(
        config=config, llm=llm, recorder=recorder, latency=tracker,
        naturalness=naturalness, latency_filler=filler,
        finalize_grace=0.02, complete_endpoint=0.02, short_reply_endpoint=0.02,
    )
    tts = Router(
        tts_config=config.tts, language="hi-IN", sample_rate=16000,
        provider_factory=lambda settings: provider, recorder=recorder, latency=tracker,
    )
    incoming, outgoing = AudioProbe(), AudioProbe()
    worker = PipelineWorker(
        Pipeline([brain, tts, incoming, filler, outgoing, output]),
        params=PipelineParams(audio_in_sample_rate=16000, audio_out_sample_rate=16000),
        enable_rtvi=False, idle_timeout_secs=None,
    )
    observations = []

    async def feed():
        await asyncio.sleep(0.1)
        for index, (name, llm_delay, tts_delay, audio_at) in enumerate(scenarios):
            state.update(llm=llm_delay, tts=tts_delay, release_at=None)
            tracker.mark_speech_started()
            await worker.queue_frame(UserStartedSpeakingFrame())
            await asyncio.sleep(0.03)
            tracker.mark_speech_stopped()
            stopped = time.monotonic()
            if audio_at is not None:
                state["release_at"] = stopped + audio_at
            await worker.queue_frame(transcript(f"मुझे अपनी बात बतानी है और जानकारी चाहिए {chr(65 + index)}"))
            await worker.queue_frame(UserStoppedSpeakingFrame())
            await asyncio.sleep(max(
                (audio_at or llm_delay + tts_delay) + 0.7,
                threshold + 1.2 if adaptive else 0,
            ))

            def relative(at):
                return round((at - stopped) * 1000, 3) if at is not None else None

            def first_frame(probe, label):
                return next((at for at, kind in probe.frames if at >= stopped and kind == label), None)

            events = [(at, kind, data) for at, kind, data in recorder.timed_events if at >= stopped]

            def first_event(kind):
                return next((at for at, event, data in events if event == kind), None)

            writes = [(at, label) for at, label in output.writes if at >= stopped]
            reply_playback = next((at for at, label in writes if label == "reply"), None)
            filler_writes = [(at, label) for at, label in writes if label != "reply"]
            forwarded = [(at, label) for at, label in outgoing.frames if at >= stopped]
            forwarded_reply = first_frame(outgoing, "reply")
            observations.append({
                "scenario": name, "acknowledgements": acknowledgements,
                "threshold_ms": threshold_ms, "speech_end_monotonic_s": stopped,
                "speech_end_ms": 0.0,
                "llm_start_ms": relative(llm.started[index]),
                "tts_started_ms": relative(first_frame(incoming, "tts_started")),
                "filler_start_ms": relative(first_event("latency_filler_played")),
                "filler_playback_ms": relative(filler_writes[0][0] if filler_writes else None),
                "filler_kind": filler_writes[0][1] if filler_writes else None,
                "first_playable_tts_audio_ms": relative(first_frame(incoming, "reply")),
                "filler_cancel_ms": relative(first_event("latency_filler_cut")),
                "filler_complete_ms": relative(first_event("latency_filler_completed")),
                "response_forwarded_ms": relative(forwarded_reply),
                "response_playback_ms": relative(reply_playback),
                "filler_frames_after_reply_forwarded": sum(
                    label == "filler" and at > forwarded_reply for at, label in forwarded
                ) if forwarded_reply else None,
                "filler_writes_after_reply_started": sum(
                    at > reply_playback for at, _ in filler_writes
                ) if reply_playback else None,
                "filler_events": [
                    {"ms": relative(at), "kind": kind, **data}
                    for at, kind, data in events if kind.startswith(("latency_filler", "early_ack"))
                ],
            })
        await worker.queue_frame(EndFrame())

    feeder = asyncio.create_task(feed())
    try:
        await asyncio.wait_for(WorkerRunner(handle_sigint=False).run(worker), timeout=90)
        await feeder
    finally:
        if not feeder.done():
            feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)

    assert len(llm.started) == len(provider.requested) == len(scenarios)
    assert all(len(text.split()) > 4 for text in provider.texts.values())
    assert output.mixed_audio_frames == 0
    assert all(end <= next_start for (_, end), (next_start, _) in zip(output.intervals, output.intervals[1:]))
    return observations


@pytest.mark.parametrize("acknowledgements", [False, True])
async def test_playable_readiness_across_slow_tts_turns_and_threshold_edges(acknowledgements):
    observations = await run_readiness_scenarios(acknowledgements)
    for row in observations:
        ready, forwarded, playback = (row[key] for key in (
            "first_playable_tts_audio_ms", "response_forwarded_ms", "response_playback_ms",
        ))
        assert row["tts_started_ms"] < ready <= forwarded <= playback, row
        assert row["llm_start_ms"] < 150, row
        # Readiness does not await a filler duration; output queue tails are
        # a separately deferred issue. Test the processor handoff here.
        assert forwarded - ready < 50, row
        assert row["filler_frames_after_reply_forwarded"] == 0, row
        assert row["filler_writes_after_reply_started"] == 0, row
        if row["scenario"] in {"fast_llm_fast_tts", "audio_before_threshold"}:
            assert ready < 500 and row["filler_start_ms"] is None, row
            assert row["filler_playback_ms"] is None, row
            continue
        assert 495 <= row["filler_start_ms"] < 600, row
        assert row["filler_start_ms"] < ready, row
        if row["filler_complete_ms"] is None:
            assert ready <= row["filler_cancel_ms"] <= forwarded, row
        if row["scenario"].startswith("slow_tts_turn_"):
            assert row["tts_started_ms"] < row["filler_start_ms"], row
            assert row["filler_cancel_ms"] is not None, row
        if row["scenario"] == "audio_after_filler_start":
            assert 0 < ready - row["filler_start_ms"] < 100, row
    kinds = [row["filler_kind"] for row in observations]
    if acknowledgements:
        assert "ack" in kinds
        assert all(a != "ack" or b != "ack" for a, b in zip(kinds, kinds[1:]))
    else:
        assert "ack" not in kinds
