"""Observability added after the 2026-09-17 live STT audit (local code only).

- FreeSWITCH fork ingress: per-message size, cadence gaps, optional pre-gate
  capture, and an end-of-call ``telephony_inbound_media`` summary.
- Silero confidence evidence: ``vad_segment`` events per VAD stop.
- Barge-in confirmation reason on the call's event stream.
- Sarvam: ``stt_final_missing`` when even the retried flush yields nothing.
"""

import asyncio
import os
import time

import numpy as np
import pytest
from pipecat.frames.frames import (
    InputAudioRawFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)

from voice_runtime import sarvam_stt as sarvam_mod
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.telephony import FreeSwitchAudioForkSerializer
from voice_runtime.vad_confidence import (
    ConfidenceTrackingSileroVADAnalyzer,
    VADConfidenceProbe,
    summarize_confidences,
)


class _Recorder:
    def __init__(self):
        self.events = []

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.events]


# ── fork ingress ──────────────────────────────────────────────────────────────
class TestForkIngressEvidence:
    def test_counts_messages_sizes_and_gaps(self, monkeypatch):
        ser = FreeSwitchAudioForkSerializer()
        clock = [100.0]
        monkeypatch.setattr("voice_runtime.telephony.time.monotonic", lambda: clock[0])
        frame = b"\x10\x00" * 160  # 20 ms @ 8 kHz
        for gap in (0.0, 0.02, 0.02, 0.35, 0.02, 0.8):
            clock[0] += gap
            out = asyncio.run(ser.deserialize(frame))
            assert isinstance(out, InputAudioRawFrame) and out.sample_rate == 8000
        clock[0] += 0.02
        asyncio.run(ser.deserialize(frame[:100]))  # a smaller message
        stats = ser.inbound_media_stats()
        assert stats["messages"] == 7
        assert stats["msg_bytes_min"] == 100 and stats["msg_bytes_max"] == 320
        assert stats["gaps_over_100ms"] == 2 and stats["gaps_over_500ms"] == 1
        assert stats["gap_max_ms"] == pytest.approx(800.0, abs=1.0)
        assert stats["total_bytes"] == 6 * 320 + 100
        assert stats["audio_seconds"] == pytest.approx((6 * 320 + 100) / 16000, abs=0.05)

    def test_pre_gate_capture_writes_raw_pcm_and_stops_at_cap(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ECHOSPHERE_FS_AUDIO_DEBUG_DIR", str(tmp_path))
        monkeypatch.setenv("ECHOSPHERE_FS_AUDIO_DEBUG_SECONDS", "1")  # 16 000 bytes
        ser = FreeSwitchAudioForkSerializer()
        chunk = np.arange(160, dtype="<i2").tobytes()
        for _ in range(60):  # 60 × 320 B = 19 200 B > cap
            asyncio.run(ser.deserialize(chunk))
        files = list(tmp_path.glob("echosphere-fork-*.s16le"))
        assert len(files) == 1
        data = files[0].read_bytes()
        assert len(data) == 16000
        assert data[:320] == chunk  # what the module sent, before any gate
        assert ser._debug_file is None  # closed at the cap

    def test_no_capture_without_the_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ECHOSPHERE_FS_AUDIO_DEBUG_DIR", raising=False)
        ser = FreeSwitchAudioForkSerializer()
        asyncio.run(ser.deserialize(b"\x00\x01" * 160))
        assert ser._debug_file is None and not list(tmp_path.iterdir())


# ── Silero confidence evidence ────────────────────────────────────────────────
class TestConfidenceEvidence:
    def test_summary_numbers(self):
        s = summarize_confidences([0.2, 0.6, 0.9, 0.95], threshold=0.58)
        assert s["windows"] == 4
        assert s["conf_max"] == 0.95
        assert s["above_threshold_ratio"] == 0.75
        assert summarize_confidences([], 0.58) == {"windows": 0}

    @pytest.mark.asyncio
    async def test_probe_records_a_segment_per_vad_stop(self):
        analyzer = ConfidenceTrackingSileroVADAnalyzer()
        analyzer.set_sample_rate(8000)
        recorder = _Recorder()
        probe = VADConfidenceProbe(analyzer, recorder)
        pushed = []

        async def _push(frame, direction=None):
            pushed.append(frame)

        probe.push_frame = _push
        # Quiet windows BEFORE the segment (two of them near-misses just
        # under the 0.7 default threshold), then the segment's own windows.
        now = time.monotonic()
        for i, c in enumerate([0.1, 0.55, 0.6]):
            analyzer._history.append((now - 0.9 + i * 0.1, c))
        # Start confirmed 0.25 s after speech began (start_secs), stop 0.3 s after it ended.
        started = VADUserStartedSpeakingFrame(); started.start_secs = 0.25
        await probe.process_frame(started, None)
        for i, c in enumerate([0.9, 0.95, 0.85, 0.5]):
            analyzer._history.append((time.monotonic() - 0.2 + i * 0.05, c))
        await asyncio.sleep(0.05)
        stopped = VADUserStoppedSpeakingFrame(); stopped.stop_secs = 0.0
        await probe.process_frame(stopped, None)
        assert [type(f) for f in pushed] == [VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame]
        kinds = recorder.kinds()
        assert kinds == ["vad_segment"]
        seg = recorder.events[0][1]
        assert seg["index"] == 1
        assert seg["threshold"] == analyzer.params.confidence
        assert seg["windows"] == 4
        assert seg["conf_max"] == 0.95
        assert seg["above_threshold_ratio"] == 0.75
        assert seg["near_miss_windows_before"] == 2


# ── barge-in reason ───────────────────────────────────────────────────────────
class TestBargeInReasonEvidence:
    @pytest.mark.asyncio
    async def test_reason_and_sustained_duration_are_reported(self, monkeypatch):
        seen = []
        strategy = WordConfirmedBargeInStrategy(
            min_words=2, vad_fallback_secs=0.5,
            on_confirmed=lambda why, sustained: seen.append((why, sustained)),
        )
        fired = []

        async def _trigger():
            fired.append(True)

        strategy.trigger_user_turn_started = _trigger
        from pipecat.frames.frames import BotStartedSpeakingFrame, TranscriptionFrame

        await strategy.process_frame(BotStartedSpeakingFrame())
        await strategy.process_frame(VADUserStartedSpeakingFrame())
        clock = time.monotonic()
        monkeypatch.setattr("voice_runtime.barge_in.time.monotonic", lambda: clock + 0.6)
        await strategy.process_frame(TranscriptionFrame("ek", "u", "t"))  # 1 word: fallback fires
        assert fired == [True]
        assert len(seen) == 1
        why, sustained = seen[0]
        assert why.startswith("sustained VAD speech")
        assert sustained == pytest.approx(0.6, abs=0.05)


# ── Sarvam retry outcome ──────────────────────────────────────────────────────
class TestMissingFinalOutcome:
    @pytest.mark.asyncio
    async def test_event_when_the_retried_flush_also_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(sarvam_mod, "_MISSING_FINAL_RETRY_S", 0.01)
        recorder = _Recorder()
        flushes = []

        class _Client:
            async def flush(self):
                flushes.append(True)

        svc = object.__new__(sarvam_mod.EndpointedSarvamSTTService)
        svc._recorder = recorder
        svc._socket_client = _Client()
        svc._utterance_generation = 3
        svc._transcript_generation = 2
        svc._stt_stopping = False
        await sarvam_mod.EndpointedSarvamSTTService._missing_final_retry(svc, 3)
        assert flushes == [True]
        assert recorder.kinds() == ["stt_missing_final_retry", "stt_final_missing"]
        assert recorder.events[1][1]["generation"] == 3

    @pytest.mark.asyncio
    async def test_no_loss_event_when_the_retry_recovers(self, monkeypatch):
        monkeypatch.setattr(sarvam_mod, "_MISSING_FINAL_RETRY_S", 0.01)
        recorder = _Recorder()

        class _Client:
            async def flush(self):
                svc._transcript_generation = 3  # the transcript arrives

        svc = object.__new__(sarvam_mod.EndpointedSarvamSTTService)
        svc._recorder = recorder
        svc._socket_client = _Client()
        svc._utterance_generation = 3
        svc._transcript_generation = 2
        svc._stt_stopping = False
        await sarvam_mod.EndpointedSarvamSTTService._missing_final_retry(svc, 3)
        assert recorder.kinds() == ["stt_missing_final_retry"]
