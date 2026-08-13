"""Regressions for the 2026-08-11 latency / barge-in audit fixes.

Each test pins one verified defect from the audit:

- the caller-audio gate evicting the onset of a barge-in because preroll was
  shorter than the echo-guard sustain requirement;
- telephony noise-gate defaults stricter than the browser's on the quieter
  medium (echo margin, absolute threshold floor);
- the transcript gate rejecting genuine one-word data answers (names,
  amounts, references) on the single_token + bot_audio_overlap pair;
- STT final dedup treating a replayed metric-less final as new (timestamp in
  the identity key) while a time-bounded window keeps genuine repeats
  answerable;
- the Sarvam barge-in flush loop (mid-utterance finals while the caller talks
  over the bot).
"""

import asyncio

import pytest

from pipecat.frames.frames import TranscriptionFrame

import voice_runtime.brain as brain_module
from shared.turn_detection import (
    NOISE_GATE_DEFAULTS,
    TURN_DETECTION_BOUNDS,
    TURN_DETECTION_DEFAULTS,
)
from voice_runtime.audio_gate import CallerAudioGate
from voice_runtime.brain import ConversationBrain
from voice_runtime.sarvam_stt import EndpointedSarvamSTTService
from voice_runtime.stt_events import final_event_key
from voice_runtime.transcript_gate import SegmentQuality, assess_transcript


class TestAudioGatePrerollClamp:
    def test_preroll_covers_the_echo_guard_open_delay(self):
        # preroll (160) < echo_min_speech_ms (200) evicted the first ~40 ms
        # of every barge-in before the gate opened.
        gate = CallerAudioGate(
            preroll_ms=160.0, min_speech_ms=120.0, echo_min_speech_ms=200.0
        )
        assert gate._preroll_ms == 200.0

    def test_generous_preroll_is_kept(self):
        gate = CallerAudioGate(
            preroll_ms=400.0, min_speech_ms=120.0, echo_min_speech_ms=200.0
        )
        assert gate._preroll_ms == 400.0


class TestTurnDetectionDefaults:
    def test_vad_fallback_key_exists_with_bounds(self):
        for transport in ("browser", "telephony"):
            assert "barge_in_vad_fallback_secs" in TURN_DETECTION_DEFAULTS[transport]
        low, high = TURN_DETECTION_BOUNDS["barge_in_vad_fallback_secs"]
        assert low == 0.0 and high >= 3.0

    def test_telephony_gate_is_not_stricter_than_browser(self):
        telephony = NOISE_GATE_DEFAULTS["telephony"]
        browser = NOISE_GATE_DEFAULTS["browser"]
        # PSTN audio is quieter: the absolute floor must not sit above the
        # browser's, and the echo margin must leave normal-volume speech
        # audible while the bot talks.
        assert telephony["min_threshold_dbfs"] <= browser["min_threshold_dbfs"]
        assert telephony["echo_margin_db"] <= 6.0


class TestTranscriptGateDataAnswers:
    def test_bare_amount_survives_weak_signal_pair(self):
        # "5000" answered promptly (bot_audio_overlap via the echo tail) and
        # single_token — previously rejected, forcing the caller to repeat.
        verdict = assess_transcript(
            "5000", SegmentQuality(during_bot_audio=True, audio_seconds=0.4)
        )
        assert verdict.accepted

    def test_clear_single_word_during_bot_audio_is_accepted(self):
        verdict = assess_transcript(
            "Ramesh", SegmentQuality(during_bot_audio=True, snr_db=18.0)
        )
        assert verdict.accepted

    def test_quiet_single_word_during_bot_audio_is_still_echo(self):
        # Near the noise floor the overlap explanation stands: low_snr +
        # single_token still corroborate.
        verdict = assess_transcript(
            "Ramesh", SegmentQuality(during_bot_audio=True, snr_db=4.0)
        )
        assert not verdict.accepted


def _sarvam_final(text: str, timestamp: str) -> TranscriptionFrame:
    return TranscriptionFrame(
        text=text,
        user_id="u",
        timestamp=timestamp,
        result={"data": {"request_id": "conn-1", "transcript": text}},
    )


class TestFinalDedupReplayWindow:
    def test_metricless_key_ignores_the_frame_timestamp(self):
        # A replayed provider message is rebuilt as a fresh frame with a
        # fresh timestamp; the identity must not change with it.
        a = final_event_key(_sarvam_final("हाँ ठीक है", "t1"), "हाँ ठीक है")
        b = final_event_key(_sarvam_final("हाँ ठीक है", "t2"), "हाँ ठीक है")
        assert a == b

    def _brain(self) -> ConversationBrain:
        stub = ConversationBrain.__new__(ConversationBrain)
        stub._seen_finals = {}
        return stub

    def test_immediate_redelivery_is_a_duplicate(self):
        stub = self._brain()
        frame = _sarvam_final("हाँ ठीक है", "t1")
        assert not stub._is_duplicate_final(frame, "हाँ ठीक है")
        replay = _sarvam_final("हाँ ठीक है", "t2")
        assert stub._is_duplicate_final(replay, "हाँ ठीक है")

    def test_genuine_repeat_after_the_window_is_answered(self, monkeypatch):
        monkeypatch.setattr(brain_module, "_SEEN_FINALS_REPLAY_WINDOW", 0.05)
        stub = self._brain()
        assert not stub._is_duplicate_final(
            _sarvam_final("हाँ", "t1"), "हाँ"
        )
        import time as _time

        _time.sleep(0.06)
        assert not stub._is_duplicate_final(
            _sarvam_final("हाँ", "t2"), "हाँ"
        )


class TestSarvamBargeInFlush:
    def _service(self):
        svc = EndpointedSarvamSTTService.__new__(EndpointedSarvamSTTService)
        svc._bot_speaking = True
        svc._barge_in_flush_task = None

        def _create(coro, name=None):
            return asyncio.get_event_loop().create_task(coro)

        async def _cancel(task, timeout=None):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        svc.create_task = _create
        svc.cancel_task = _cancel
        return svc

    async def test_flush_loop_forces_periodic_finals(self, monkeypatch):
        import voice_runtime.sarvam_stt as sarvam_stt_module

        monkeypatch.setattr(
            sarvam_stt_module, "_BARGE_IN_FLUSH_INTERVAL_S", 0.01
        )
        svc = self._service()
        flushes = []

        class _Socket:
            async def flush(self):
                flushes.append(True)

        svc._socket_client = _Socket()
        svc._start_barge_in_flush()
        await asyncio.sleep(0.05)
        await svc._stop_barge_in_flush()
        assert len(flushes) >= 2

    async def test_stop_cancels_the_loop(self, monkeypatch):
        import voice_runtime.sarvam_stt as sarvam_stt_module

        monkeypatch.setattr(
            sarvam_stt_module, "_BARGE_IN_FLUSH_INTERVAL_S", 0.01
        )
        svc = self._service()
        flushes = []

        class _Socket:
            async def flush(self):
                flushes.append(True)

        svc._socket_client = _Socket()
        svc._start_barge_in_flush()
        await asyncio.sleep(0.025)
        await svc._stop_barge_in_flush()
        settled = len(flushes)
        await asyncio.sleep(0.03)
        assert len(flushes) == settled

    async def test_missing_socket_keeps_the_loop_alive_for_reconnect(
        self, monkeypatch
    ):
        """A socket gap (mid-reconnect) must not end the loop: it resumes
        flushing the moment a live client is back, so transcript-confirmed
        barge-in survives an STT reconnect. Stop/turn-close still cancels it
        (covered above)."""
        import voice_runtime.sarvam_stt as sarvam_stt_module

        monkeypatch.setattr(
            sarvam_stt_module, "_BARGE_IN_FLUSH_INTERVAL_S", 0.01
        )
        svc = self._service()
        svc._socket_client = None
        svc._start_barge_in_flush()
        await asyncio.sleep(0.03)
        assert not svc._barge_in_flush_task.done()

        flushes = []

        class _Socket:
            async def flush(self):
                flushes.append(True)

        svc._socket_client = _Socket()
        await asyncio.sleep(0.03)
        await svc._stop_barge_in_flush()
        assert flushes

    async def test_flush_failure_does_not_kill_the_loop(self, monkeypatch):
        """One failed flush (socket died between ticks) must not disable
        barge-in for the rest of the call."""
        import voice_runtime.sarvam_stt as sarvam_stt_module

        monkeypatch.setattr(
            sarvam_stt_module, "_BARGE_IN_FLUSH_INTERVAL_S", 0.01
        )
        svc = self._service()
        flushes = []

        class _FlakySocket:
            def __init__(self):
                self.calls = 0

            async def flush(self):
                self.calls += 1
                if self.calls == 1:
                    raise ConnectionError("socket died")
                flushes.append(True)

        svc._socket_client = _FlakySocket()
        svc._start_barge_in_flush()
        await asyncio.sleep(0.05)
        await svc._stop_barge_in_flush()
        assert flushes
