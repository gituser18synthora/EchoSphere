"""Finals that arrive after their turn, and a Smart Turn close that is still
waiting for its own final.

Live 2026-10-23, after 5f82302bb (demo, SARVAM_REALTIME_STT=false):

- cv_2d1119054a92 (Haier): the caller said "haan. call karke bahut ulta
  seedha bola" over two VAD episodes. The flush retry recovered "call" from
  the first; Smart Turn then closed the second episode with a confident
  verdict and the brain dispatched "…call" at once because SOMETHING was
  buffered — the second episode's own final ("karke bahut ulta seedha bola")
  landed 36 ms later and ran as a separate turn ("sorry, I did not get
  that"). A short-circuit needs the final of the CURRENT speech episode;
  older buffered text waits for it, bounded.
- cv_0f224432545d (MDND): finals arrived with no VAD at all for a short
  "haan ji, haan" over the bot's audio; the turn they opened ended on
  pipecat's transcript fallback timer, the strategy still reported
  ``endpoint_by=smart_turn`` and the brain short-circuited on the PREVIOUS
  turn's verdict (p 0.71). A verdict belongs to the turn it was computed in.
- A final delivered long after its turn closed, with no caller audio seen
  since by the VAD or the gate, is the provider's old speech: it is logged
  (``stt_final_late``) and dropped instead of answering a newer question.
- The bounded turn-stop never closes a turn EMPTY before the Sarvam
  adapter's flush retry (2 x 0.65 s) could still deliver the final; the
  caller's pause window stays the outer bound.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TurnMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

import voice_runtime.brain as brain_module
from tests.unit.test_smart_turn_short_circuit import _dispatched, _vad_stop, _verdict, make_brain
from tests.unit.test_turn_endpointing import transcript
from voice_runtime.smart_turn import EndpointReportingSmartTurnStrategy
from voice_runtime.turn_metrics import TurnLatencyTracker
from voice_runtime.turn_stop import FinalBoundedTurnStopStrategy


async def _speech(brain, tracker):
    tracker.mark_speech_started()
    await brain.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)


class TestEpisodeFinal:
    @pytest.mark.asyncio
    async def test_a_confident_close_waits_for_its_own_episodes_final(self, monkeypatch):
        monkeypatch.setattr(brain_module, "_SMART_TURN_FINAL_WAIT_S", 0.4)
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)                         # episode 1 ("haan" — flush answered late)
        await brain.process_frame(transcript("call"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await _vad_stop(brain, tracker)                         # episode 2: its final is still in flight
        _verdict(tracker, 0.978)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await asyncio.sleep(0.05)
        assert dispatched == [] and brain._smart_turn_closed is True   # "call" alone is not the turn
        await brain.process_frame(transcript("करके बहुत उल्टा सीधा बोला।"), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)
        assert dispatched[0][0] == "call करके बहुत उल्टा सीधा बोला।"   # one turn, one reply
        assert brain._recorder.event("smart_turn_short_circuit")["final_after_close"] is True

    @pytest.mark.asyncio
    async def test_buffered_text_still_dispatches_when_no_final_ever_comes(self, monkeypatch):
        monkeypatch.setattr(brain_module, "_SMART_TURN_FINAL_WAIT_S", 0.3)
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        await brain.process_frame(transcript("call"), FrameDirection.DOWNSTREAM)
        await brain.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        await _vad_stop(brain, tracker)
        _verdict(tracker, 0.95)
        closed_at = time.monotonic()
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)
        assert dispatched[0][0] == "call"
        assert 0.25 <= dispatched[0][1] - closed_at < 0.6      # the bounded wait, not forever

    @pytest.mark.asyncio
    async def test_the_previous_turns_verdict_never_short_circuits_a_vad_less_turn(self):
        brain, tracker, dispatched = make_brain()
        await _speech(brain, tracker)
        await _vad_stop(brain, tracker)
        await brain.process_frame(transcript("हाँ"), FrameDirection.DOWNSTREAM)
        _verdict(tracker, 0.71)
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)                    # turn 1: a real verdict
        dispatched.clear()
        # Turn 2 opens from a transcript the VAD never saw (bot echo, quiet
        # "haan ji"): no VAD frames, no new verdict.
        await brain.process_frame(UserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert tracker.smart_turn_probability is None          # marks reset for the new turn
        await brain.process_frame(transcript("हाँ जी, हाँ।"), FrameDirection.DOWNSTREAM)
        tracker.mark_endpoint_by("transcript_timeout")          # what the strategy now reports
        await brain.process_frame(UserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
        assert await _dispatched(dispatched)
        events = [k for k, _ in brain._recorder.events if k == "smart_turn_short_circuit"]
        assert len(events) == 1                                 # only turn 1's


# ── the strategy reports a verdict only for the turn it was computed in ─────

class _Analyzer:
    params = type("P", (), {"stop_secs": 0.7})()

    def set_sample_rate(self, rate):
        pass

    def update_vad_start_secs(self, secs):
        pass

    def append_audio(self, audio, is_speech):
        return EndOfTurnState.INCOMPLETE

    async def analyze_end_of_turn(self):
        return EndOfTurnState.COMPLETE, TurnMetricsData(
            processor="BaseSmartTurn", is_complete=True, probability=0.9, e2e_processing_time_ms=80.0)

    def clear(self):
        pass

    async def cleanup(self):
        pass


class TestStrategyVerdictScope:
    @pytest.mark.asyncio
    async def test_no_verdict_in_this_turn_is_not_a_smart_turn_close(self, monkeypatch):
        tracker = TurnLatencyTracker(session_id="s")
        strategy = EndpointReportingSmartTurnStrategy(turn_analyzer=_Analyzer(), wait_for_transcript=True, tracker=tracker)
        await strategy.setup(FrameProcessorSetup(
            clock=SystemClock(), task_manager=TaskManager(), audio_in_sample_rate=8000,
            audio_out_sample_rate=8000, pipeline_worker=None))
        triggered = []

        async def _trigger(self_inner, **kwargs):
            triggered.append(True)

        async def _push(frame, direction=None):
            pass

        monkeypatch.setattr(strategy, "push_frame", _push)
        monkeypatch.setattr(type(strategy).__mro__[1], "trigger_user_turn_stopped", _trigger)
        try:
            await strategy.handle_user_turn_started()
            await strategy.trigger_user_turn_stopped()          # transcript fallback: no verdict
            assert tracker.endpoint_by == "transcript_timeout"
            tracker.mark_speech_started()
            await strategy.handle_user_turn_started()
            await strategy._handle_prediction_result(TurnMetricsData(
                processor="BaseSmartTurn", is_complete=True, probability=0.93, e2e_processing_time_ms=80.0))
            await strategy.trigger_user_turn_stopped()
            assert tracker.endpoint_by == "smart_turn" and tracker.verdict_at is not None
            tracker.mark_speech_started()
            await strategy.handle_user_turn_started()           # the flag resets per turn
            await strategy.trigger_user_turn_stopped()
            assert tracker.endpoint_by == "transcript_timeout"
        finally:
            await strategy.cleanup()
