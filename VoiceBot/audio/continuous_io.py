"""Continuous microphone capture with VAD utterance detection and playback.

Barge-in support
================
During bot speech the mic stays open but routes through a dedicated
_InterruptDetector (higher aggressiveness + energy threshold) so that
only real human speech — not room noise or speaker bleed — triggers
an interrupt.

When a barge-in is confirmed:
  1. sd.stop() cuts the speaker immediately.
  2. The frames already captured are handed to the main UtteranceDetector
     so the utterance is not lost — it arrives via utterances() normally.

barge_in_event lifecycle (critical):
  SET   — by audio thread when _InterruptDetector confirms speech
  CLEAR — at the START of every play() call (arms fresh for this response)
           AND at the start of _sd_callback normal-listening path
  This guarantees the event is never stale across turns.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import threading
from collections.abc import AsyncIterator

import numpy as np
import sounddevice as sd

from voicebot.audio.utterance_detector import UtteranceDetector

logger = logging.getLogger(__name__)

_SHUTDOWN_SENTINEL = object()

# ── Barge-in tuning ────────────────────────────────────────────────────────
#
# The core insight: speaker bleed IS speech (voiced, correct energy, low ZCR)
# so signal-property gates alone cannot distinguish it from a human barge-in.
# The solution is two-layered:
#
#   Layer 1 — Grace window (400 ms)
#     No barge-in is allowed in the first 400 ms of any playback.
#     Human minimum reaction time (hear first word → open mouth) is ~300 ms.
#     Speaker bleed is strongest and most likely to trigger during the early
#     frames when the speaker is ramping up.
#
#   Layer 2 — Dynamic bleed baseline + relative threshold
#     During the grace window the detector MEASURES the mic energy while only
#     the bot speaker is playing (guaranteed no human yet). This becomes the
#     bleed_baseline_dbfs. After the grace window, a barge-in only fires if
#     the mic energy is _BARGE_ABOVE_BASELINE_DB (8 dB) ABOVE the baseline.
#     Human voice at 20-40 cm is typically 10-18 dB louder than bleed.
#     So this threshold separates human barge-in from bleed reliably.
#
#   Layer 3 — Consecutive frame confirmation (200 ms)
#     10 consecutive frames (200 ms) must all exceed the relative threshold
#     AND pass WebRTC VAD mode 2. Any failing frame resets the counter.

_BARGE_AGGRESSIVENESS    = 2      # VAD mode 2: better steady-noise rejection
_BARGE_CONFIRM_FRAMES    = 10     # 10 × 20 ms = 200 ms consecutive speech
_BARGE_GRACE_FRAMES      = 20     # 20 × 20 ms = 400 ms no-trigger grace window
_BARGE_BASELINE_FRAMES   = 10     # frames used to measure bleed baseline
_BARGE_ABOVE_BASELINE_DB = 8.0    # human voice must be 8 dB above bleed
_BARGE_FLOOR_DBFS        = -38.0  # absolute minimum energy (ignores silence)
# ───────────────────────────────────────────────────────────────────────────


def _rms_dbfs(frame: bytes) -> float:
    count = len(frame) // 2
    if count == 0:
        return -96.0
    samples = struct.unpack(f"{count}h", frame)
    mean_sq = sum(s * s for s in samples) / count
    if mean_sq == 0:
        return -96.0
    return 20 * math.log10(math.sqrt(mean_sq) / 32768.0)


class _InterruptDetector:
    """
    Barge-in detector that runs in the PortAudio callback thread during
    bot playback only.

    Three-layer design:

    Layer 1 — Grace window (400 ms, _BARGE_GRACE_FRAMES = 20)
      All frames in the first 400 ms are ignored for triggering.
      During this window the first _BARGE_BASELINE_FRAMES (10) frames are
      used to measure the speaker bleed energy level at this mic.
      This adapts automatically to room acoustics and speaker volume.

    Layer 2 — Relative energy threshold
      After the grace window, a frame only qualifies if its RMS energy is
      at least _BARGE_ABOVE_BASELINE_DB (8 dB) above the measured bleed
      baseline AND above the absolute floor _BARGE_FLOOR_DBFS.
      Human voice at 20-40 cm is 10-18 dB above typical bleed — well above
      the 8 dB threshold. Bleed itself never exceeds 0 dB above itself.

    Layer 3 — VAD + consecutive confirmation (200 ms)
      WebRTC VAD mode 2 must confirm speech. Then 10 consecutive qualifying
      frames are required. Any failing frame resets the counter to zero.
    """

    def __init__(self, sample_rate: int, frame_ms: int) -> None:
        import webrtcvad
        self._vad = webrtcvad.Vad(_BARGE_AGGRESSIVENESS)
        self._sample_rate = sample_rate
        self._frame_ms = frame_ms
        self._frame_bytes = sample_rate * frame_ms // 1000 * 2

        self._frame_count = 0            # total frames since last reset
        self._confirm_count = 0          # consecutive qualifying frames

        # Bleed baseline — measured during grace window
        self._baseline_samples: list[float] = []
        self._bleed_baseline_dbfs: float = -96.0  # updated after grace window

        self._captured: list[bytes] = []
        self.triggered = threading.Event()

    # ── Public ──────────────────────────────────────────────────────────

    def feed(self, frame: bytes) -> None:
        if self.triggered.is_set():
            self._captured.append(frame)
            return
        if len(frame) != self._frame_bytes:
            return

        energy = _rms_dbfs(frame)
        self._frame_count += 1

        # ── GRACE WINDOW ─────────────────────────────────────────────────
        if self._frame_count <= _BARGE_GRACE_FRAMES:
            # Measure bleed baseline from first N frames
            if self._frame_count <= _BARGE_BASELINE_FRAMES:
                if energy > -96.0:
                    self._baseline_samples.append(energy)
                # After collecting enough samples, compute baseline
                if self._frame_count == _BARGE_BASELINE_FRAMES:
                    if self._baseline_samples:
                        # Use the 75th percentile so a quiet frame doesn't
                        # underestimate the real bleed level
                        sorted_samples = sorted(self._baseline_samples)
                        p75_idx = max(0, int(len(sorted_samples) * 0.75) - 1)
                        self._bleed_baseline_dbfs = sorted_samples[p75_idx]
                        logger.debug(
                            "[Barge-in] Bleed baseline measured: %.1f dBFS",
                            self._bleed_baseline_dbfs,
                        )
            return  # never trigger during grace window

        # ── POST-GRACE: three-gate check ─────────────────────────────────

        # Gate 1 — absolute floor (silence / very quiet background)
        if energy < _BARGE_FLOOR_DBFS:
            self._confirm_count = 0
            self._captured.clear()
            return

        # Gate 2 — relative threshold above bleed baseline
        # If baseline was never measured (silence during grace), use floor
        effective_baseline = max(self._bleed_baseline_dbfs, _BARGE_FLOOR_DBFS)
        if energy < effective_baseline + _BARGE_ABOVE_BASELINE_DB:
            self._confirm_count = 0
            self._captured.clear()
            return

        # Gate 3 — WebRTC VAD
        if not self._vad.is_speech(frame, self._sample_rate):
            self._confirm_count = 0
            self._captured.clear()
            return

        # All gates passed — accumulate consecutive qualifying frames
        self._confirm_count += 1
        self._captured.append(frame)

        if self._confirm_count >= _BARGE_CONFIRM_FRAMES:
            self.triggered.set()
            logger.info(
                "[Barge-in] Confirmed | frames=%d | energy=%.1f dBFS | "
                "baseline=%.1f dBFS | delta=%.1f dB",
                self._confirm_count,
                energy,
                self._bleed_baseline_dbfs,
                energy - self._bleed_baseline_dbfs,
            )

    # ── Accessors ────────────────────────────────────────────────────────

    def drain_captured(self) -> list[bytes]:
        frames = list(self._captured)
        self._captured.clear()
        return frames

    def reset(self) -> None:
        self._frame_count = 0
        self._confirm_count = 0
        self._baseline_samples = []
        self._bleed_baseline_dbfs = -96.0
        self._captured.clear()
        self.triggered.clear()


class ContinuousAudio:
    def __init__(
        self,
        input_sample_rate: int = 16000,
        frame_ms: int = 20,
        vad_aggressiveness: int = 2,
        channels: int = 1,
    ):
        self._input_sample_rate = input_sample_rate
        self._frame_ms = frame_ms
        self._channels = channels
        self._frame_samples = input_sample_rate * frame_ms // 1000
        self._frame_bytes = self._frame_samples * 2

        self._detector = UtteranceDetector(
            sample_rate=input_sample_rate,
            frame_ms=frame_ms,
            aggressiveness=vad_aggressiveness,
        )
        self._interrupt_detector = _InterruptDetector(
            sample_rate=input_sample_rate,
            frame_ms=frame_ms,
        )

        self._queue: asyncio.Queue[object] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._leftover = b""

        # suppression_event SET  → bot is speaking, suppress main VAD
        self.suppression_event: asyncio.Event = asyncio.Event()

        # barge_in_event SET → interrupt detector confirmed real speech
        # MUST be cleared at the start of every play() call.
        self.barge_in_event: asyncio.Event = asyncio.Event()

        self._stream: sd.InputStream | None = None
        self._shutdown_requested = False

    # ── Shutdown ───────────────────────────────────────────────────────────

    def request_shutdown(self) -> None:
        self._shutdown_requested = True
        if self._queue is None or self._loop is None:
            return
        try:
            self._queue.put_nowait(_SHUTDOWN_SENTINEL)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(_SHUTDOWN_SENTINEL)
            except asyncio.QueueFull:
                logger.warning("Could not enqueue shutdown sentinel")

    # ── Queue helpers ──────────────────────────────────────────────────────

    def _enqueue_utterance(self, utterance: bytes) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(utterance)
        except asyncio.QueueFull:
            logger.warning("Utterance queue full — dropping utterance")

    def _process_frames(self, chunk: bytes) -> None:
        data = self._leftover + chunk
        offset = 0
        while offset + self._frame_bytes <= len(data):
            frame_bytes = data[offset: offset + self._frame_bytes]
            offset += self._frame_bytes
            utterance = self._detector.feed(frame_bytes)
            if utterance is not None and self._loop is not None:
                self._loop.call_soon_threadsafe(
                    self._enqueue_utterance, utterance,
                )
        self._leftover = data[offset:]

    # ── PortAudio callback (audio thread) ──────────────────────────────────

    def _sd_callback(self, indata, frames, time, status) -> None:
        if status:
            logger.warning("PortAudio input status: %s", status)
        if self._queue is None or self._loop is None:
            return

        raw = indata.tobytes()

        if self.suppression_event.is_set():
            # Bot is speaking — barge-in detector only, main VAD suppressed.
            data = self._leftover + raw
            offset = 0
            while offset + self._frame_bytes <= len(data):
                frame = data[offset: offset + self._frame_bytes]
                offset += self._frame_bytes
                self._interrupt_detector.feed(frame)
                if (
                    self._interrupt_detector.triggered.is_set()
                    and not self.barge_in_event.is_set()
                ):
                    self._loop.call_soon_threadsafe(self.barge_in_event.set)
            self._leftover = data[offset:]
        else:
            # Normal listening — clear any stale barge_in_event and feed
            # main detector. This is the safety net that guarantees the
            # event never stays set into the next response cycle.
            if self.barge_in_event.is_set():
                self._loop.call_soon_threadsafe(self.barge_in_event.clear)
            self._process_frames(raw)

    # ── Utterance stream ───────────────────────────────────────────────────

    async def utterances(self) -> AsyncIterator[bytes]:
        self._shutdown_requested = False
        self._queue = asyncio.Queue(maxsize=4)
        self._loop = asyncio.get_running_loop()
        self._leftover = b""
        self._stream = sd.InputStream(
            samplerate=self._input_sample_rate,
            channels=self._channels,
            dtype="int16",
            blocksize=self._frame_samples * 2,
            callback=self._sd_callback,
        )
        try:
            self._stream.start()
            while not self._shutdown_requested:
                item = await self._queue.get()
                if item is _SHUTDOWN_SENTINEL:
                    break
                yield item
        finally:
            self._shutdown_requested = False
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
            self._loop = None
            self._leftover = b""

    # ── Barge-in state (shared with TTSStreamPlayer) ────────────────────────

    def reset_barge_in_state(self) -> None:
        """Clear stale barge-in flag and interrupt detector.

        Must run at the start of every bot playback (greeting, streaming TTS,
        or fallback play). If skipped after a streaming barge-in,
        ``_InterruptDetector.triggered`` stays set and the next response is
        cut off on the first sentence.
        """
        self.barge_in_event.clear()
        self._interrupt_detector.reset()

    def handoff_barge_in_captured(self) -> None:
        """Replay frames captured during a streaming-TTS barge-in into main VAD."""
        captured = self._interrupt_detector.drain_captured()
        self._interrupt_detector.reset()
        if not captured or self._loop is None:
            return
        logger.info(
            "[Barge-in] Replaying %d captured frames into main detector",
            len(captured),
        )
        for frame in captured:
            utterance = self._detector.feed(frame)
            if utterance is not None:
                self._loop.call_soon_threadsafe(
                    self._enqueue_utterance, utterance,
                )

    # ── Playback with barge-in ─────────────────────────────────────────────

    async def play(self, audio_bytes: bytes, sample_rate: int = 8000) -> None:
        """
        Play audio while listening for barge-in.
        barge_in_event is ALWAYS cleared at entry so a previous turn's
        barge-in never leaks into this response.
        """
        if not audio_bytes:
            return

        self.reset_barge_in_state()

        self.suppression_event.set()
        self._detector.reset()
        self._leftover = b""

        loop = asyncio.get_running_loop()
        interrupted = False

        try:
            arr = np.frombuffer(audio_bytes, dtype=np.int16)
            silence = np.zeros(int(sample_rate * 0.15), dtype=np.int16)
            padded = np.concatenate([arr, silence])

            sd.play(padded, samplerate=sample_rate)

            # Poll every 20 ms — stop immediately if barge-in fires.
            while sd.get_stream().active:
                if self.barge_in_event.is_set():
                    sd.stop()
                    interrupted = True
                    logger.info("[Barge-in] Playback stopped")
                    break
                await asyncio.sleep(0.02)

            if not interrupted:
                await loop.run_in_executor(None, sd.wait)

        finally:
            await asyncio.sleep(0.1)
            self._detector.reset()
            self._leftover = b""
            self.suppression_event.clear()

            # ── CRITICAL: always clear barge_in_event on exit ─────────────
            self.barge_in_event.clear()
            # ─────────────────────────────────────────────────────────────

            if interrupted:
                self.handoff_barge_in_captured()
            else:
                self._interrupt_detector.reset()

            # Additional wait for room acoustics / DAC hardware drain
            await asyncio.sleep(0.15)
            self._detector.reset()   # reset again — mic may have caught speaker tail
            self._leftover = b""
            self.suppression_event.clear()