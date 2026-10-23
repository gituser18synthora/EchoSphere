"""A confident Smart Turn verdict dispatches the turn as soon as the STT
final is in — no complete-thought, short-reply or finalization wait.

Live 2026-10-21/22 (SARVAM_REALTIME_STT=false, pipecat 1.13): 84 % of turns
ended by Smart Turn, but the endpoint span (speech end → dispatch) stayed at
~730 ms p50, the same as the pause window. Trace of 13 turns: the verdict
arrived 80–180 ms after the VAD stop, the Sarvam final 100–500 ms after it,
and the dispatch exactly 600 ms after the final on turn after turn — the
brain had armed its ``complete_endpoint`` (0.6 s on the demo tenant) when the
final landed and deliberately left it alone when the controller closed the
turn moments later ("turn close carries no newer information"). A Smart Turn
COMPLETE verdict IS newer information.

Rule (``turn_detection.smart_turn_threshold``, default 0.7): verdict at or
above the threshold + final in hand → dispatch now; verdict first, final later
→ dispatch the moment the final lands; below the threshold or a pause-window
close → the waits apply exactly as before.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pipecat.frames.frames import (
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from shared.bot_config import ResolvedBotConfig
from shared.turn_detection import TURN_DETECTION_BOUNDS, TURN_DETECTION_DEFAULTS, TURN_DETECTION_FIELDS
from tests.unit.test_turn_endpointing import _RecorderStub, transcript, wait_for
from voice_runtime.brain import ConversationBrain
from voice_runtime.turn_metrics import TurnLatencyTracker

GRACE = 0.3
SETTLE = 0.05
COMPLETE = 0.5          # the demo tenant's 0.6 s, shortened for the test clock
SHORT = 0.3
SLACK = 0.05            # "dispatch within 50 ms of the later of verdict and final"


def make_brain(*, threshold=0.7):
    config = ResolvedBotConfig(
        tenant_id="tn-x", bot_id="bot-x", bot_name="Test", version="v1",
        published=True, language="hi-IN", languages=["hi-IN"],
        stt={"provider": "sarvam"}, system_prompt="You are Test.",
    )
    tracker = TurnLatencyTracker(session_id="s-test")
    brain = ConversationBrain(
        config=config, llm=None, recorder=_RecorderStub(),
        finalize_grace=GRACE, finalize_settle=SETTLE,
        complete_endpoint=COMPLETE, short_reply_endpoint=SHORT,
        smart_turn_threshold=threshold, latency=tracker,
    )
    brain._pushed = []

    async def _push(frame, direction=None):
        brain._pushed.append(frame)

    async def _notify(payload):
        pass

    async def _cancel_task(task, timeout=None):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    brain.push_frame = _push
    brain._notify_client = _notify
    brain.create_task = lambda coro, name=None: asyncio.get_event_loop().create_task(coro)
    brain.cancel_task = _cancel_task
    dispatched: list[tuple[str, float]] = []

    async def _handle(text):
        dispatched.append((text, time.monotonic()))

    brain._handle_turn = _handle
    return brain, tracker, dispatched


async def _speech(brain, tracker):
    """The caller starts speaking: VAD probe + turn controller frames."""
    tracker.mark_speech_started()
    await brain.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def _vad_stop(brain, tracker):
    tracker.mark_speech_stopped()
    await brain.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.25), FrameDirection.DOWNSTREAM)


def _verdict(tracker, probability: float, complete: bool = True):
    """What EndpointReportingSmartTurnStrategy records before the controller
    emits UserStoppedSpeakingFrame."""
    tracker.note_smart_turn(probability=probability, is_complete=complete, inference_ms=85.0)
    if complete:
        tracker.mark_endpoint_by("smart_turn")


async def _dispatched(dispatched, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not dispatched and time.monotonic() < deadline:
        await asyncio.sleep(0.002)
    return bool(dispatched)


class TestShortCircuit:
    @pytest.mark.asyncio
    async def test_final_then_verdict_dispatches_at_the_turn_close(self):
        # The live order: the final arms complete_endpoint, the controller
        # closes the turn on the verdict a few ms later.
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        await brain.process_frame(transcript("हाँ, मैं बोल रहा हूँ।"), FrameDirection.DOWNSTREAM)
        assert brain._finalize_pending()                      # complete_endpoint armed (0.5 s)
        await asyncio.sleep(0.02)
        _verdict(tracker, 0.979)
        closed_at = time.monotonic()
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)
        text, at = dispatched[0]
        assert text == "हाँ, मैं बोल रहा हूँ।"
        assert at - closed_at < SLACK, f"dispatched {(at - closed_at) * 1000:.0f} ms after the close"
        event = brain._recorder.event("smart_turn_short_circuit")
        assert event["probability"] == 0.979 and event["threshold"] == 0.7
        assert event["endpoint_armed"] is True and event["final_after_close"] is False
        assert 0 <= event["final_age_ms"] < 200
        assert tracker.endpoint_by == "smart_turn"
        assert tracker.counts.get("smart_turn_short_circuits") == 1

    @pytest.mark.asyncio
    async def test_verdict_then_final_dispatches_when_the_final_lands(self):
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        _verdict(tracker, 0.95)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.08)
        assert dispatched == [] and brain._smart_turn_closed is True
        final_at = time.monotonic()
        await brain.process_frame(transcript("हाँ बोलिए।"), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)
        text, at = dispatched[0]
        assert text == "हाँ बोलिए।"
        assert at - final_at < SLACK, f"dispatched {(at - final_at) * 1000:.0f} ms after the final"
        event = brain._recorder.event("smart_turn_short_circuit")
        assert event["final_after_close"] is True and event["endpoint_armed"] is False
        assert brain._smart_turn_closed is False

    @pytest.mark.asyncio
    async def test_short_reply_no_longer_waits_its_own_window_either(self):
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)   # short_reply_endpoint armed (0.3 s)
        _verdict(tracker, 0.9)
        closed_at = time.monotonic()
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)
        assert dispatched[0][1] - closed_at < SLACK


class TestUnchangedBehaviour:
    @pytest.mark.asyncio
    async def test_below_the_threshold_the_complete_endpoint_still_runs(self):
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        await brain.process_frame(transcript("हाँ, मैं बोल रहा हूँ।"), FrameDirection.DOWNSTREAM)
        _verdict(tracker, 0.62)                                 # COMPLETE, but under 0.7
        armed = brain._finalize_task
        closed_at = time.monotonic()
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._finalize_task is armed                    # left alone, as before
        assert "smart_turn_short_circuit" not in brain._recorder.event_kinds()
        assert await _dispatched(dispatched)
        assert dispatched[0][1] - closed_at >= COMPLETE - 0.1

    @pytest.mark.asyncio
    async def test_tenant_threshold_is_honoured(self):
        brain, tracker, dispatched = make_brain(threshold=0.95)
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        await brain.process_frame(transcript("हाँ, मैं बोल रहा हूँ।"), FrameDirection.DOWNSTREAM)
        _verdict(tracker, 0.9)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert "smart_turn_short_circuit" not in brain._recorder.event_kinds()
        assert await _dispatched(dispatched)

    @pytest.mark.asyncio
    async def test_pause_window_close_keeps_the_settled_grace_rules(self):
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        _verdict(tracker, 0.3, complete=False)                  # INCOMPLETE: the pause window closed it
        tracker.mark_endpoint_by("pause_timeout")
        await brain.process_frame(transcript("नहीं मेरे पास अभी"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert "smart_turn_short_circuit" not in brain._recorder.event_kinds()
        assert brain._smart_turn_closed is False
        assert await _dispatched(dispatched)                    # finalize_grace, as before
        assert tracker.endpoint_by == "pause_timeout"

    @pytest.mark.asyncio
    async def test_a_new_speech_episode_forgets_an_unused_close(self):
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        _verdict(tracker, 0.95)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert brain._smart_turn_closed is True
        await _speech(brain, tracker)                           # the caller starts again: new episode
        assert brain._smart_turn_closed is False


class TestTenantField:
    def test_schema_default_and_bounds(self):
        field = next(f for f in TURN_DETECTION_FIELDS if f["key"] == "smart_turn_threshold")
        assert field["section"] == "end_of_turn" and field["default"] == {"browser": 0.7, "telephony": 0.7}
        assert TURN_DETECTION_BOUNDS["smart_turn_threshold"] == (0.5, 0.99)
        for transport in ("browser", "telephony"):
            assert TURN_DETECTION_DEFAULTS[transport]["smart_turn_threshold"] == 0.7

    def test_turn_timing_carries_the_endpoint_sub_marks(self):
        tracker = TurnLatencyTracker(session_id="s")
        tracker.mark_speech_started()
        tracker.mark_speech_stopped()
        tracker.note_smart_turn(probability=0.9, is_complete=True, inference_ms=85.0)
        tracker.mark_final()
        tracker.mark_dispatched()
        marks = tracker.structured()["endpoint_marks_ms"]
        assert set(marks) == {"verdict", "final", "dispatch"}
        assert all(isinstance(v, float) and v >= 0 for v in marks.values())
        assert marks["dispatch"] >= marks["final"] >= 0
        tracker.mark_speech_started()                           # next turn: marks reset
        assert tracker.structured()["endpoint_marks_ms"] == {"verdict": None, "final": None, "dispatch": None}
