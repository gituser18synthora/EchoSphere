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
import base64
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

from shared.bot_config import ResolvedBotConfig
from shared.turn_detection import TURN_DETECTION_DEFAULTS
from voice_runtime.pipeline import build_stt_service
from voice_runtime.sarvam_stt import (
    EndpointedSarvamSTTService,
    _CodecAwareStreamingClient,
)

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

    async def test_mid_speech_flush_is_marked_as_segment_not_utterance(
        self, monkeypatch
    ):
        forwarded = []

        async def _base_push(self, frame, direction=None):
            forwarded.append(frame)

        monkeypatch.setattr(SarvamSTTService, "push_frame", _base_push)
        service = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)
        service._physical_speech_active = True

        frame = TranscriptionFrame(
            "हाँ मैंने कॉल किया था और लोकेशन पर भी गया था।", "caller", "t"
        )
        await service.push_frame(frame)

        assert forwarded == [frame]
        assert frame.finalized is True
        assert frame._echosphere_mid_utterance is True

    async def test_post_vad_stop_final_is_not_marked_mid_utterance(
        self, monkeypatch
    ):
        forwarded = []

        async def _base_push(self, frame, direction=None):
            forwarded.append(frame)

        monkeypatch.setattr(SarvamSTTService, "push_frame", _base_push)
        service = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)
        service._physical_speech_active = False

        frame = TranscriptionFrame("गार्ड को दिया था।", "caller", "t")
        await service.push_frame(frame)

        assert forwarded == [frame]
        assert frame.finalized is True
        assert frame._echosphere_mid_utterance is False

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


class TestRawPcmTransport:
    @staticmethod
    def _config() -> ResolvedBotConfig:
        return ResolvedBotConfig(
            tenant_id="t",
            bot_id="b",
            bot_name="Test",
            version="1",
            published=True,
            language="hi-IN",
            languages=["hi-IN", "en-IN"],
            stt={
                "provider": "sarvam",
                "model": "saaras:v3",
                "api_key_reference": "env:TEST_SARVAM_API_KEY",
                "settings": {"input_encoding": "pcm_s16le"},
            },
        )

    def test_raw_codec_is_forwarded_during_websocket_connect(self):
        calls = []

        class _Client:
            def connect(self, **kwargs):
                calls.append(kwargs)
                return "context"

        client = _CodecAwareStreamingClient(_Client(), "pcm_s16le")
        assert client.connect(language_code="hi-IN") == "context"
        assert calls == [{
            "language_code": "hi-IN",
            "input_audio_codec": "pcm_s16le",
        }]

    async def test_raw_pcm_uses_sdk_envelope_with_connection_codec(self, monkeypatch):
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
        service = build_stt_service(
            self._config(),
            use_provider_vad=False,
        )
        sent = []

        class _Socket:
            async def transcribe(self, **kwargs):
                sent.append(kwargs)

        service._socket_client = _Socket()
        # Normally populated by STTService.start(StartFrame) before audio
        # arrives; avoid opening a real provider socket in this unit test.
        # Telephony audio stays native at 8 kHz; the per-message rate must
        # match the WebSocket connection setting used by Sarvam.
        service._sample_rate = 8000
        frames = [frame async for frame in service.run_stt(b"\x01\x02\x03\x04")]
        assert frames == [None]
        assert sent == [{
            "audio": base64.b64encode(b"\x01\x02\x03\x04").decode("ascii"),
            # Sarvam selects raw PCM from the connection query. Its generated
            # per-message model supports this legacy envelope value only.
            "encoding": "audio/wav",
            "sample_rate": 8000,
        }]
        service._socket_client = None
        await service.cleanup()

    async def test_clean_socket_close_does_not_emit_error_per_audio_chunk(
        self, monkeypatch
    ):
        import voice_runtime.sarvam_stt as sarvam_stt_module

        class _CleanClose(Exception):
            pass

        monkeypatch.setattr(sarvam_stt_module, "ConnectionClosed", _CleanClose)
        monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
        service = build_stt_service(self._config(), use_provider_vad=False)
        calls = 0

        class _Socket:
            async def transcribe(self, **kwargs):
                nonlocal calls
                calls += 1
                raise _CleanClose("received 1000 (OK); then sent 1000 (OK)")

        service._socket_client = _Socket()
        service._sample_rate = 16000
        first = [frame async for frame in service.run_stt(b"\x00\x00")]
        second = [frame async for frame in service.run_stt(b"\x00\x00")]

        assert first == [None]
        assert second == [None]
        assert calls == 1
        assert service._socket_send_failed is True
        service._socket_client = None
        await service.cleanup()


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


class TestMeasuredFinalLatencyWiring:
    """2026-09-24: the telephony pause window (0.4 s after VAD stop) closed the
    turn before 19 % of Sarvam finals existed; a merge's re-queued text was then
    dispatched alone and the final ran as a second turn. The adapter now
    advertises its measured final latency and the turn end waits for the final
    up to that bound (minus stop_secs) — finals still short-circuit the wait."""

    def test_sarvam_service_advertises_the_measured_p99(self, monkeypatch):
        from voice_runtime.pipeline import SARVAM_TTFS_P99_S

        monkeypatch.setenv("TEST_SARVAM_API_KEY", "test-key")
        service = build_stt_service(TestRawPcmTransport._config(), use_provider_vad=False)
        assert isinstance(service, EndpointedSarvamSTTService)
        assert service._ttfs_p99_latency == SARVAM_TTFS_P99_S == 0.8
        assert service.service_metadata_frame().ttfs_p99_latency == 0.8

    async def test_wait_bounded_by_the_advertised_p99_when_no_final_arrives(self):
        # stop_secs 0.3 (tenant telephony) → stt wait 0.5 s; policy window 0.4 s:
        # a lost final closes the turn at ~0.5 s, not at the 5 s processor fallback.
        from voice_runtime.turn_stop import FinalBoundedTurnStopStrategy

        stopped = []
        strategy = FinalBoundedTurnStopStrategy(user_speech_timeout=0.4, wait_for_transcript=True)
        strategy.add_event_handler("on_user_turn_stopped", lambda *a, **k: stopped.append(time.monotonic()))
        tm = TaskManager()
        tm.setup(asyncio.get_running_loop())
        await strategy.setup(tm)
        await strategy.process_frame(STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=0.8))
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        t0 = time.monotonic()
        await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.3))
        await asyncio.sleep(0.9)
        await strategy.cleanup()
        assert len(stopped) == 1
        assert 0.45 <= stopped[0] - t0 <= 0.7, stopped[0] - t0

    async def test_finalized_final_inside_the_window_keeps_the_policy_latency(self):
        from voice_runtime.turn_stop import FinalBoundedTurnStopStrategy

        stopped = []
        strategy = FinalBoundedTurnStopStrategy(user_speech_timeout=0.4, wait_for_transcript=True)
        strategy.add_event_handler("on_user_turn_stopped", lambda *a, **k: stopped.append(time.monotonic()))
        tm = TaskManager()
        tm.setup(asyncio.get_running_loop())
        await strategy.setup(tm)
        await strategy.process_frame(STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=0.8))
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        t0 = time.monotonic()
        await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.3))
        await asyncio.sleep(0.26)
        frame = TranscriptionFrame("haan bol raha hoon", "caller", "t")
        frame.finalized = True
        await strategy.process_frame(frame)
        await asyncio.sleep(0.4)
        await strategy.cleanup()
        assert len(stopped) == 1
        assert 0.38 <= stopped[0] - t0 <= 0.5, stopped[0] - t0

    async def test_late_final_closes_the_turn_when_it_arrives(self):
        from voice_runtime.turn_stop import FinalBoundedTurnStopStrategy

        stopped = []
        strategy = FinalBoundedTurnStopStrategy(user_speech_timeout=0.4, wait_for_transcript=True)
        strategy.add_event_handler("on_user_turn_stopped", lambda *a, **k: stopped.append(time.monotonic()))
        tm = TaskManager()
        tm.setup(asyncio.get_running_loop())
        await strategy.setup(tm)
        await strategy.process_frame(STTMetadataFrame(service_name="sarvam", ttfs_p99_latency=0.8))
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        t0 = time.monotonic()
        await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=0.3))
        await asyncio.sleep(0.45)            # final later than the 0.4 s window, inside the 0.5 s net
        assert stopped == []                 # the turn waited for it
        frame = TranscriptionFrame("haan bol raha hoon", "caller", "t")
        frame.finalized = True
        await strategy.process_frame(frame)
        await asyncio.sleep(0.05)
        await strategy.cleanup()
        assert len(stopped) == 1
        assert 0.44 <= stopped[0] - t0 <= 0.55, stopped[0] - t0
