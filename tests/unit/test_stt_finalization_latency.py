"""STT finalization signalling and the turn-close latency it controls.

Pipecat's turn-stop strategy runs two timers after a VAD stop and closes the
turn when BOTH finish: our ``user_speech_timeout`` policy window, and an
``stt_timeout`` safety net worth ``ttfs_p99_latency - stop_secs`` that exists to
cover a slow provider. The net is short-circuited by a transcript arriving with
``finalized=True``.

``SarvamSTTService`` never set that flag, so the net always ran: with
``SARVAM_TTFS_P99 = 1.17`` and ``stop_secs = 0.2`` that is a fixed ~970 ms wait
after every single utterance, silently overriding the 800 ms telephony window —
and making the window look inert, because lowering it changed nothing.

These tests pin both halves: that our subclass supplies the flag, and that the
flag is what actually moves turn-close latency.
"""

import asyncio
import time

import pytest
from pipecat.frames.frames import (
    STTMetadataFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.stt_latency import SARVAM_TTFS_P99
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.utils.asyncio.task_manager import TaskManager

from shared.turn_detection import TURN_DETECTION_DEFAULTS
from voice_runtime.sarvam_stt import EndpointedSarvamSTTService

STOP_SECS = TURN_DETECTION_DEFAULTS["telephony"]["stop_secs"]
UST = TURN_DETECTION_DEFAULTS["telephony"]["user_speech_timeout"]
# What the un-finalized safety net costs after every utterance.
STT_NET_SECS = SARVAM_TTFS_P99 - STOP_SECS


class TestFinalizedFlag:
    async def test_service_marks_transcription_finals_as_finalized(self, monkeypatch):
        forwarded = []

        async def _base_push(self, frame, direction=None):
            forwarded.append(frame)

        # Patch the parent so super().push_frame() resolves to the stub; the
        # override under test is still the real one.
        monkeypatch.setattr(SarvamSTTService, "push_frame", _base_push)
        service = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)

        frame = TranscriptionFrame("मैं कल कर दूंगा", "caller", "t")
        assert frame.finalized is False  # what upstream shipped
        await service.push_frame(frame)
        assert forwarded == [frame]
        assert frame.finalized is True

    async def test_already_finalized_frame_is_left_alone(self, monkeypatch):
        async def _base_push(self, frame, direction=None):
            pass

        monkeypatch.setattr(SarvamSTTService, "push_frame", _base_push)
        service = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)
        frame = TranscriptionFrame("हाँ", "caller", "t")
        frame.finalized = True
        await service.push_frame(frame)
        assert frame.finalized is True

    async def test_non_transcription_frames_pass_through_untouched(self, monkeypatch):
        forwarded = []

        async def _base_push(self, frame, direction=None):
            forwarded.append(frame)

        monkeypatch.setattr(SarvamSTTService, "push_frame", _base_push)
        service = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)
        frame = VADUserStoppedSpeakingFrame(stop_secs=0.2)
        await service.push_frame(frame)
        assert forwarded == [frame]


async def close_turn(*, finalized: bool, stt_lag: float,
                     user_speech_timeout: float = UST) -> float:
    """Seconds from VAD stop to the strategy closing the user turn."""
    manager = TaskManager()
    strategy = SpeechTimeoutUserTurnStopStrategy(
        user_speech_timeout=user_speech_timeout, wait_for_transcript=False
    )
    await strategy.setup(manager)
    stopped = asyncio.Event()
    strategy.add_event_handler("on_user_turn_stopped", lambda *a, **k: stopped.set())

    # Broadcast at pipeline start by every STT service.
    await strategy.process_frame(
        STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=SARVAM_TTFS_P99)
    )
    await strategy.process_frame(VADUserStartedSpeakingFrame())

    start = time.monotonic()
    await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=STOP_SECS))

    async def deliver():
        await asyncio.sleep(stt_lag)
        frame = TranscriptionFrame("मैं कल पेमेंट कर दूंगा", "caller", "t")
        frame.finalized = finalized
        await strategy.process_frame(frame)

    task = asyncio.ensure_future(deliver())
    try:
        await asyncio.wait_for(stopped.wait(), 5)
    finally:
        await task
        await strategy.cleanup()
    return time.monotonic() - start


@pytest.mark.perf
class TestTurnCloseLatency:
    async def test_unfinalized_transcript_is_bound_by_the_stt_safety_net(self):
        # The bug: turn close waits out the provider-latency net even though
        # the transcript has been in hand for hundreds of milliseconds.
        elapsed = await close_turn(finalized=False, stt_lag=0.2)
        assert elapsed >= STT_NET_SECS - 0.05
        assert elapsed > UST + 0.1, (
            "the policy window should NOT be the binding constraint here"
        )

    async def test_lowering_the_policy_window_alone_does_not_help(self):
        # Why "just reduce the timeout" was never the fix.
        slow = await close_turn(finalized=False, stt_lag=0.2, user_speech_timeout=UST)
        fast = await close_turn(finalized=False, stt_lag=0.2, user_speech_timeout=0.3)
        assert abs(slow - fast) < 0.1, (
            f"cutting the window moved latency by {(slow - fast) * 1000:.0f}ms"
        )

    async def test_finalized_transcript_closes_on_the_policy_window(self):
        elapsed = await close_turn(finalized=True, stt_lag=0.2)
        assert elapsed == pytest.approx(UST, abs=0.12)
        assert elapsed < STT_NET_SECS - 0.05

    async def test_finalization_never_closes_the_turn_before_the_window(self):
        # The pause a caller is entitled to must survive the fix: a transcript
        # arriving early must not shorten the window below policy.
        elapsed = await close_turn(finalized=True, stt_lag=0.01)
        assert elapsed >= UST - 0.05

    async def test_finalization_saves_the_safety_net_wait(self):
        before = await close_turn(finalized=False, stt_lag=0.2)
        after = await close_turn(finalized=True, stt_lag=0.2)
        assert before - after >= 0.1, (
            f"expected the net to be skipped; saved only "
            f"{(before - after) * 1000:.0f}ms"
        )
