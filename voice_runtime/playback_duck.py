"""Provisional pause of bot playback for hybrid barge-in.

The word-confirmed barge-in policy (voice_runtime.barge_in) cannot tell a
genuine interruption from a blip until either the caller has kept talking
for the commit window or a multi-word transcript arrives. Until then the bot
either keeps talking over the caller (slow to yield) or is cancelled on
speculation (blips and backchannels chop the reply; 2026-09-18 benchmark:
13 of 16 real one/two-word backchannels and 5 of 10 no-transcript blips
cancelled the reply under the current policy). This processor gives the
strategy a third option: PAUSE the reply the moment provisional speech is
detected, then either COMMIT (interruption, the held audio is discarded) or
RESUME (the held audio continues exactly where it stopped — nothing already
synthesised is thrown away).

Placement: after the TTS service and the latency filler, immediately before
``transport.output()``, so every frame that would reach the wire passes here.

Mechanics:

- While paused, downstream DATA frames (reply audio, TTS start/stop, reply
  markers, filler audio) are held in arrival order. System frames are never
  held. The output transport, receiving no audio, pads the wire with its
  own silence, so the caller hears the bot go quiet within the transport's
  pacing tick plus whatever the far end had already buffered (≈40 ms tick +
  ≤200 ms fork packet on telephony).
- The transport declares ``BotStoppedSpeaking`` after 0.35 s without audio.
  During a pause that is a false signal (the reply is not over), so it is
  swallowed here, and the ``BotStartedSpeaking`` the transport emits when
  playback resumes is swallowed too: upstream (brain, barge-in strategy,
  audio gate, STT) sees one continuous bot utterance. If the pause instead
  ends in an interruption, the swallowed stop is re-emitted upstream so the
  brain's turn bookkeeping closes the reply exactly as before.
- ``InterruptionFrame``, ``EndFrame`` and ``CancelFrame`` discard everything
  held: stale audio from an old turn can never replay into a new one. The
  held buffer is also capped (``max_hold_seconds`` of audio; oldest dropped).

Feature-controlled: the pipeline only inserts this processor when the
tenant's ``barge_in_duck_enabled`` is on. Every transition is recorded on
the call's event stream (``playback_paused`` / ``playback_resumed`` /
``playback_discarded``).
"""

from __future__ import annotations

import logging
import time
from collections import deque

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    SystemFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


class PlaybackDuck(FrameProcessor):
    """Hold-and-release gate for bot output audio (see module docstring).

    Args:
        recorder: Session recorder for evidence events (optional).
        max_hold_seconds: Cap on held reply audio; beyond it the oldest held
            audio is dropped (a pause that long has already lost its context).
    """

    def __init__(self, *, recorder=None, max_hold_seconds: float = 30.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._recorder = recorder
        self._max_hold_seconds = max(1.0, float(max_hold_seconds))
        self._paused = False
        self._flushing = False
        self._held: deque[tuple[Frame, FrameDirection]] = deque()
        self._held_audio_bytes = 0
        self._sample_rate = 0
        self._paused_at: float | None = None
        # Transport bot-speaking illusion while paused (see module docstring).
        self._suppressed_stop = False
        self._suppress_next_start = False
        self._stats = {"pauses": 0, "resumes": 0, "discards": 0, "cap_drops": 0}

    # ── public state ─────────────────────────────────────────────────────
    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def held_ms(self) -> float:
        if not self._sample_rate:
            return 0.0
        return self._held_audio_bytes / 2 / self._sample_rate * 1000.0

    def stats(self) -> dict:
        return dict(self._stats)

    def _event(self, kind: str, **data) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.add_event(kind, **data)
        except Exception:  # noqa: BLE001 — evidence must never break audio
            logger.debug("playback duck event failed", exc_info=True)

    # ── control (called by the barge-in strategy through the pipeline) ───
    async def set_provisional(self, active: bool) -> None:
        """Enter (``True``) or leave (``False``) the provisional pause."""
        if active:
            await self.pause()
        else:
            await self.resume()

    async def pause(self) -> None:
        if self._paused:
            return
        self._paused = True
        self._paused_at = time.monotonic()
        self._stats["pauses"] += 1
        self._event("playback_paused")

    async def resume(self) -> None:
        """Release held frames in order; playback continues where it stopped."""
        if not self._paused:
            return
        self._paused = False
        paused_ms = (
            (time.monotonic() - self._paused_at) * 1000.0 if self._paused_at else 0.0
        )
        self._paused_at = None
        self._stats["resumes"] += 1
        held_ms = self.held_ms
        if self._suppressed_stop:
            # The transport thinks the bot stopped; it will announce a new
            # start when the held audio reaches it. Upstream must not see it.
            self._suppress_next_start = True
            self._suppressed_stop = False
        self._event(
            "playback_resumed", paused_ms=round(paused_ms), held_ms=round(held_ms),
        )
        await self._flush_held()

    async def _flush_held(self) -> None:
        self._flushing = True
        try:
            while self._held:
                frame, direction = self._held.popleft()
                if isinstance(frame, OutputAudioRawFrame):
                    self._held_audio_bytes = max(0, self._held_audio_bytes - len(frame.audio))
                await self.push_frame(frame, direction)
        finally:
            self._flushing = False
            self._held_audio_bytes = 0

    def _discard_held(self, reason: str) -> None:
        if not self._held and not self._paused:
            return
        held_ms = self.held_ms
        self._held.clear()
        self._held_audio_bytes = 0
        if self._paused:
            self._stats["discards"] += 1
            self._event("playback_discarded", reason=reason, held_ms=round(held_ms))
        self._paused = False
        self._paused_at = None

    async def _synthesize_stop(self) -> None:
        """Re-emit the transport's swallowed BotStopped upstream."""
        if not self._suppressed_stop:
            return
        self._suppressed_stop = False
        self._suppress_next_start = False
        await self.push_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    # ── frame handling ───────────────────────────────────────────────────
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            self._sample_rate = frame.audio_out_sample_rate or self._sample_rate
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, (InterruptionFrame, EndFrame, CancelFrame)):
            # A committed interruption (or teardown): nothing held may ever
            # play again, and the reply the transport already declared over
            # must now be declared over upstream as well.
            reason = "interruption" if isinstance(frame, InterruptionFrame) else "end"
            self._discard_held(reason)
            await self._synthesize_stop()
            await self.push_frame(frame, direction)
            return

        if direction == FrameDirection.UPSTREAM:
            if isinstance(frame, BotStoppedSpeakingFrame) and (self._paused or self._flushing):
                # False end-of-reply caused by the pause itself.
                self._suppressed_stop = True
                self._suppress_next_start = False
                self._event("playback_bot_stop_suppressed")
                return
            if isinstance(frame, BotStartedSpeakingFrame) and self._suppress_next_start:
                # Playback resumed: upstream already believes the bot is
                # speaking — one continuous utterance.
                self._suppress_next_start = False
                return
            await self.push_frame(frame, direction)
            return

        if (self._paused or self._flushing) and not isinstance(frame, SystemFrame):
            self._hold(frame, direction)
            return

        await self.push_frame(frame, direction)

    def _hold(self, frame: Frame, direction: FrameDirection) -> None:
        self._held.append((frame, direction))
        if isinstance(frame, OutputAudioRawFrame):
            self._held_audio_bytes += len(frame.audio)
            cap = int(self._max_hold_seconds * max(self._sample_rate, 8000) * 2)
            while self._held_audio_bytes > cap and self._held:
                old, _ = self._held.popleft()
                if isinstance(old, OutputAudioRawFrame):
                    self._held_audio_bytes -= len(old.audio)
                self._stats["cap_drops"] += 1
