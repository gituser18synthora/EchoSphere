"""Caller-audio noise gate — the first stage of speech/noise separation.

Sits between ``transport.input()`` and the VAD, so nothing downstream (VAD,
turn detection, STT, the LLM) ever sees audio this gate judged to be
background noise. That ordering is the point: a fan, a keyboard, a distant
conversation or the bot's own speaker bleed reaching the VAD is what makes the
bot "start listening" to nothing, and it is what makes realtime STT hallucinate
words out of hiss.

Three layers cooperate, cheapest first:

1. **this gate** — adaptive energy relative to the noise floor *measured on
   this call*, plus a sustained-speech requirement and a raised bar while the
   bot is speaking (echo/barge-in guard);
2. **Silero VAD** (downstream) — neural speech probability on what survives;
3. **the transcript gate** (:mod:`voice_runtime.transcript_gate`) — provider
   quality metadata, script and language validation on final transcripts.

Why energy first and not a second neural VAD: Silero already runs immediately
downstream on every frame, so a second neural pass would double CPU per call
for the same verdict. What Silero cannot do is judge *this line's* noise floor
— it has no notion of "loud for this caller" — and that is exactly the signal
an absolute threshold gets wrong. A steady background (fan, hum, road noise)
raises the floor, so the bar rises with it; transients that beat the bar
(keyboard, door, mic handling) are cut by the sustained-speech requirement;
quiet-but-real speech on a bad line still passes because the bar is relative.

Why this layer is load-bearing rather than belt-and-braces: pipecat's
``VADParams.min_volume`` is NOT a full-scale amplitude, it is normalised EBU
R128 loudness over a -20..80 LUFS range. Measured against this build, a -60 dBFS
tone already scores 0.494 and a -30 dBFS tone scores 0.795, so the configured
gates (0.4 telephony / 0.6 browser) are effectively inert for anything that is
not digital silence. Before this gate existed, background noise was therefore
held back by Silero's speech confidence alone.

Closed-gate behaviour is silence *substitution*, never frame dropping: the
downstream stream stays continuous and correctly paced, so the VAD's timers,
the STT socket's keepalive and the call recording all keep working. When the
gate opens it first emits the retained pre-roll so a word's onset ("हाँ",
"yes") is never clipped — the cost is that each utterance carries a pre-roll's
worth of extra audio duration (~160 ms), which is deliberate: it buys onset
fidelity without adding any delay to end-of-turn detection.
"""

import logging
import math
import time

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

# A "noise floor" is only meaningful inside this band. Below it the line is
# digitally silent and a relative threshold would open on anything; above it we
# are tracking speech, not noise, so the floor must not follow.
_FLOOR_MIN_DBFS = -90.0
_FLOOR_MAX_DBFS = -35.0
# Asymmetric tracking: follow a falling floor quickly (a quiet gap is the best
# estimate available) and a rising one slowly, so a burst of noise cannot walk
# the threshold up and deafen the gate.
_FLOOR_FALL_ALPHA = 0.25
_FLOOR_RISE_ALPHA = 0.02
_SILENT_DBFS = -120.0
_STATS_LOG_SECONDS = 15.0


def frame_dbfs(audio: bytes) -> float:
    """RMS level of PCM16 audio in dBFS (full scale = 0 dB)."""
    if not audio or len(audio) < 2:
        return _SILENT_DBFS
    samples = np.frombuffer(audio[: len(audio) - (len(audio) % 2)], dtype="<i2")
    if samples.size == 0:
        return _SILENT_DBFS
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32)))))
    if rms <= 0.0:
        return _SILENT_DBFS
    return 20.0 * math.log10(rms / 32768.0)


class CallerAudioGate(FrameProcessor):
    """Adaptive noise gate for the inbound caller stream.

    Args:
        noise_margin_db: How far above the measured noise floor caller audio
            must sit to count as speech.
        min_speech_ms: Sustained above-threshold audio required to open the
            gate. Rejects clicks, taps and other transients.
        echo_min_speech_ms: The same requirement while the bot is speaking —
            a real barge-in must be sustained slightly longer than a blip of
            speaker echo, but still short enough to interrupt promptly.
        hangover_ms: Below-threshold audio tolerated before the gate closes.
            Covers the natural gaps inside a word.
        preroll_ms: Audio retained while closed and emitted on open, so the
            onset of the first word is not clipped.
        echo_margin_db: Extra margin required while the bot is speaking and
            for ``echo_tail_ms`` afterwards (speaker bleed and room echo).
        echo_tail_ms: How long the echo margin outlives the bot's audio.
        min_threshold_dbfs: Absolute floor for the threshold, so a digitally
            silent line cannot produce an effectively open gate.
    """

    def __init__(
        self,
        *,
        noise_margin_db: float = 9.0,
        min_speech_ms: float = 120.0,
        echo_min_speech_ms: float = 180.0,
        hangover_ms: float = 320.0,
        preroll_ms: float = 160.0,
        echo_margin_db: float = 6.0,
        echo_tail_ms: float = 250.0,
        min_threshold_dbfs: float = -45.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._noise_margin_db = noise_margin_db
        self._min_speech_ms = min_speech_ms
        self._echo_min_speech_ms = echo_min_speech_ms
        self._hangover_ms = hangover_ms
        # The gate can only open AFTER the sustained-speech requirement is
        # met, so preroll shorter than that requirement guarantees the first
        # syllables of every utterance are evicted before the gate opens —
        # clipping exactly the audio the STT (and the barge-in word gate)
        # needs. Clamp so retention always covers the open delay.
        self._preroll_ms = max(preroll_ms, min_speech_ms, echo_min_speech_ms)
        self._echo_margin_db = echo_margin_db
        self._echo_tail_ms = echo_tail_ms
        self._min_threshold_dbfs = min_threshold_dbfs

        self._floor_dbfs: float | None = None
        self._open = False
        self._above_ms = 0.0
        self._below_ms = 0.0
        self._preroll: list[bytes] = []
        self._preroll_ms_held = 0.0
        self._bot_speaking = False
        self._bot_stopped_at: float | None = None
        self._sample_rate = 0
        self._last_stats_log = 0.0
        # Evidence about the speech segment currently passing (or the one that
        # just finished): how far it sat above this line's noise floor, and
        # whether bot audio was playing while it was captured. The brain feeds
        # both into the transcript quality gate.
        self._segment_power = 0.0
        self._segment_ms = 0.0
        self._segment_during_bot_audio = False
        self._last_segment: dict | None = None
        # Backchannel window: the bot is murmuring a short "hmm/ji" WHILE the
        # caller holds the floor. For a segment that is already passing, that
        # audio must be treated as decoration, not as the bot taking the
        # floor — no echo-margin raise mid-utterance and no during_bot_audio
        # latch, either of which would get the caller's own speech chopped or
        # rejected by the transcript gate. A CLOSED gate keeps full echo
        # protection so the backchannel's own acoustic echo cannot open it.
        self._backchannel_active = False
        self._backchannel_ended_at: float | None = None
        # Bounded post-gate PCM retention for identifier batch recovery
        # (voice_runtime.identifier_capture). OFF by default — outside an
        # identifier collection window no caller audio is retained at all.
        self._retain_enabled = False
        self._retain_max_seconds = 0.0
        self._retained: list[bytes] = []
        self._retained_bytes = 0
        self._retained_rate = 0
        # Diagnostics only — no audio and no transcript text is ever retained.
        self._stats = {
            "opens": 0,
            "suppressed_ms": 0.0,
            "passed_ms": 0.0,
            "echo_guard_ms": 0.0,
        }

    # ── utterance retention (identifier batch recovery) ─────────────────
    def enable_utterance_retention(self, max_seconds: float = 30.0) -> None:
        """Keep the post-gate caller PCM (bounded) while an identifier is
        being dictated, so ONE batch transcription can recover a value the
        streaming STT mangled. Only audio that passed the gate is retained;
        the cap keeps memory bounded (oldest audio drops first)."""
        self._retain_enabled = True
        self._retain_max_seconds = max(1.0, float(max_seconds))

    def disable_utterance_retention(self) -> None:
        self._retain_enabled = False
        self.clear_retained_audio()

    def clear_retained_audio(self) -> None:
        self._retained.clear()
        self._retained_bytes = 0

    def take_retained_audio(self) -> tuple[bytes, int] | None:
        """(pcm16 bytes, sample_rate) retained so far, clearing the buffer."""
        if not self._retained or not self._retained_rate:
            return None
        audio, rate = b"".join(self._retained), self._retained_rate
        self.clear_retained_audio()
        return audio, rate

    def _retain(self, audio: bytes, sample_rate: int) -> None:
        if not self._retain_enabled or not audio or not sample_rate:
            return
        if sample_rate != self._retained_rate:
            self.clear_retained_audio()
            self._retained_rate = sample_rate
        self._retained.append(bytes(audio))
        self._retained_bytes += len(audio)
        cap = int(self._retain_max_seconds * sample_rate * 2)
        while self._retained_bytes > cap and len(self._retained) > 1:
            self._retained_bytes -= len(self._retained.pop(0))

    # ── state helpers ────────────────────────────────────────────────────
    def begin_backchannel_window(self) -> None:
        self._backchannel_active = True
        self._backchannel_ended_at = None

    def end_backchannel_window(self) -> None:
        if self._backchannel_active:
            self._backchannel_active = False
            self._backchannel_ended_at = time.monotonic()

    def _backchannel_shielded(self) -> bool:
        """Open-gate shield: the current bot audio (and its echo tail) is a
        mid-caller-turn backchannel, so the live caller segment must not be
        echo-guarded against it."""
        if not self._open:
            return False
        if self._backchannel_active:
            return True
        if self._backchannel_ended_at is None:
            return False
        return (
            time.monotonic() - self._backchannel_ended_at
        ) * 1000.0 < self._echo_tail_ms

    @property
    def live_speech_ms(self) -> float:
        """Duration of the caller speech segment currently passing (0 when
        the gate is closed) — the backchannel controller's floor-holding
        evidence."""
        return self._segment_ms if self._open else 0.0

    @property
    def segments_started(self) -> int:
        """How many distinct caller speech segments have opened the gate.

        Independent of the VAD/word-gate turn machinery, so it still ticks
        for speech bursts that never open a user turn (sub-word-gate digits
        while the bot is speaking). The brain uses it as segment provenance:
        a new burst since a buffered STT final proves a later prefix-matching
        final is NEW speech, not a cumulative re-emission.
        """
        return int(self._stats["opens"])

    def _echo_guarded(self) -> bool:
        """Whether bot audio may still be bleeding into the caller stream."""
        if self._backchannel_shielded():
            return False
        if self._bot_speaking:
            return True
        if self._bot_stopped_at is None:
            return False
        return (time.monotonic() - self._bot_stopped_at) * 1000.0 < self._echo_tail_ms

    def threshold_dbfs(self) -> float:
        """Current speech threshold: noise floor + margins, absolutely bounded."""
        floor = _FLOOR_MAX_DBFS if self._floor_dbfs is None else self._floor_dbfs
        threshold = floor + self._noise_margin_db
        if self._echo_guarded():
            threshold += self._echo_margin_db
        return max(threshold, self._min_threshold_dbfs)

    def _track_floor(self, level_dbfs: float) -> None:
        """Fold a non-speech frame's level into the noise-floor estimate."""
        if self._floor_dbfs is None:
            self._floor_dbfs = min(max(level_dbfs, _FLOOR_MIN_DBFS), _FLOOR_MAX_DBFS)
            return
        alpha = _FLOOR_FALL_ALPHA if level_dbfs < self._floor_dbfs else _FLOOR_RISE_ALPHA
        updated = self._floor_dbfs + (level_dbfs - self._floor_dbfs) * alpha
        self._floor_dbfs = min(max(updated, _FLOOR_MIN_DBFS), _FLOOR_MAX_DBFS)

    def _required_speech_ms(self) -> float:
        return self._echo_min_speech_ms if self._echo_guarded() else self._min_speech_ms

    def _begin_segment(self) -> None:
        self._segment_power = 0.0
        self._segment_ms = 0.0
        self._segment_during_bot_audio = self._echo_guarded()

    def _accumulate_segment(self, level_dbfs: float, duration_ms: float) -> None:
        # Averaged in the power domain: dB values cannot be averaged directly
        # without under-weighting the loud (i.e. actually spoken) frames.
        self._segment_power += (10.0 ** (level_dbfs / 10.0)) * duration_ms
        self._segment_ms += duration_ms
        if self._echo_guarded():
            self._segment_during_bot_audio = True

    def _close_segment(self) -> None:
        self._last_segment = self.speech_snapshot()
        self._segment_power = 0.0
        self._segment_ms = 0.0
        self._segment_during_bot_audio = False

    def speech_snapshot(self) -> dict | None:
        """Evidence about the live speech segment, or the last one to finish.

        ``snr_db`` is the segment's mean level above the measured noise floor —
        a relative measure, so it means the same thing on a loud handset and a
        quiet PSTN trunk. Returns None before any speech has been observed.
        """
        if self._segment_ms <= 0:
            return self._last_segment
        mean_dbfs = 10.0 * math.log10(
            max(self._segment_power / self._segment_ms, 1e-12)
        )
        floor = self._floor_dbfs if self._floor_dbfs is not None else _FLOOR_MAX_DBFS
        return {
            "snr_db": round(mean_dbfs - floor, 1),
            "speech_dbfs": round(mean_dbfs, 1),
            "during_bot_audio": self._segment_during_bot_audio,
        }

    def stats(self) -> dict:
        """Gate diagnostics for the call (counters and durations only)."""
        return {
            **{k: (round(v, 1) if isinstance(v, float) else v)
               for k, v in self._stats.items()},
            "noise_floor_dbfs": (round(self._floor_dbfs, 1)
                                 if self._floor_dbfs is not None else None),
        }

    # ── frame handling ───────────────────────────────────────────────────
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            self._sample_rate = frame.audio_in_sample_rate or self._sample_rate
            await self.push_frame(frame, direction)
            return

        # Bot-speaking boundaries arrive UPSTREAM from the output transport.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()
            await self.push_frame(frame, direction)
            return

        if not isinstance(frame, InputAudioRawFrame) or not frame.audio:
            await self.push_frame(frame, direction)
            return

        await self._process_audio(frame, direction)

    async def _process_audio(self, frame: InputAudioRawFrame, direction) -> None:
        sample_rate = frame.sample_rate or self._sample_rate or 8000
        channels = max(1, frame.num_channels or 1)
        duration_ms = (len(frame.audio) / 2 / channels) / sample_rate * 1000.0
        level = frame_dbfs(frame.audio)
        threshold = self.threshold_dbfs()
        speechlike = level >= threshold
        if self._echo_guarded():
            self._stats["echo_guard_ms"] += duration_ms

        if speechlike:
            self._above_ms += duration_ms
            self._below_ms = 0.0
        else:
            self._below_ms += duration_ms
            self._above_ms = 0.0
            # Only non-speech frames may move the floor, and only while the
            # gate is shut: adapting during speech would chase the caller's
            # own voice and shut the gate on them mid-sentence.
            if not self._open:
                self._track_floor(level)

        if self._open:
            self._accumulate_segment(level, duration_ms)
            if self._below_ms >= self._hangover_ms:
                self._open = False
                self._below_ms = 0.0
                self._close_segment()
            self._stats["passed_ms"] += duration_ms
            self._retain(frame.audio, sample_rate)
            await self.push_frame(frame, direction)
            self._maybe_log_stats()
            return

        if self._above_ms >= self._required_speech_ms():
            # Sustained speech confirmed: open and release the retained
            # pre-roll so the caller's first syllable survives.
            self._open = True
            self._above_ms = 0.0
            self._below_ms = 0.0
            self._stats["opens"] += 1
            self._begin_segment()
            self._accumulate_segment(level, duration_ms)
            for buffered in self._preroll:
                self._retain(buffered, sample_rate)
                await self.push_frame(
                    InputAudioRawFrame(
                        audio=buffered,
                        sample_rate=sample_rate,
                        num_channels=channels,
                    ),
                    direction,
                )
            self._stats["passed_ms"] += self._preroll_ms_held + duration_ms
            self._preroll.clear()
            self._preroll_ms_held = 0.0
            self._retain(frame.audio, sample_rate)
            await self.push_frame(frame, direction)
            self._maybe_log_stats()
            return

        # Gate closed: retain the audio for pre-roll and pass silence on, so
        # downstream timing, keepalives and recording stay intact while the
        # VAD and STT are given nothing to react to.
        self._preroll.append(frame.audio)
        self._preroll_ms_held += duration_ms
        while self._preroll_ms_held > self._preroll_ms and len(self._preroll) > 1:
            dropped = self._preroll.pop(0)
            self._preroll_ms_held -= (
                (len(dropped) / 2 / channels) / sample_rate * 1000.0
            )
        self._stats["suppressed_ms"] += duration_ms
        frame.audio = b"\x00" * len(frame.audio)
        await self.push_frame(frame, direction)
        self._maybe_log_stats()

    def _maybe_log_stats(self) -> None:
        now = time.monotonic()
        if self._last_stats_log == 0.0:
            self._last_stats_log = now
            return
        if now - self._last_stats_log < _STATS_LOG_SECONDS:
            return
        self._last_stats_log = now
        logger.info(
            "caller audio gate: floor=%.1fdBFS threshold=%.1fdBFS opens=%d "
            "passed=%.0fms suppressed=%.0fms",
            self._floor_dbfs if self._floor_dbfs is not None else float("nan"),
            self.threshold_dbfs(),
            self._stats["opens"],
            self._stats["passed_ms"],
            self._stats["suppressed_ms"],
        )
