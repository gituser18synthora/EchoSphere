"""Silero confidence evidence for turn-detection tuning.

The Silero analyzer decides speech/no-speech inside pipecat's executor thread
and throws the probability away; a call's event stream only ever showed the
resulting start/stop frames. Tuning ``confidence`` / ``stop_secs`` for the
narrowband telephony leg therefore had no measurement to lean on (the
2026-09-17 audit could only simulate a 300–3400 Hz band-limit offline).

:class:`ConfidenceTrackingSileroVADAnalyzer` keeps a bounded, timestamped
history of every window's probability; :class:`VADConfidenceProbe` sits right
after the VAD, and on each ``VADUserStoppedSpeakingFrame`` records a
``vad_segment`` event with the segment's confidence profile plus how many
sub-threshold "near miss" windows preceded it. Numbers only — no audio, no
text — and the probe never withholds or alters a frame.
"""

from __future__ import annotations

import time
from collections import deque

import numpy as np
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# ~4000 windows × 32 ms ≈ 2 minutes of history; segments are summarised as
# they close, so a bounded ring is plenty.
_HISTORY_WINDOWS = 4000
# A window this close below the threshold is speech the VAD *nearly* took.
_NEAR_MISS_BAND = 0.2


class ConfidenceTrackingSileroVADAnalyzer(SileroVADAnalyzer):
    """Silero analyzer that remembers each window's speech probability."""

    def __init__(self, *, sample_rate: int | None = None, params=None) -> None:
        super().__init__(sample_rate=sample_rate, params=params)
        self._history: deque[tuple[float, float]] = deque(maxlen=_HISTORY_WINDOWS)

    def voice_confidence(self, buffer) -> float:
        confidence = super().voice_confidence(buffer)
        try:
            value = float(np.asarray(confidence).ravel()[0])
        except Exception:  # noqa: BLE001 — evidence must never break the VAD
            value = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        # deque.append is atomic under the GIL; the probe reads on the loop
        # thread while this runs in the analyzer's executor thread.
        self._history.append((time.monotonic(), value))
        return confidence

    def confidences_between(self, start: float, end: float) -> list[float]:
        return [c for t, c in list(self._history) if start <= t <= end]


def summarize_confidences(values: list[float], threshold: float) -> dict:
    """Numbers the tuning replay compares: profile of one speech segment."""
    if not values:
        return {"windows": 0}
    arr = np.asarray(values, dtype=np.float32)
    return {
        "windows": int(arr.size),
        "conf_mean": round(float(arr.mean()), 3),
        "conf_p25": round(float(np.percentile(arr, 25)), 3),
        "conf_max": round(float(arr.max()), 3),
        "above_threshold_ratio": round(float((arr >= threshold).mean()), 3),
    }


class VADConfidenceProbe(FrameProcessor):
    """Pass-through probe recording a ``vad_segment`` event per VAD stop."""

    def __init__(self, analyzer: ConfidenceTrackingSileroVADAnalyzer, recorder, **kwargs) -> None:
        super().__init__(**kwargs)
        self._analyzer = analyzer
        self._recorder = recorder
        self._segment_started_at: float | None = None
        self._last_stop_at: float | None = None
        self._segments = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            # The VAD confirms a start only after ``start_secs`` of speech:
            # the segment began that much earlier.
            start_secs = float(getattr(frame, "start_secs", 0.0) or 0.0)
            self._segment_started_at = time.monotonic() - start_secs
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._record_segment(frame)
        await self.push_frame(frame, direction)

    def _record_segment(self, frame) -> None:
        now = time.monotonic()
        started = self._segment_started_at
        self._segment_started_at = None
        if started is None or self._recorder is None:
            self._last_stop_at = now
            return
        threshold = float(self._analyzer.params.confidence)
        stop_secs = float(getattr(frame, "stop_secs", 0.0) or 0.0)
        # The stop is confirmed ``stop_secs`` after speech actually ended.
        speech = self._analyzer.confidences_between(started, now - stop_secs)
        gap_start = self._last_stop_at if self._last_stop_at is not None else started - 10.0
        before = self._analyzer.confidences_between(gap_start, started)
        near_miss = sum(1 for c in before if threshold - _NEAR_MISS_BAND <= c < threshold)
        self._segments += 1
        self._last_stop_at = now
        try:
            self._recorder.add_event(
                "vad_segment",
                index=self._segments,
                duration_s=round(max(0.0, now - stop_secs - started), 2),
                threshold=threshold,
                near_miss_windows_before=near_miss,
                **summarize_confidences(speech, threshold),
            )
        except Exception:  # noqa: BLE001 — diagnostics never break a call
            pass
