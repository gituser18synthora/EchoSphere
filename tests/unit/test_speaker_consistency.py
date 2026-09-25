"""Caller speaker-consistency prototype (voice_runtime.speaker_consistency)."""

import os

import numpy as np
import pytest
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from voice_runtime.speaker_consistency import (
    ATTR_CALLER,
    ATTR_MISMATCH,
    ATTR_UNCERTAIN,
    ATTR_UNKNOWN,
    MIN_REFERENCE_SECONDS,
    MODE_ENFORCE,
    MODE_OFF,
    MODE_SHADOW,
    CallerAudioTap,
    OnnxGE2EBackend,
    SpeakerConsistency,
    mode_from_setting,
)

SR = 8000


class Recorder:
    def __init__(self):
        self.events = []

    def add_event(self, kind, **data):
        self.events.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.events]


class VoiceBackend:
    """Fake embedder: the 'voice' is the first sample value of the PCM (an id
    byte), mapped to fixed unit vectors; noise adds a small perturbation."""

    name = "fake"

    def __init__(self):
        rng = np.random.default_rng(0)
        self._voices = {v: self._unit(rng.standard_normal(16)) for v in range(1, 6)}

    @staticmethod
    def _unit(v):
        return (v / np.linalg.norm(v)).astype(np.float32)

    def embed(self, wav16):
        vid = int(round(float(wav16[0]) * 1000))
        if vid not in self._voices:
            return None
        mix = self._voices[vid] + 0.05 * float(wav16[1]) * 100  # tiny perturbation from sample 1
        return self._unit(mix)


def voice_pcm(voice_id: int, seconds: float, jitter: float = 0.0) -> bytes:
    n = int(seconds * SR)
    x = np.zeros(n, dtype=np.float32)
    x[0] = voice_id / 1000.0
    x[1] = jitter / 100.0
    x[2:] = 0.01
    return (x * 32768).astype("<i2").tobytes()


def make(mode=MODE_SHADOW, backend=None):
    rec = Recorder()
    return SpeakerConsistency(backend=backend if backend is not None else VoiceBackend(), mode=mode, recorder=rec), rec


class TestReference:
    def test_reference_only_from_vouched_calls_and_quality_rules(self):
        spk, rec = make()
        assert not spk.reference_available
        upd = spk.add_reference(voice_pcm(1, 0.5), SR, seconds=0.5, reason="workflow_advanced")
        assert not upd.accepted and upd.reason == "too_short"
        upd = spk.add_reference(voice_pcm(1, 2.0), SR, seconds=2.0, reason="workflow_advanced", during_bot_audio=True)
        assert not upd.accepted and upd.reason == "during_bot_audio"
        upd = spk.add_reference(voice_pcm(1, 2.0), SR, seconds=2.0, reason="identity_confirmed")
        assert upd.accepted and spk.reference_available and spk.reference_segments == 1
        assert spk.reference_seconds == 2.0
        assert rec.kinds().count("speaker_reference") == 3
        assert rec.events[-1][1]["accepted"] is True and rec.events[-1][1]["reason"] == "identity_confirmed"

    def test_contamination_guard_rejects_a_different_voice_even_when_vouched(self):
        spk, rec = make()
        assert spk.add_reference(voice_pcm(1, 2.0), SR, seconds=2.0, reason="identity_confirmed").accepted
        upd = spk.add_reference(voice_pcm(2, 2.0), SR, seconds=2.0, reason="workflow_advanced")
        assert not upd.accepted and upd.reason == "contamination_guard" and upd.distance_to_existing > 0.45
        assert spk.reference_segments == 1
        # The same voice again is merged.
        assert spk.add_reference(voice_pcm(1, 1.5, jitter=0.3), SR, seconds=1.5, reason="workflow_advanced").accepted
        assert spk.reference_segments == 2 and spk.reference_seconds == 3.5

    def test_reference_memory_is_bounded(self):
        spk, _ = make()
        for i in range(6):
            assert spk.add_reference(voice_pcm(1, 3.0, jitter=0.1 * i), SR, seconds=3.0, reason="workflow_advanced").accepted
        assert spk.reference_segments <= 4 and spk.reference_seconds <= 9.0


class TestScoring:
    def test_unknown_before_a_reference_exists_and_for_short_or_backendless(self):
        spk, rec = make()
        ev = spk.score(voice_pcm(1, 2.0), SR, seconds=2.0)
        assert ev.attribution == ATTR_UNKNOWN and ev.reason == "no_reference" and not ev.reference_available
        spk.add_reference(voice_pcm(1, 2.0), SR, seconds=2.0, reason="identity_confirmed")
        ev = spk.score(voice_pcm(1, 0.4), SR, seconds=0.4)
        assert ev.attribution == ATTR_UNKNOWN and ev.reason == "too_short"
        none, _ = make(backend=None)
        none._backend = None
        assert none.score(voice_pcm(1, 2.0), SR, seconds=2.0).reason == "no_backend"
        assert rec.kinds().count("speaker_consistency") == 2

    def test_same_voice_is_caller_and_another_voice_is_mismatch(self):
        spk, rec = make()
        spk.add_reference(voice_pcm(1, 2.0), SR, seconds=2.0, reason="identity_confirmed")
        same = spk.score(voice_pcm(1, 1.0, jitter=0.2), SR, seconds=1.0, context={"question_open": True})
        assert same.attribution == ATTR_CALLER and same.distance < 0.35 and same.enforceable is False
        other = spk.score(voice_pcm(3, 1.0), SR, seconds=1.0)
        assert other.attribution == ATTR_MISMATCH and other.distance >= 0.45 and other.enforceable is False  # shadow
        ev = rec.events[-1][1]
        assert ev["attribution"] == ATTR_MISMATCH and ev["mode"] == "shadow" and ev["reference_segments"] == 1
        assert rec.events[-2][1]["question_open"] is True
        assert spk.stats["caller"] == 1 and spk.stats["mismatch"] == 1 and spk.stats["scored"] == 2

    def test_uncertain_band_and_enforce_flag(self):
        spk, _ = make(mode=MODE_ENFORCE)
        spk.add_reference(voice_pcm(1, 2.0), SR, seconds=2.0, reason="identity_confirmed")
        d = spk.score(voice_pcm(3, 1.0), SR, seconds=1.0).distance
        spk.caller_max, spk.mismatch_min = d - 0.05, d + 0.05   # put the band around the measured distance
        assert spk.score(voice_pcm(3, 1.0), SR, seconds=1.0).attribution == ATTR_UNCERTAIN
        spk.caller_max, spk.mismatch_min = 0.35, min(0.45, d - 0.01)
        ev = spk.score(voice_pcm(3, 1.0), SR, seconds=1.0)
        assert ev.attribution == ATTR_MISMATCH and ev.enforceable is True

    def test_mode_setting_and_validation(self):
        assert mode_from_setting(0) == MODE_OFF and mode_from_setting(1.0) == MODE_SHADOW and mode_from_setting(2) == MODE_ENFORCE
        assert mode_from_setting("shadow") == MODE_SHADOW and mode_from_setting(None) == MODE_OFF and mode_from_setting(9) == MODE_OFF
        with pytest.raises(ValueError):
            SpeakerConsistency(backend=None, mode="loud")

    def test_platform_default_is_off(self):
        from shared.turn_detection import TURN_DETECTION_DEFAULTS, TURN_DETECTION_RECOMMENDED

        for t in ("browser", "telephony"):
            assert TURN_DETECTION_DEFAULTS[t]["speaker_consistency_mode"] == 0.0
            assert TURN_DETECTION_RECOMMENDED[t]["speaker_consistency_mode"] == 0.0


class TestTap:
    async def test_tap_keeps_passed_audio_and_returns_the_latest_run(self):
        clock = {"t": 100.0}
        tap = CallerAudioTap(max_seconds=2.0, gap_seconds=0.5, clock=lambda: clock["t"])
        pushed = []

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        tap.push_frame = _push
        loud = (np.full(160, 1000, dtype="<i2")).tobytes()
        silent = bytes(320)
        for i in range(10):                       # 200 ms of passed audio
            clock["t"] += 0.02
            await tap.process_frame(InputAudioRawFrame(audio=loud, sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        for i in range(50):                       # 1 s of gate silence: not stored, does not break the run
            clock["t"] += 0.02
            await tap.process_frame(InputAudioRawFrame(audio=silent, sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        for i in range(15):                       # a new 300 ms run
            clock["t"] += 0.02
            await tap.process_frame(InputAudioRawFrame(audio=loud, sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        assert len(pushed) == 75                  # pure observer
        pcm, rate, seconds = tap.take_recent(5.0)
        assert rate == SR and abs(seconds - 0.3) < 1e-6 and len(pcm) == 15 * 320   # only the latest contiguous run
        pcm, _, seconds = tap.take_recent(0.1)
        assert abs(seconds - 0.1) < 1e-6
        for i in range(200):                      # ring stays bounded
            clock["t"] += 0.02
            await tap.process_frame(InputAudioRawFrame(audio=loud, sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        assert tap._seconds <= 2.0 + 0.02


    async def test_tap_survives_pipecat_owning_the_clock_attribute(self):
        # pipecat's FrameProcessor sets ``_clock`` to its SystemClock when the
        # pipeline starts; the tap must keep its own clock and, whatever
        # happens in its bookkeeping, always pass the frame on.
        tap = CallerAudioTap()
        tap._clock = object()      # what pipecat does to every processor
        pushed = []

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            pushed.append(frame)

        tap.push_frame = _push
        loud = (np.full(160, 1000, dtype="<i2")).tobytes()
        for _ in range(5):
            await tap.process_frame(InputAudioRawFrame(audio=loud, sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        assert len(pushed) == 5
        assert tap.take_recent(5.0)[2] == pytest.approx(0.1)
        tap._tap_clock = None      # a broken clock must not stop audio either
        await tap.process_frame(InputAudioRawFrame(audio=loud, sample_rate=SR, num_channels=1), FrameDirection.DOWNSTREAM)
        assert len(pushed) == 6


MODEL = "storage/models/speaker/ge2e_lstm.onnx"


@pytest.mark.skipif(not os.path.isfile(MODEL), reason="local model asset not present")
class TestOnnxBackend:
    def test_embeds_known_voices_and_separates_them(self):
        import glob
        import json
        import wave

        clips = "/tmp/claude-1000/-var-www-html-python-EchoSphere/e5f54338-fbd7-482a-9eb2-e0d281b276b1/scratchpad/e2e/clips"
        if not os.path.isdir(clips):
            pytest.skip("emulator clips not present")
        backend = OnnxGE2EBackend.load(MODEL)
        assert backend is not None and backend.model_bytes > 1_000_000
        meta = json.load(open(os.path.join(clips, "meta.json")))
        spk = SpeakerConsistency(backend=backend, mode=MODE_SHADOW)

        def pcm(name):
            w = wave.open(os.path.join(clips, name + ".wav"))
            return w.readframes(w.getnframes()), w.getframerate(), w.getnframes() / w.getframerate()

        p, sr, sec = pcm("caller_affirm")
        assert spk.add_reference(p, sr, seconds=sec, reason="identity_confirmed").accepted
        p, sr, sec = pcm("hi_normal")
        same = spk.score(p, sr, seconds=sec)
        p, sr, sec = pcm("other_short")
        other = spk.score(p, sr, seconds=sec)
        assert same.attribution == ATTR_CALLER, same
        assert other.attribution in (ATTR_MISMATCH, ATTR_UNCERTAIN) and other.distance > same.distance, other
        assert same.embed_ms is not None and same.embed_ms < 500
