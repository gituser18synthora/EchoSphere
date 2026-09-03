"""Latency fillers: a breath in the gap before the reply starts.

Between the caller's last word and the first byte of reply audio the bot is
silent for as long as turn detection, the decision layer, the LLM and the
TTS provider take together — 1.5–4 s on telephony, worst on the first reply
of a call (cold decision/LLM/knowledge paths). A human agent is never that
silent: they breathe, hum, shift. This module plays a short, voice-gender-
matched breath from pre-rendered audio when the reply has not started
speaking ``delay_ms`` after the caller stopped, and cuts it the instant
real reply audio arrives.

Design constraints, in priority order:

* **Never delay the reply.** The clip is streamed to the transport in 20 ms
  chunks at real-time pace (two chunks of lead), so when reply audio shows
  up at most ~40 ms of breath sits ahead of it in the transport queue, plus
  one 20 ms taper chunk so the cut ends as a breath rather than a click.
  Reply audio is never mixed with, queued behind or held for the filler.
* **Invisible to turn bookkeeping.** Chunks are plain ``OutputAudioRawFrame``
  instances — pipecat's output transport flips bot-speaking state only for
  ``TTSAudioRawFrame`` / ``SpeechOutputAudioRawFrame`` — so no
  ``BotStartedSpeakingFrame`` fires: the brain's latency measurement, the
  barge-in/merge discriminator, the word-confirmed barge-in gate and the
  audio gate's echo guard all still see a quiet bot. A caller who talks over
  a breath simply opens a turn, exactly as they would over silence.
* **One opportunity per dispatched turn.** Armed by the brain at dispatch,
  disarmed by every cancellation path (barge-in, late merge, hang-up,
  teardown) and by the first reply audio. Nothing is spoken, so history,
  turn records and the client transcript never see it.
* **Gender-matched.** Clips come from the operator asset directory
  (``Settings.filler_audio_dir``: WAV files whose name carries a ``male`` /
  ``female`` / ``neutral`` token) or are synthesized here per gender.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from shared.audio.pcm import apply_fade_in, apply_fade_out, resample_pcm, wav_to_pcm

logger = logging.getLogger(__name__)

GENDERS = ("male", "female", "neutral")
# Two clip kinds: the pre-reply ``breath`` (front-loaded, trailing off into
# the wait) and the in-reply ``inhale`` (short, RISING into the sentence that
# follows — the shape a person makes right before speaking; the exhale shape
# trimmed and dropped between two sentences sounds like a cut, not a breath).
KINDS = ("breath", "inhale")


def normalize_gender(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ("male", "female") else "neutral"


# --------------------------------------------------------------------------
# Synthesized breath
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _BreathProfile:
    """Spectral/temporal shape of one gender's synthesized breath.

    The band is a log-domain bump: breath noise concentrates between a few
    hundred Hz and ~3 kHz, lower and longer for a male voice, lighter and
    shorter for a female one. Levels sit well below speech (TTS peaks around
    -6 dBFS, RMS -18..-20 dBFS): audible presence, never a word.
    """

    duration_s: float
    band_low_hz: float
    band_peak_hz: float
    band_high_hz: float
    # Beta-shaped envelope x^attack · (1-x)^decay: the peak sits at
    # attack / (attack + decay) of the clip — front-loaded, like an inhale
    # that builds quickly and tails off. A reply that lands 200–300 ms into
    # the breath therefore cuts a breath that was already audible, not one
    # still in its attack.
    attack: float
    decay: float
    target_rms_dbfs: float  # RMS of the loudest 100 ms window


_PROFILES: dict[str, _BreathProfile] = {
    "male": _BreathProfile(0.88, 180.0, 700.0, 2600.0, 1.3, 2.6, -30.0),
    "female": _BreathProfile(0.74, 320.0, 1250.0, 3600.0, 1.4, 2.4, -31.0),
    "neutral": _BreathProfile(0.80, 250.0, 950.0, 3100.0, 1.3, 2.5, -30.5),
}
# In-reply inhale: about a third of a second, energy building toward its end
# (peak at ~2/3) so it runs INTO the next sentence, a touch brighter (air
# drawn through the mouth) and quieter than the pre-reply breath, since it
# sits right next to speech.
_INHALE_PROFILES: dict[str, _BreathProfile] = {
    "male": _BreathProfile(0.34, 260.0, 900.0, 3000.0, 2.6, 1.2, -33.0),
    "female": _BreathProfile(0.30, 420.0, 1500.0, 3900.0, 2.6, 1.1, -34.0),
    "neutral": _BreathProfile(0.32, 330.0, 1150.0, 3400.0, 2.6, 1.15, -33.5),
}
# A few variants per gender so consecutive fillers in one call never sound
# like the same recording; deterministic seeds keep every process identical.
_VARIANTS_PER_GENDER = 3
_SEEDS = {"male": 1101, "female": 2203, "neutral": 3307}
_PEAK_CEILING_DBFS = -12.0


def _spectral_gain(freqs: np.ndarray, profile: _BreathProfile, sample_rate: int) -> np.ndarray:
    """Log-Gaussian bump centred on the profile peak, -12 dB at the band
    edges, rolled off below 80 Hz and near Nyquist (8 kHz telephony safe)."""
    log_f = np.log(np.maximum(freqs, 1.0))
    half_width = (math.log(profile.band_high_hz) - math.log(profile.band_low_hz)) / 2.0
    sigma = half_width / math.sqrt(2.0 * math.log(4.0))  # ×0.25 amplitude at the edges
    gain = np.exp(-((log_f - math.log(profile.band_peak_hz)) ** 2) / (2.0 * sigma**2))
    gain[freqs < 80.0] = 0.0
    nyquist = sample_rate / 2.0
    gain *= np.clip((nyquist * 0.95 - freqs) / (nyquist * 0.05), 0.0, 1.0)
    return gain


def synthesize_breath(
    gender: str, sample_rate: int, *, variant: int = 0, kind: str = "breath"
) -> bytes:
    """Deterministic 16-bit mono PCM breath for ``gender`` at ``sample_rate``.

    Shaped noise under a smooth Beta envelope with a faint slow flutter so it
    never reads as steady hiss: front-loaded for the pre-reply ``breath``,
    rising for the in-reply ``inhale``. Starts and ends at zero — no fades
    needed, no clicks.
    """
    gender = normalize_gender(gender)
    kind = kind if kind in KINDS else "breath"
    profile = (_INHALE_PROFILES if kind == "inhale" else _PROFILES)[gender]
    if sample_rate <= 0:
        return b""
    variant = int(variant) % _VARIANTS_PER_GENDER
    rng = np.random.default_rng(_SEEDS[gender] + 17 * variant + (5000 if kind == "inhale" else 0))
    duration = profile.duration_s * (1.0 + 0.08 * (variant - 1))
    n = int(sample_rate * duration)
    if n < 16:
        return b""
    noise = rng.standard_normal(n)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    shaped = np.fft.irfft(np.fft.rfft(noise) * _spectral_gain(freqs, profile, sample_rate), n)
    x = np.linspace(0.0, 1.0, n, endpoint=False)
    envelope = (x ** profile.attack) * ((1.0 - x) ** profile.decay)
    envelope /= float(envelope.max())
    flutter_hz = 5.5 + 0.7 * variant
    envelope *= 1.0 + 0.06 * np.sin(2.0 * np.pi * flutter_hz * x * duration + variant)
    signal = shaped * envelope
    window = max(1, int(sample_rate * 0.1))
    power = np.convolve(signal**2, np.ones(window) / window, mode="valid")
    peak_rms = math.sqrt(float(power.max())) if power.size else 0.0
    if peak_rms <= 0.0:
        return b""
    signal *= (10 ** (profile.target_rms_dbfs / 20.0) * 32767.0) / peak_rms
    ceiling = 10 ** (_PEAK_CEILING_DBFS / 20.0) * 32767.0
    peak = float(np.max(np.abs(signal)))
    if peak > ceiling:
        signal *= ceiling / peak
    return np.clip(np.rint(signal), -32768, 32767).astype("<i2").tobytes()


# --------------------------------------------------------------------------
# Clip library: operator recordings first, synthesized fallback
# --------------------------------------------------------------------------


def scale_pcm(pcm: bytes, gain_db: float) -> bytes:
    """Apply a gain (dB, negative = quieter) to 16-bit mono PCM."""
    if not pcm or not gain_db:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float32)
    samples *= 10 ** (gain_db / 20.0)
    return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()


def gender_from_filename(path: Path) -> str | None:
    """``filler_female_1.wav`` → ``female``; a token match, so ``female``
    never reads as ``male``. None when the name carries no gender token."""
    for token in re.split(r"[^a-z]+", path.stem.lower()):
        if token in GENDERS:
            return token
    return None


def kind_from_filename(path: Path) -> str:
    """``inhale_female.wav`` → ``inhale`` (in-reply); anything else is the
    pre-reply ``breath``."""
    tokens = set(re.split(r"[^a-z]+", path.stem.lower()))
    return "inhale" if "inhale" in tokens else "breath"


class _FileClip:
    def __init__(self, path: Path) -> None:
        self.path = path

    def describe(self) -> str:
        return f"file:{self.path.name}"

    def render(self, sample_rate: int) -> bytes:
        try:
            pcm, rate = wav_to_pcm(self.path.read_bytes())
        except OSError:
            logger.warning("latency-filler: cannot read %s", self.path, exc_info=True)
            return b""
        if not pcm or rate <= 0:
            logger.warning(
                "latency-filler: %s is not a 16-bit PCM WAV file; ignored", self.path.name
            )
            return b""
        if rate != sample_rate:
            pcm = resample_pcm(pcm, rate, sample_rate)
        pcm = apply_fade_in(pcm, sample_rate=sample_rate, fade_ms=10)
        return apply_fade_out(pcm, sample_rate=sample_rate, fade_ms=10)


class _SynthClip:
    def __init__(self, gender: str, variant: int, kind: str = "breath") -> None:
        self.gender = gender
        self.variant = variant
        self.kind = kind

    def describe(self) -> str:
        prefix = "synth" if self.kind == "breath" else f"synth-{self.kind}"
        return f"{prefix}:{self.gender}:{self.variant}"

    def render(self, sample_rate: int) -> bytes:
        return synthesize_breath(
            self.gender, sample_rate, variant=self.variant, kind=self.kind
        )


class FillerClipLibrary:
    """Per-gender filler clips, rendered once per output sample rate.

    A gender with operator files in ``directory`` uses them (all of them, in
    rotation); a gender without files gets the synthesized breath variants.
    Neutral voices never borrow gendered recordings. The directory is scanned
    lazily on first use, so a missing or unreadable directory costs nothing
    and simply means "synthesized".
    """

    def __init__(self, directory: str | Path | None = None, *, synthesize: bool = True) -> None:
        self._directory = Path(directory) if directory else None
        self._synthesize = synthesize
        self._sources: dict[tuple[str, str], list] | None = None
        self._rendered: dict[tuple[str, str, int, int], bytes] = {}
        self._cursor: dict[tuple[str, str], int] = {}

    def _scan(self) -> dict[tuple[str, str], list]:
        sources: dict[tuple[str, str], list] = {
            (kind, gender): [] for kind in KINDS for gender in GENDERS
        }
        if self._directory is not None:
            try:
                files = sorted(
                    path for path in self._directory.iterdir()
                    if path.is_file() and path.suffix.lower() == ".wav"
                )
            except OSError:
                files = []
            for path in files:
                gender = gender_from_filename(path)
                if gender is None:
                    logger.info(
                        "latency-filler: %s carries no male/female/neutral token; ignored",
                        path.name,
                    )
                    continue
                sources[(kind_from_filename(path), gender)].append(_FileClip(path))
        if self._synthesize:
            for key in sources:
                if not sources[key]:
                    kind, gender = key
                    sources[key] = [
                        _SynthClip(gender, variant, kind)
                        for variant in range(_VARIANTS_PER_GENDER)
                    ]
        return sources

    def sources_for(self, gender: str, kind: str = "breath") -> list:
        if self._sources is None:
            self._sources = self._scan()
        kind = kind if kind in KINDS else "breath"
        return self._sources[(kind, normalize_gender(gender))]

    def describe(self, kind: str = "breath") -> dict[str, list[str]]:
        return {
            gender: [s.describe() for s in self.sources_for(gender, kind)]
            for gender in GENDERS
        }

    def clip(
        self, gender: str, sample_rate: int, *,
        kind: str = "breath", max_ms: int | None = None, gain_db: float = 0.0,
    ) -> bytes:
        """The next ``kind`` clip for ``gender`` (rotating), or b"" when none
        renders.

        Operator files that fail to render (not a PCM WAV, unreadable) are
        skipped; when none of a gender's files renders, the synthesized
        variants take over so a bad upload degrades to the default, never to
        dead air where a breath was configured. ``max_ms`` trims the clip
        (with a short fade-out) and ``gain_db`` lowers it.
        """
        gender = normalize_gender(gender)
        kind = kind if kind in KINDS else "breath"
        sources = self.sources_for(gender, kind)
        if sample_rate <= 0:
            return b""
        clip = self._next_rendered((kind, gender), sources, int(sample_rate))
        if not clip and self._synthesize and not any(
            isinstance(source, _SynthClip) for source in sources
        ):
            logger.warning(
                "latency-filler: no %s %s clip file renders; using synthesized one",
                gender, kind,
            )
            sources.extend(_SynthClip(gender, v, kind) for v in range(_VARIANTS_PER_GENDER))
            clip = self._next_rendered((kind, gender), sources, int(sample_rate))
        if clip and max_ms is not None:
            limit = int(sample_rate * max(50, int(max_ms)) / 1000) * 2
            if len(clip) > limit:
                clip = apply_fade_out(clip[:limit], sample_rate=int(sample_rate), fade_ms=60)
        if clip and gain_db:
            clip = scale_pcm(clip, gain_db)
        return clip

    def _next_rendered(self, slot: tuple[str, str], sources: list, sample_rate: int) -> bytes:
        if not sources:
            return b""
        start = self._cursor.get(slot, 0)
        for step in range(len(sources)):
            index = (start + step) % len(sources)
            key = (*slot, index, sample_rate)
            clip = self._rendered.get(key)
            if clip is None:
                clip = sources[index].render(sample_rate)
                self._rendered[key] = clip
            if clip:
                self._cursor[slot] = index + 1
                return clip
        return b""


_library: FillerClipLibrary | None = None


def get_filler_library() -> FillerClipLibrary:
    """Process-wide library (scanned once; clips rendered once per rate)."""
    global _library
    if _library is None:
        from shared.config import get_settings

        _library = FillerClipLibrary(get_settings().filler_audio_dir)
    return _library


# --------------------------------------------------------------------------
# Pipeline processor
# --------------------------------------------------------------------------

# A speech-stop mark older than this cannot belong to the turn being
# dispatched (no VAD on this path, or a stale probe): time from dispatch.
_MAX_SPEECH_STOP_AGE_S = 10.0


@dataclass
class _ArmedTurn:
    turn_id: int
    gender: str
    origin: float            # monotonic: when the caller stopped (or dispatch)
    fire_at: float
    playing_since: float | None = None
    clip_ms: float = 0.0
    # Streaming position: the clip being played and the byte offset of the
    # next chunk, so a cut can taper from exactly where playback stopped.
    clip: bytes = b""
    next_offset: int = 0


def _taper(chunk: bytes) -> bytes:
    """Linear fade of one chunk to silence (click-free end of a cut breath)."""
    if len(chunk) < 4:
        return b""
    samples = np.frombuffer(chunk[: len(chunk) - (len(chunk) % 2)], dtype="<i2").astype(np.float32)
    samples *= np.linspace(1.0, 0.0, samples.size, dtype=np.float32)
    return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()


class LatencyFillerProcessor(FrameProcessor):
    """Sits between the TTS service and the output transport.

    The brain arms it per dispatched turn (``arm``) and disarms it on every
    cancellation (``cancel``); reply audio, interruptions, caller speech and
    bot-speaking frames passing through disarm/cut it on their own. The
    processor never withholds or alters a frame.
    """

    def __init__(
        self,
        *,
        delay_ms: int,
        library: FillerClipLibrary,
        sample_rate: int = 24000,
        recorder=None,
        chunk_ms: int = 20,
        lead_chunks: int = 2,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._delay_s = max(0.0, float(delay_ms) / 1000.0)
        self._library = library
        self._sample_rate = int(sample_rate)
        self._recorder = recorder
        self._chunk_ms = max(10, int(chunk_ms))
        self._lead_chunks = max(0, int(lead_chunks))
        self._armed: _ArmedTurn | None = None
        self._task: asyncio.Task | None = None
        self._bot_speaking = False
        self.fillers_played = 0
        # Armed turns whose reply audio arrived before the deadline — the
        # common case, and the number that says whether the delay is tuned.
        self.fillers_unneeded = 0

    # -- state ---------------------------------------------------------

    @property
    def delay_ms(self) -> int:
        return int(round(self._delay_s * 1000.0))

    @property
    def armed(self) -> bool:
        return self._armed is not None

    @property
    def playing(self) -> bool:
        return self._armed is not None and self._armed.playing_since is not None

    def _event(self, kind: str, **data) -> None:
        if self._recorder is None:
            return
        add_event = getattr(self._recorder, "add_event", None)
        if add_event is not None:
            add_event(kind, **data)

    def _session(self) -> str:
        return str(getattr(self._recorder, "session_id", "") or "?")

    # -- brain-facing API ----------------------------------------------

    async def arm(
        self,
        *,
        turn_id: int,
        gender: str,
        speech_stopped_at: float | None = None,
        dispatched_at: float | None = None,
    ) -> None:
        """A reply is now in flight for ``turn_id``.

        The wait is measured from the caller's end of speech when the latency
        tracker knows it (that is when the caller started waiting), else from
        dispatch; a deadline already in the past fires at once.
        """
        await self._cut("rearmed")
        now = time.monotonic()
        origin = dispatched_at if dispatched_at is not None else now
        if (
            speech_stopped_at is not None
            and 0.0 <= now - speech_stopped_at <= _MAX_SPEECH_STOP_AGE_S
        ):
            origin = min(speech_stopped_at, origin)
        armed = _ArmedTurn(
            turn_id=int(turn_id),
            gender=normalize_gender(gender),
            origin=origin,
            fire_at=max(origin + self._delay_s, now),
        )
        self._armed = armed
        self._task = self.create_task(self._run(armed))

    async def cancel(self, reason: str = "cancelled") -> None:
        await self._cut(reason)

    # -- internals -----------------------------------------------------

    async def _cut(self, reason: str) -> None:
        armed, self._armed = self._armed, None
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            await self.cancel_task(task)
        if armed is None:
            return
        if armed.playing_since is None:
            if reason == "tts_audio":
                self.fillers_unneeded += 1
            return
        if reason == "tts_audio":
            # The reply is about to speak: taper the breath over ONE more
            # chunk (20 ms) instead of stopping it mid-sample, so it ends as a
            # breath and not as a click. The reply's first frame follows
            # right behind it — an imperceptible cost, well under the lead.
            chunk_bytes = self._chunk_bytes()
            tail = _taper(armed.clip[armed.next_offset:armed.next_offset + chunk_bytes])
            if tail:
                await self.push_frame(
                    OutputAudioRawFrame(
                        audio=tail, sample_rate=self._sample_rate, num_channels=1,
                    )
                )
        played_ms = (time.monotonic() - armed.playing_since) * 1000.0
        self._event(
            "latency_filler_cut",
            turn=armed.turn_id, reason=reason,
            played_ms=round(played_ms, 1), clip_ms=round(armed.clip_ms, 1),
        )
        logger.info(
            "turn[%s] latency filler cut after %.0f ms (reason=%s turn=%d)",
            self._session(), played_ms, reason, armed.turn_id,
        )

    async def _run(self, armed: _ArmedTurn) -> None:
        try:
            delay = armed.fire_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._armed is not armed:
                return
            if self._bot_speaking:
                # Dispatched while the previous reply's tail was still
                # audible and it has not stopped yet: nothing to fill.
                self._event(
                    "latency_filler_skipped", turn=armed.turn_id, reason="bot_speaking",
                )
                return
            clip = self._library.clip(armed.gender, self._sample_rate)
            if not clip:
                self._event(
                    "latency_filler_skipped", turn=armed.turn_id, reason="no_clip",
                    gender=armed.gender,
                )
                return
            armed.clip_ms = len(clip) / (self._sample_rate * 2) * 1000.0
            armed.clip = clip
            armed.playing_since = time.monotonic()
            self.fillers_played += 1
            waited_ms = (armed.playing_since - armed.origin) * 1000.0
            self._event(
                "latency_filler_played",
                turn=armed.turn_id, gender=armed.gender,
                waited_ms=round(waited_ms, 1), clip_ms=round(armed.clip_ms, 1),
            )
            logger.info(
                "turn[%s] latency filler playing (turn=%d gender=%s waited=%.0fms clip=%.0fms)",
                self._session(), armed.turn_id, armed.gender, waited_ms, armed.clip_ms,
            )
            await self._stream(armed)
            self._event(
                "latency_filler_completed", turn=armed.turn_id,
                played_ms=round(armed.clip_ms, 1),
            )
        finally:
            if self._armed is armed:
                self._armed = None
                self._task = None

    def _chunk_bytes(self) -> int:
        return max(1, int(self._sample_rate * self._chunk_ms / 1000)) * 2

    async def _stream(self, armed: _ArmedTurn) -> None:
        """Push the clip as plain output audio at real-time pace.

        Chunk ``k`` is due ``(k - lead) × chunk`` after the start: the first
        chunks go out immediately (a small cushion against event-loop jitter)
        and everything after rides the wall clock, so the transport queue never
        holds more than the lead when reply audio arrives and cuts this off.
        The clip is zero-padded to whole 40 ms so the transport's own chunk
        buffer is left empty, not holding a stray tail.
        """
        rate = self._sample_rate
        chunk_bytes = self._chunk_bytes()
        clip = armed.clip
        remainder = len(clip) % (chunk_bytes * 2)
        if remainder:
            clip = clip + b"\x00" * (chunk_bytes * 2 - remainder)
            armed.clip = clip
        chunk_s = chunk_bytes / (rate * 2)
        started = time.monotonic()
        for index, offset in enumerate(range(0, len(clip), chunk_bytes)):
            due = started + max(0, index - self._lead_chunks) * chunk_s
            wait = due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            armed.next_offset = offset + chunk_bytes
            await self.push_frame(
                OutputAudioRawFrame(
                    audio=clip[offset:offset + chunk_bytes], sample_rate=rate, num_channels=1,
                )
            )

    # -- pipeline plumbing ---------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            rate = getattr(frame, "audio_out_sample_rate", 0)
            if rate:
                self._sample_rate = int(rate)
        elif isinstance(frame, TTSAudioRawFrame):
            # The reply is speaking. Cut BEFORE forwarding so not one more
            # breath chunk can be queued behind this frame.
            if self._task is not None:
                await self._cut("tts_audio")
        elif isinstance(frame, InterruptionFrame):
            await self._cut("interruption")
        elif isinstance(frame, UserStartedSpeakingFrame):
            await self._cut("caller_speech")
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            await self._cut("bot_speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._cut("pipeline_end")
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self._cut("cleanup")
        await super().cleanup()
