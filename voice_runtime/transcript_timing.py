"""Playback-only speech anchors. Never changes turn detection or transcript clocks."""
from __future__ import annotations

import math
import time


class TranscriptSpeechTiming:
    def __init__(self) -> None:
        self.episode_start: float | None = None
        self.pending_start: float | None = None
        self.episode_active = False

    def speech_started(self, timestamp: float | None = None, start_secs: float = 0.0) -> None:
        # The VAD frame carries when detection happened, before pipeline queue
        # delay. start_secs is the speech already heard before confirmation.
        timestamp = timestamp if isinstance(timestamp, (int, float)) and math.isfinite(timestamp) and timestamp > 0 else time.time()
        delay = start_secs if isinstance(start_secs, (int, float)) and math.isfinite(start_secs) else 0.0
        self.episode_start = timestamp - max(0.0, min(5.0, delay))
        self.episode_active = True

    def speech_stopped(self) -> None:
        self.episode_active = False

    def include(self, start: float | None) -> None:
        if start is not None:
            self.pending_start = start if self.pending_start is None else min(self.pending_start, start)

    def accepted_segment(self, *, new_buffer: bool) -> None:
        if new_buffer:
            self.pending_start = None
        self.include(self.episode_start)

    def take_start(self) -> float | None:
        start, self.pending_start = self.pending_start, None
        # An orphan final must not inherit an earlier turn's VAD episode.
        # A still-active episode can legitimately supply a late segment.
        if not self.episode_active:
            self.episode_start = None
        return start
