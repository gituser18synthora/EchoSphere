"""Self-echo evidence for the telephony caller gate.

On the FreeSWITCH/PSTN path the bot's own reply can come back on the caller
leg: line (hybrid) echo and handset acoustic echo return an attenuated,
delayed copy of what the transport just sent. The gate's existing "+5 dB
above the noise floor while the bot speaks" margin cannot see such a copy
when it sits well above the floor. This module supplies the evidence the
gate was missing — not that the inbound signal is quiet, but that it IS the
bot's own output — and a physical sanity check on that evidence.

Decision (per speechlike 20 ms inbound frame while bot audio may still be
returning), all parameters global and tenant-agnostic:

1. Correlation: normalized cross-correlation peak of the last ``window_s``
   of inbound audio against the bot audio sent within the echo delay range.
   A peak ≥ ``DEFAULT_ECHO_NCC_THRESHOLD`` (0.7) is a "strong" candidate and
   learns the call's echo-path delay; a peak ≥ ``WEAK_ECHO_NCC_THRESHOLD``
   (0.5) is a "weak" candidate only when its lag agrees with that learned
   delay within ``LAG_TOLERANCE_MS``.
2. Source plausibility: an echo cannot be louder than the audio it copies,
   and on every real route measured it is far quieter. A candidate is
   enforceable only when its inbound window is at least
   ``SOURCE_PLAUSIBILITY_MARGIN_DB`` (6 dB) BELOW the matched outgoing
   window. Genuine caller voices correlate with the bot voice at loud voiced
   onsets on real lines (2026-09-25 cross-tenant replay, 13 tenants, 114
   calls: 73 such frames, every one within 6 dB of or louder than its
   supposed source — 47 of them within ±6 dB, so a "not more than 6 dB
   louder" test alone leaves those), while every residual-echo frame sat
   ≥ 15 dB below its source. Requiring 6 dB below removes all 73 and keeps
   all 50 echo frames with ≥ 9 dB of headroom.

Modes (``mode``): ``off`` — the pipeline builds nothing, no correlation work;
``shadow`` — decisions and evidence are computed and recorded, caller audio
is never suppressed; ``enforce`` — the gate treats a frame as non-speech only
when the complete decision (correlation, lag consistency and plausibility)
passes. Platform default is ``off``; the mode and the lag search span come
from the generic telephony noise-gate configuration.

Evidence is aggregated in :attr:`EchoReference.stats` (exposed in the gate's
end-of-call stats) plus one ``echo_reference_burst`` recorder event at the
start of each would-reject burst, capped per call so production logs never
become frame-by-frame.

Timing: the telephony output transport feeds every audio frame into the
reference inside its write path, BEFORE the socket send
(``FillerWebsocketOutputTransport.attach_echo_reference``), so the reference
always holds a frame before its echo can return. :class:`EchoReferenceTap`
is the fallback for transports without that hook: it sits right after
``transport.output()`` and sees each frame once the pacing wait has passed,
which a fast echo path can beat.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np
from pipecat.frames.frames import Frame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

# Normalized cross-correlation peak at or above which the inbound window is
# a candidate on its own ("strong"). A scaled/delayed copy sits near 1.0 and
# survives a PSTN band-pass + reflections + μ-law at ≈ 0.86–0.91 (tests); a
# mixture reaches 0.7 when the echo and the caller carry equal power.
DEFAULT_ECHO_NCC_THRESHOLD = 0.7
# A weaker peak (≥ this) counts only when its lag agrees with the echo path
# already established by strong matches in this call: a real echo path has
# one stable delay, a spurious peak of the caller's own voice lands at a
# random lag. This is what catches a mixture the echo dominates by ≳ 5 dB.
WEAK_ECHO_NCC_THRESHOLD = 0.5
LAG_TOLERANCE_MS = 60.0
# Physical plausibility: the inbound window must be at least this much
# QUIETER than the outgoing window it matched to be its echo (see module
# docstring for the cross-tenant evidence behind the sign and size).
SOURCE_PLAUSIBILITY_MARGIN_DB = 6.0
# Echo delay range to search (configurable per telephony route through the
# noise-gate configuration; not widened by default — offline data showed one
# tenant near this boundary but no production symptom).
DEFAULT_MAX_LAG_S = 0.7
# Inbound analysis window: long enough for a stable correlation estimate,
# short enough to act within the gate's own confirmation time (180–220 ms).
DEFAULT_WINDOW_S = 0.12
# Per-call cap on burst evidence events; aggregates continue in ``stats``.
MAX_BURST_EVENTS = 40
# Below this the reference/inbound windows carry no signal to compare.
_MIN_RMS = 1e-4


def _dbfs(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.asarray(samples, dtype=np.float64) ** 2)))
    return -120.0 if rms <= 1e-6 else max(-120.0, 20.0 * math.log10(rms))


@dataclass(frozen=True)
class EchoMatch:
    """Correlation peak of one inbound window against the sent audio."""

    peak: float
    lag_ms: float
    inbound_dbfs: float
    source_dbfs: float

    @property
    def delta_db(self) -> float:
        """Inbound level relative to the matched outgoing window."""
        return self.inbound_dbfs - self.source_dbfs


@dataclass(frozen=True)
class EchoDecision:
    """Complete decision for one inbound frame, with its evidence."""

    ncc: float | None
    lag_ms: float | None
    tier: str | None                 # "strong" | "weak" | None
    inbound_dbfs: float | None
    source_dbfs: float | None
    delta_db: float | None
    source_plausible: bool | None    # None when there is no candidate
    would_reject: bool               # the complete decision (what ENFORCE does)
    actually_rejected: bool          # would_reject and mode == enforce
    lag_est_ms: float | None

    def as_event(self) -> dict:
        def r(v, d=1):
            return None if v is None else round(v, d)

        return {
            "ncc": r(self.ncc, 3), "lag_ms": r(self.lag_ms, 0), "tier": self.tier,
            "inbound_dbfs": r(self.inbound_dbfs), "source_dbfs": r(self.source_dbfs),
            "delta_db": r(self.delta_db), "source_plausible": self.source_plausible,
            "would_reject": self.would_reject, "actually_rejected": self.actually_rejected,
            "lag_est_ms": r(self.lag_est_ms, 0),
        }


class EchoReference:
    """Recent outgoing bot audio, time-indexed, plus the echo decision."""

    def __init__(
        self,
        *,
        sample_rate: int,
        mode: str = MODE_ENFORCE,
        history_s: float = 2.0,
        max_lag_s: float = DEFAULT_MAX_LAG_S,
        window_s: float = DEFAULT_WINDOW_S,
        threshold: float = DEFAULT_ECHO_NCC_THRESHOLD,
        weak_threshold: float = WEAK_ECHO_NCC_THRESHOLD,
        plausibility_margin_db: float = SOURCE_PLAUSIBILITY_MARGIN_DB,
        recorder=None,
        clock=None,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown echo reference mode {mode!r}")
        self.mode = mode
        self.sample_rate = int(sample_rate)
        self._clock = clock or time.monotonic
        self.max_lag_s = float(max_lag_s)
        self.window_s = float(window_s)
        self.threshold = float(threshold)
        self.weak_threshold = float(weak_threshold)
        self.plausibility_margin_db = float(plausibility_margin_db)
        # Must cover the echo search span twice over: audio can still be
        # echoing back ``max_lag + window`` after the bot's last frame, and the
        # search then reaches another ``max_lag + window`` further back.
        self._history = int(max(history_s, 2.2 * (self.max_lag_s + self.window_s)) * self.sample_rate)
        self._ref = np.zeros(0, dtype=np.float32)
        self._ref_end_time: float | None = None
        self._last_output_at: float | None = None
        self._recorder = recorder
        self._in_burst = False
        self._run_frames = 0
        self._events_emitted = 0
        # Echo-path delay learned from strong matches (EMA, ms) and its
        # spread (Welford over strong-match lags).
        self._lag_est_ms: float | None = None
        self._lag_n = 0
        self._lag_mean = 0.0
        self._lag_m2 = 0.0
        self._delta_n = 0
        self._delta_sum = 0.0
        self.stats = {
            "mode": mode,
            "frames_checked": 0,        # frames with a correlation result
            "candidates": 0,            # strong or lag-consistent weak peaks
            "strong": 0,
            "weak_rejected": 0,         # weak-tier would-reject frames
            "weak_inconsistent": 0,     # weak peaks at an unestablished/other lag
            "source_implausible": 0,    # candidates louder than their source
            "would_reject": 0,          # complete decision passed
            "frames_rejected": 0,       # actually suppressed (enforce only)
            "bursts_rejected": 0,       # would-reject bursts
            "longest_run_ms": 0.0,      # longest continuous would-reject run
            "peak_rejected_max": 0.0,   # max NCC among would-reject frames
            "peak_accepted_max": 0.0,   # max NCC among frames not would-reject
            "lag_ms_last": None,
            "lag_est_ms": None,
            "lag_std_ms": None,
            "delta_db_min": None, "delta_db_max": None, "delta_db_mean": None,
            "events_emitted": 0,
        }

    @property
    def enforcing(self) -> bool:
        return self.mode == MODE_ENFORCE

    # ── outgoing side ────────────────────────────────────────────────────
    def add_output(self, pcm: bytes, sample_rate: int, at: float | None = None) -> None:
        """Append one sent bot audio frame (the transport's real-time clock)."""
        if not pcm or len(pcm) < 2:
            return
        now = self._clock() if at is None else at
        samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float32) / 32768.0
        if sample_rate != self.sample_rate and sample_rate > 0:
            # Only the transport's own rate is expected here; a mismatch is
            # resampled crudely rather than dropped so timing stays intact.
            n = int(round(len(samples) * self.sample_rate / sample_rate))
            samples = np.interp(
                np.linspace(0, len(samples) - 1, max(n, 1)), np.arange(len(samples)), samples,
            ).astype(np.float32)
        duration = len(samples) / self.sample_rate
        if self._ref_end_time is not None:
            # Silence between frames is real time on the wire: pad it so the
            # buffer's time axis stays linear. After a gap longer than the
            # echo search span nothing older can matter — start afresh.
            gap = now - duration - self._ref_end_time
            if gap > self.max_lag_s + self.window_s:
                self._ref = np.zeros(0, dtype=np.float32)
            elif gap > 0.005:
                pad = int(gap * self.sample_rate)
                self._ref = np.concatenate([self._ref, np.zeros(pad, dtype=np.float32)])
        self._ref = np.concatenate([self._ref, samples])
        if len(self._ref) > self._history:
            self._ref = self._ref[-self._history:]
        self._ref_end_time = now
        self._last_output_at = now

    def active(self, now: float | None = None) -> bool:
        """Whether bot audio was sent recently enough to be echoing back now."""
        if self._last_output_at is None:
            return False
        now = self._clock() if now is None else now
        return now - self._last_output_at <= self.max_lag_s + self.window_s

    # ── inbound side ─────────────────────────────────────────────────────
    def match_detail(self, inbound: np.ndarray, now: float | None = None) -> EchoMatch | None:
        """Correlation peak of ``inbound`` (float32 at the reference rate, the
        gate's recent window) against the reference sent within the echo
        delay range, with the levels of both windows. None when either side
        has no signal to compare."""
        if self._ref_end_time is None or len(inbound) < 8:
            return None
        now = self._clock() if now is None else now
        sr = self.sample_rate
        raw_inb = np.asarray(inbound, dtype=np.float32)
        inb = raw_inb - float(raw_inb.mean())
        inb_rms = float(np.sqrt(np.mean(inb * inb)))
        if inb_rms < _MIN_RMS:
            return None
        # Reference region: audio sent between (now - max_lag - window) and
        # now. Nothing was sent after _ref_end_time, so the region always ends
        # at the end of the buffer and, once the bot has been quiet for
        # `elapsed`, reaches only (max_lag + window - elapsed) back from it:
        # anything older cannot be arriving as echo any more.
        elapsed = max(0.0, now - self._ref_end_time)
        span = self.max_lag_s + self.window_s - elapsed
        if span <= 0:
            return None
        end_idx = len(self._ref)
        start_idx = max(0, end_idx - int(span * sr))
        raw_ref = self._ref[start_idx:end_idx]
        if len(raw_ref) < len(inb):
            return None
        ref = raw_ref - float(raw_ref.mean())
        if float(np.sqrt(np.mean(ref * ref))) < _MIN_RMS:
            return None
        from scipy.signal import correlate

        corr = correlate(ref, inb, mode="valid", method="fft")
        # Sliding energy of the reference under each lag position.
        sq = np.concatenate([[0.0], np.cumsum(ref.astype(np.float64) ** 2)])
        n = len(inb)
        energy = sq[n:] - sq[:-n]
        denom = np.sqrt(np.maximum(energy, 1e-12)) * (inb_rms * np.sqrt(n))
        ncc = corr / denom
        pos = int(np.argmax(ncc))
        peak = float(ncc[pos])
        # Position `pos` aligns the inbound window with ref[pos:pos+n], whose
        # last sample was sent (len(ref) - pos - n)/sr before the region's
        # end, i.e. that much plus `elapsed` before now.
        lag_ms = (elapsed + (len(ref) - pos - n) / sr) * 1000.0
        return EchoMatch(peak, lag_ms, _dbfs(raw_inb), _dbfs(raw_ref[pos:pos + n]))

    def match(self, inbound: np.ndarray, now: float | None = None) -> tuple[float, float] | None:
        """``(peak, lag_ms)`` of :meth:`match_detail`, or None."""
        m = self.match_detail(inbound, now)
        return None if m is None else (m.peak, m.lag_ms)

    def decide(self, inbound: np.ndarray, level_dbfs: float, now: float | None = None) -> EchoDecision:
        """Complete decision for the inbound window ending now (records evidence)."""
        m = self.match_detail(inbound, now)
        if m is None:
            self._end_burst()
            return EchoDecision(None, None, None, None, None, None, None, False, False, self._lag_est_ms)
        self.stats["frames_checked"] += 1
        tier = None
        if m.peak >= self.threshold:
            tier = "strong"
            self.stats["strong"] += 1
            if self._lag_est_ms is None or abs(m.lag_ms - self._lag_est_ms) > LAG_TOLERANCE_MS:
                self._lag_est_ms = m.lag_ms
            else:
                self._lag_est_ms = 0.7 * self._lag_est_ms + 0.3 * m.lag_ms
            self.stats["lag_est_ms"] = round(self._lag_est_ms)
            self._lag_n += 1
            d = m.lag_ms - self._lag_mean
            self._lag_mean += d / self._lag_n
            self._lag_m2 += d * (m.lag_ms - self._lag_mean)
            if self._lag_n >= 2:
                self.stats["lag_std_ms"] = round(math.sqrt(self._lag_m2 / (self._lag_n - 1)))
        elif m.peak >= self.weak_threshold:
            if self._lag_est_ms is not None and abs(m.lag_ms - self._lag_est_ms) <= LAG_TOLERANCE_MS:
                tier = "weak"
            else:
                self.stats["weak_inconsistent"] += 1
        plausible = None
        would_reject = False
        if tier is not None:
            self.stats["candidates"] += 1
            delta = m.delta_db
            self._delta_n += 1
            self._delta_sum += delta
            rd = round(delta, 1)
            self.stats["delta_db_min"] = rd if self.stats["delta_db_min"] is None else min(self.stats["delta_db_min"], rd)
            self.stats["delta_db_max"] = rd if self.stats["delta_db_max"] is None else max(self.stats["delta_db_max"], rd)
            self.stats["delta_db_mean"] = round(self._delta_sum / self._delta_n, 1)
            plausible = delta <= -self.plausibility_margin_db
            if plausible:
                would_reject = True
                if tier == "weak":
                    self.stats["weak_rejected"] += 1
            else:
                self.stats["source_implausible"] += 1
        actually = would_reject and self.enforcing
        decision = EchoDecision(
            m.peak, m.lag_ms, tier, m.inbound_dbfs, m.source_dbfs, m.delta_db,
            plausible, would_reject, actually, self._lag_est_ms,
        )
        if would_reject:
            self.stats["would_reject"] += 1
            if actually:
                self.stats["frames_rejected"] += 1
            self.stats["peak_rejected_max"] = max(self.stats["peak_rejected_max"], round(m.peak, 3))
            self.stats["lag_ms_last"] = round(m.lag_ms)
            self._run_frames += 1
            self.stats["longest_run_ms"] = max(self.stats["longest_run_ms"], self._run_frames * self.window_frame_ms())
            if not self._in_burst:
                self._in_burst = True
                self.stats["bursts_rejected"] += 1
                if self._events_emitted < MAX_BURST_EVENTS:
                    self._events_emitted += 1
                    self.stats["events_emitted"] = self._events_emitted
                    self._event("echo_reference_burst", mode=self.mode, level_dbfs=round(level_dbfs, 1), **decision.as_event())
        else:
            self.stats["peak_accepted_max"] = max(self.stats["peak_accepted_max"], round(m.peak, 3))
            self._end_burst()
        return decision

    def judge(self, inbound: np.ndarray, level_dbfs: float, now: float | None = None) -> bool:
        """Whether the gate must treat this frame as the bot's own echo
        (the complete decision, honoured only in enforce mode)."""
        return self.decide(inbound, level_dbfs, now).actually_rejected

    def window_frame_ms(self) -> float:
        """Nominal per-decision frame duration used for run-length bookkeeping."""
        return 20.0

    def _end_burst(self) -> None:
        self._in_burst = False
        self._run_frames = 0

    def _event(self, kind: str, **data) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.add_event(kind, **data)
        except Exception:  # noqa: BLE001 — evidence must never break audio
            logger.debug("echo reference event failed", exc_info=True)


class EchoReferenceTap(FrameProcessor):
    """Copies every sent bot audio frame into the :class:`EchoReference`.

    Fallback for transports without ``attach_echo_reference``: placed
    immediately after ``transport.output()``, so it sees each frame only once
    the transport's pacing wait has passed (see module docstring).
    """

    def __init__(self, reference: EchoReference, **kwargs) -> None:
        super().__init__(**kwargs)
        self._reference = reference

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame) and frame.audio:
            try:
                self._reference.add_output(frame.audio, frame.sample_rate)
            except Exception:  # noqa: BLE001 — evidence must never break audio
                logger.debug("echo reference tap failed", exc_info=True)
        await self.push_frame(frame, direction)


def mode_from_setting(value) -> str:
    """Map the generic numeric noise-gate setting (0 off, 1 shadow, 2 enforce)
    or a mode name to a mode; anything unknown is ``off``."""
    if isinstance(value, str):
        v = value.strip().lower()
        return v if v in MODES else MODE_OFF
    try:
        code = int(round(float(value)))
    except (TypeError, ValueError):
        return MODE_OFF
    return {0: MODE_OFF, 1: MODE_SHADOW, 2: MODE_ENFORCE}.get(code, MODE_OFF)


__all__ = [
    "EchoReference", "EchoReferenceTap", "EchoDecision", "EchoMatch", "mode_from_setting",
    "MODE_OFF", "MODE_SHADOW", "MODE_ENFORCE", "MODES",
    "DEFAULT_ECHO_NCC_THRESHOLD", "WEAK_ECHO_NCC_THRESHOLD", "LAG_TOLERANCE_MS",
    "SOURCE_PLAUSIBILITY_MARGIN_DB", "DEFAULT_MAX_LAG_S", "DEFAULT_WINDOW_S",
]
