"""Caller speaker-consistency evidence (prototype, shadow-first).

Answers one question per accepted caller segment: "does this speech sound
like the same person who has already been interacting with the bot?" It is
NOT identity authentication and it never decides anything on its own.

Why: level/VAD cannot separate a second person from the caller at similar
loudness (2026-09-25 investigation: TV, a nearby male or female speaker and
background conversation at −12…0 dB were accepted as the caller in every
controlled call, and their "hang up / wrong number / call later / don't
call" phrases executed). Live shadow evidence puts the rate at roughly 3 %
of sessions for a quiet second voice affirming or advancing something and
0.5 % for a hard destructive route. What does separate speakers is a voice
embedding: with a single 1.7 s vouched caller segment as reference, a
GE2E-class embedding kept 100 % of caller segments and rejected 93 % of
second-speaker/TV segments in the controlled set.

Design (all generic, no tenant/bot/language/workflow names):

- :class:`EmbeddingBackend` isolates the model. :class:`OnnxGE2EBackend`
  runs the exported resemblyzer GE2E encoder (3-layer LSTM, 40-mel input)
  with onnxruntime and a numpy mel front-end — no torch at runtime. Any
  backend returning a unit-norm vector per utterance can replace it.
- :class:`SpeakerConsistency` builds the caller reference ONLY from turns
  the call vouched for (identity confirmed, identifier validated, workflow
  advanced on the turn — the same hooks that seed the caller-level
  baseline), never from arbitrary accepted speech, never from audio captured
  while the bot was speaking, and — once a reference exists — never from a
  segment that itself sounds like a different voice (contamination guard).
  Later vouched segments are combined as the normalized mean embedding;
  the nearest-segment distance is recorded alongside for evaluation.
- :meth:`SpeakerConsistency.score` returns structured evidence
  (:class:`SpeakerEvidence`): reference availability and duration, cosine
  distance to the reference, attribution ``caller`` / ``uncertain`` /
  ``mismatch`` / ``unknown`` and thresholds, plus the conversational context
  the caller passes in (question open, overlap with bot audio, …).
- Modes: ``off`` (nothing built), ``shadow`` (score, record, never act),
  ``enforce`` (reserved: the evidence carries ``enforceable``; no consumer
  acts on it in this prototype). Platform default is ``off``.
- :class:`CallerAudioTap` sits right after the caller audio gate and keeps a
  bounded ring of the audio that PASSED the gate, so the brain can hand the
  scorer the exact audio of an accepted segment without touching the gate.

Thresholds come from the controlled evaluation (GE2E cosine distance to a
1.7 s reference: caller p90 0.31, second speaker p10 0.42, overlapping
caller+background around 0.42) and are global.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

# Cosine distance (1 − cosine similarity) of a segment to the caller reference.
CALLER_MAX_DISTANCE = 0.35      # at or below: the same voice
MISMATCH_MIN_DISTANCE = 0.45    # at or above: a different voice
# A segment shorter than this carries too little voice to score; a vouched
# segment shorter than the second value is not used as reference.
MIN_SCORE_SECONDS = 0.6
MIN_REFERENCE_SECONDS = 0.8
# Reference memory: newest vouched segments, bounded.
MAX_REFERENCE_SEGMENTS = 4
MAX_REFERENCE_SECONDS = 8.0
# A vouched segment that sounds like a DIFFERENT voice than the existing
# reference is not merged into it (contamination guard).
REFERENCE_CONTAMINATION_DISTANCE = MISMATCH_MIN_DISTANCE

ATTR_UNKNOWN = "unknown"
ATTR_CALLER = "caller"
ATTR_UNCERTAIN = "uncertain"
ATTR_MISMATCH = "mismatch"

MODEL_SAMPLE_RATE = 16000


def mode_from_setting(value) -> str:
    """Generic numeric setting (0 off, 1 shadow, 2 enforce) or name → mode."""
    if isinstance(value, str):
        v = value.strip().lower()
        return v if v in MODES else MODE_OFF
    try:
        code = int(round(float(value)))
    except (TypeError, ValueError):
        return MODE_OFF
    return {0: MODE_OFF, 1: MODE_SHADOW, 2: MODE_ENFORCE}.get(code, MODE_OFF)


# ── audio helpers ────────────────────────────────────────────────────────
def pcm16_to_float(pcm: bytes) -> np.ndarray:
    pcm = pcm[: len(pcm) - (len(pcm) % 2)]
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def resample_to(x: np.ndarray, sample_rate: int, target: int) -> np.ndarray:
    if sample_rate == target or len(x) == 0:
        return x.astype(np.float32, copy=False)
    from scipy.signal import resample_poly

    g = math.gcd(int(sample_rate), int(target))
    return resample_poly(x.astype(np.float64), target // g, sample_rate // g).astype(np.float32)


# ── embedding backends ───────────────────────────────────────────────────
class EmbeddingBackend(Protocol):
    """One utterance in (float32 at :data:`MODEL_SAMPLE_RATE`), one unit-norm
    vector out. ``None`` when the utterance is unusable."""

    name: str

    def embed(self, wav16: np.ndarray) -> np.ndarray | None: ...


class OnnxGE2EBackend:
    """resemblyzer's GE2E VoiceEncoder exported to ONNX (see
    ``scratchpad/export_ge2e.py``): 40-band mel power spectrogram (25 ms
    window, 10 ms hop, librosa slaney mel filterbank) → LSTM → linear → ReLU
    → L2 normalize; utterances longer than one partial (1.6 s) are embedded
    as overlapping partials whose embeddings are averaged, exactly as the
    reference implementation does. Numerically identical to resemblyzer
    (cosine 1.00000 on the emulator clips)."""

    name = "ge2e-onnx"
    WIN = int(MODEL_SAMPLE_RATE * 25 / 1000)   # 400
    HOP = int(MODEL_SAMPLE_RATE * 10 / 1000)   # 160
    PARTIAL_FRAMES = 160
    PARTIAL_RATE = 1.3
    MIN_COVERAGE = 0.75

    def __init__(self, model_path: str, melfb_path: str | None = None, threads: int = 1) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name
        melfb_path = melfb_path or model_path.replace(".onnx", "_melfb.npy")
        self._melfb = np.load(melfb_path).astype(np.float32)          # (40, n_fft/2+1)
        self._window = np.hanning(self.WIN + 1)[:-1].astype(np.float32)
        self.model_path = model_path
        self.model_bytes = os.path.getsize(model_path)

    @classmethod
    def load(cls, model_path: str) -> "OnnxGE2EBackend | None":
        try:
            if not os.path.isfile(model_path):
                logger.warning("speaker consistency: model file %s missing; evidence disabled", model_path)
                return None
            return cls(model_path)
        except Exception:  # noqa: BLE001 — evidence must never break a call
            logger.warning("speaker consistency: backend failed to load", exc_info=True)
            return None

    def _mel(self, wav: np.ndarray) -> np.ndarray:
        pad = self.WIN // 2
        x = np.pad(wav, pad, mode="reflect") if len(wav) > pad else np.pad(wav, pad, mode="constant")
        n = 1 + (len(x) - self.WIN) // self.HOP
        if n <= 0:
            return np.zeros((0, self._melfb.shape[0]), dtype=np.float32)
        frames = np.lib.stride_tricks.as_strided(
            x, shape=(n, self.WIN), strides=(x.strides[0] * self.HOP, x.strides[0]),
        )
        spec = np.abs(np.fft.rfft(frames * self._window, self.WIN, axis=1)) ** 2
        return (spec @ self._melfb.T).astype(np.float32)

    def _slices(self, n_samples: int):
        spf = self.HOP
        n_frames = int(math.ceil((n_samples + 1) / spf))
        step = int(round((MODEL_SAMPLE_RATE / self.PARTIAL_RATE) / spf))
        steps = max(1, n_frames - self.PARTIAL_FRAMES + step + 1)
        mel_slices, wav_end = [], 0
        for i in range(0, steps, step):
            mel_slices.append(slice(i, i + self.PARTIAL_FRAMES))
            wav_end = (i + self.PARTIAL_FRAMES) * spf
        last_start = mel_slices[-1].start * spf
        coverage = (n_samples - last_start) / (wav_end - last_start) if wav_end > last_start else 1.0
        if coverage < self.MIN_COVERAGE and len(mel_slices) > 1:
            mel_slices = mel_slices[:-1]
            wav_end = mel_slices[-1].stop * spf
        return mel_slices, wav_end

    def embed(self, wav16: np.ndarray) -> np.ndarray | None:
        wav = np.asarray(wav16, dtype=np.float32)
        if len(wav) < self.HOP * 20:  # < 200 ms: nothing to embed
            return None
        mel_slices, wav_end = self._slices(len(wav))
        if wav_end > len(wav):
            wav = np.pad(wav, (0, wav_end - len(wav)), mode="constant")
        mels = self._mel(wav)
        parts = np.stack([mels[s] for s in mel_slices]).astype(np.float32)
        embs = self._sess.run(None, {self._input: parts})[0]
        e = embs.mean(axis=0)
        n = float(np.linalg.norm(e))
        return None if n <= 1e-6 else (e / n).astype(np.float32)


# ── evidence ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpeakerEvidence:
    """Structured result of scoring one accepted segment."""

    attribution: str                       # unknown | caller | uncertain | mismatch
    reference_available: bool
    reference_seconds: float
    reference_segments: int
    segment_seconds: float
    distance: float | None                 # to the mean reference
    distance_nearest: float | None         # to the nearest reference segment
    caller_max: float
    mismatch_min: float
    embed_ms: float | None
    backend: str | None
    mode: str
    enforceable: bool                      # mismatch AND mode == enforce (no consumer acts on it yet)
    reason: str                            # why unknown, else "scored"
    context: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        r = lambda v, d=3: None if v is None else round(v, d)  # noqa: E731
        return {
            "attribution": self.attribution, "reference_available": self.reference_available,
            "reference_seconds": round(self.reference_seconds, 2), "reference_segments": self.reference_segments,
            "segment_seconds": round(self.segment_seconds, 2), "distance": r(self.distance),
            "distance_nearest": r(self.distance_nearest), "caller_max": self.caller_max, "mismatch_min": self.mismatch_min,
            "embed_ms": r(self.embed_ms, 1), "backend": self.backend, "mode": self.mode,
            "enforceable": self.enforceable, "reason": self.reason, **self.context,
        }


@dataclass(frozen=True)
class ReferenceUpdate:
    accepted: bool
    reason: str
    reference_seconds: float
    reference_segments: int
    distance_to_existing: float | None
    embed_ms: float | None


class SpeakerConsistency:
    """Per-call caller reference and scorer (see module docstring)."""

    def __init__(
        self, *, backend: EmbeddingBackend | None, mode: str = MODE_SHADOW, recorder=None, clock=None,
        caller_max: float = CALLER_MAX_DISTANCE, mismatch_min: float = MISMATCH_MIN_DISTANCE,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown speaker consistency mode {mode!r}")
        self.mode = mode
        self._backend = backend
        self._recorder = recorder
        self._clock = clock or time.perf_counter
        self.caller_max = float(caller_max)
        self.mismatch_min = float(mismatch_min)
        self._refs: list[tuple[np.ndarray, float]] = []   # (unit embedding, seconds)
        self._mean: np.ndarray | None = None
        self.stats = {
            "mode": mode, "backend": getattr(backend, "name", None) if backend is not None else None,
            "reference_segments": 0, "reference_seconds": 0.0, "reference_rejected": 0,
            "scored": 0, "unknown": 0, "caller": 0, "uncertain": 0, "mismatch": 0,
            "embed_ms_max": 0.0, "embed_ms_total": 0.0,
        }

    # ── reference ───────────────────────────────────────────────────────
    @property
    def reference_available(self) -> bool:
        return self._mean is not None

    @property
    def reference_seconds(self) -> float:
        return float(sum(s for _, s in self._refs))

    @property
    def reference_segments(self) -> int:
        return len(self._refs)

    def _embed(self, pcm: bytes, sample_rate: int) -> tuple[np.ndarray | None, float]:
        if self._backend is None:
            return None, 0.0
        t0 = self._clock()
        try:
            wav = resample_to(pcm16_to_float(pcm), sample_rate, MODEL_SAMPLE_RATE)
            emb = self._backend.embed(wav)
        except Exception:  # noqa: BLE001 — evidence must never break a call
            logger.debug("speaker consistency: embedding failed", exc_info=True)
            emb = None
        ms = (self._clock() - t0) * 1000.0
        self.stats["embed_ms_max"] = max(self.stats["embed_ms_max"], round(ms, 1))
        self.stats["embed_ms_total"] = round(self.stats["embed_ms_total"] + ms, 1)
        return emb, ms

    def _distances(self, emb: np.ndarray) -> tuple[float | None, float | None]:
        if self._mean is None:
            return None, None
        d_mean = 1.0 - float(np.dot(self._mean, emb))
        d_near = min(1.0 - float(np.dot(r, emb)) for r, _ in self._refs)
        return d_mean, d_near

    def add_reference(self, pcm: bytes, sample_rate: int, *, seconds: float, reason: str,
                      during_bot_audio: bool = False) -> ReferenceUpdate:
        """Add one VOUCHED caller segment to the reference. The caller of
        this method is responsible for the vouching; this method only
        applies the audio-quality and contamination rules."""
        def rejected(why: str, dist=None, ms=None) -> ReferenceUpdate:
            self.stats["reference_rejected"] += 1
            upd = ReferenceUpdate(False, why, self.reference_seconds, self.reference_segments, dist, ms)
            self._event("speaker_reference", reason=reason, accepted=False, why=why,
                        distance_to_existing=None if dist is None else round(dist, 3),
                        reference_seconds=round(self.reference_seconds, 2), reference_segments=self.reference_segments)
            return upd
        if during_bot_audio:
            return rejected("during_bot_audio")
        if seconds < MIN_REFERENCE_SECONDS:
            return rejected("too_short")
        emb, ms = self._embed(pcm, sample_rate)
        if emb is None:
            return rejected("no_embedding" if self._backend is not None else "no_backend", None, ms)
        d_mean, _ = self._distances(emb)
        if d_mean is not None and d_mean >= REFERENCE_CONTAMINATION_DISTANCE:
            return rejected("contamination_guard", d_mean, ms)
        self._refs.append((emb, float(seconds)))
        while len(self._refs) > MAX_REFERENCE_SEGMENTS or (
            len(self._refs) > 1 and self.reference_seconds > MAX_REFERENCE_SECONDS
        ):
            self._refs.pop(0)
        mean = np.sum([r for r, _ in self._refs], axis=0)
        self._mean = (mean / (np.linalg.norm(mean) + 1e-9)).astype(np.float32)
        self.stats["reference_segments"] = self.reference_segments
        self.stats["reference_seconds"] = round(self.reference_seconds, 2)
        self._event("speaker_reference", reason=reason, accepted=True, seconds=round(seconds, 2),
                    distance_to_existing=None if d_mean is None else round(d_mean, 3), embed_ms=round(ms, 1),
                    reference_seconds=round(self.reference_seconds, 2), reference_segments=self.reference_segments)
        return ReferenceUpdate(True, "added", self.reference_seconds, self.reference_segments, d_mean, ms)

    # ── scoring ─────────────────────────────────────────────────────────
    def score(self, pcm: bytes, sample_rate: int, *, seconds: float, context: dict | None = None) -> SpeakerEvidence:
        """Attribute one accepted segment (audio that passed the gate)."""
        context = dict(context or {})
        base = dict(
            reference_available=self.reference_available, reference_seconds=self.reference_seconds,
            reference_segments=self.reference_segments, segment_seconds=float(seconds), caller_max=self.caller_max,
            mismatch_min=self.mismatch_min, backend=getattr(self._backend, "name", None) if self._backend else None,
            mode=self.mode, context=context,
        )
        def unknown(why: str, ms=None) -> SpeakerEvidence:
            self.stats["scored"] += 1
            self.stats["unknown"] += 1
            ev = SpeakerEvidence(ATTR_UNKNOWN, distance=None, distance_nearest=None, embed_ms=ms, enforceable=False, reason=why, **base)
            self._event("speaker_consistency", **ev.as_event())
            return ev
        if self._backend is None:
            return unknown("no_backend")
        if not self.reference_available:
            return unknown("no_reference")
        if seconds < MIN_SCORE_SECONDS:
            return unknown("too_short")
        emb, ms = self._embed(pcm, sample_rate)
        if emb is None:
            return unknown("no_embedding", ms)
        d_mean, d_near = self._distances(emb)
        if d_mean <= self.caller_max:
            attribution = ATTR_CALLER
        elif d_mean >= self.mismatch_min:
            attribution = ATTR_MISMATCH
        else:
            attribution = ATTR_UNCERTAIN
        self.stats["scored"] += 1
        self.stats[attribution] += 1
        ev = SpeakerEvidence(
            attribution, distance=d_mean, distance_nearest=d_near, embed_ms=ms,
            enforceable=(attribution == ATTR_MISMATCH and self.mode == MODE_ENFORCE), reason="scored", **base,
        )
        self._event("speaker_consistency", **ev.as_event())
        return ev

    def _event(self, kind: str, **data) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.add_event(kind, **data)
        except Exception:  # noqa: BLE001
            logger.debug("speaker consistency event failed", exc_info=True)


# ── passed-audio tap ─────────────────────────────────────────────────────
class CallerAudioTap(FrameProcessor):
    """Bounded ring of the caller audio that PASSED the gate (the gate
    substitutes digital silence for suppressed audio, so non-silent frames
    are exactly the audio the STT heard). Placed right after the gate; pure
    observer. ``take_recent`` hands back the most recent contiguous run."""

    def __init__(self, *, max_seconds: float = 20.0, gap_seconds: float = 0.6, clock=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._max = float(max_seconds)
        self._gap = float(gap_seconds)
        # Not ``_clock``: pipecat's FrameProcessor owns that attribute and
        # replaces it with its SystemClock object when the pipeline starts.
        self._tap_clock = clock or time.monotonic
        self._frames: list[tuple[float, bytes, int]] = []   # (arrival time, pcm, sample_rate)
        self._seconds = 0.0

    def _remember(self, frame: InputAudioRawFrame) -> None:
        if not frame.audio or not any(frame.audio):
            return
        sr = frame.sample_rate or 8000
        dur = len(frame.audio) / 2 / max(1, frame.num_channels or 1) / sr
        self._frames.append((self._tap_clock(), frame.audio, sr))
        self._seconds += dur
        while self._frames and self._seconds > self._max:
            _, old, osr = self._frames.pop(0)
            self._seconds -= len(old) / 2 / osr

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            try:
                self._remember(frame)
            except Exception:  # noqa: BLE001 — evidence must never touch the audio path
                logger.debug("caller audio tap bookkeeping failed", exc_info=True)
        await self.push_frame(frame, direction)

    def take_recent(self, max_seconds: float) -> tuple[bytes, int, float] | None:
        """The most recent contiguous run of passed audio (frames closer than
        ``gap_seconds``), at most ``max_seconds`` long: ``(pcm, rate, seconds)``."""
        if not self._frames:
            return None
        run: list[bytes] = []
        seconds = 0.0
        rate = self._frames[-1][2]
        last_t = None
        for t, pcm, sr in reversed(self._frames):
            if sr != rate:
                break
            if last_t is not None and last_t - t > self._gap:
                break
            dur = len(pcm) / 2 / sr
            if seconds + dur > max_seconds and run:
                break
            run.append(pcm)
            seconds += dur
            last_t = t
        if not run:
            return None
        run.reverse()
        return b"".join(run), rate, seconds


__all__ = [
    "SpeakerConsistency", "SpeakerEvidence", "ReferenceUpdate", "CallerAudioTap",
    "EmbeddingBackend", "OnnxGE2EBackend", "mode_from_setting",
    "MODE_OFF", "MODE_SHADOW", "MODE_ENFORCE", "MODES",
    "ATTR_UNKNOWN", "ATTR_CALLER", "ATTR_UNCERTAIN", "ATTR_MISMATCH",
    "CALLER_MAX_DISTANCE", "MISMATCH_MIN_DISTANCE", "MIN_SCORE_SECONDS", "MIN_REFERENCE_SECONDS",
]
