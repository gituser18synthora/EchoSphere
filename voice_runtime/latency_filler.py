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
from dataclasses import dataclass, field
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
    OutputTransportMessageFrame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from shared.audio.pcm import apply_fade_in, apply_fade_out, resample_pcm, wav_to_pcm
from voice_runtime.frames import AUDIO_FLUSH_MESSAGE_TYPE

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
        # Monotonic time the latency filler last started a rung (any kind):
        # the TTS router consults it so an in-reply inhale never follows a
        # pre-reply breath within a couple of seconds (two breaths back to
        # back around a short first sentence read as a stutter).
        self.last_played_at: float | None = None

    def note_played(self, when: float | None = None) -> None:
        self.last_played_at = time.monotonic() if when is None else when

    def recently_played(self, within_s: float) -> bool:
        return (
            self.last_played_at is not None
            and time.monotonic() - self.last_played_at < within_s
        )

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
# Ladder rungs, in order. ``breath`` is the pre-rendered gap breath; ``hmm``
# and ``wait`` are voiced cues in the bot's own voice (voice_runtime.voiced_cues).
RUNGS = ("breath", "hmm", "wait")
# Quiet before a held rung plays once the bot's previous reply falls silent:
# a breath right on the heels of speech sounds like a gasp.
_RESUME_GAP_S = 0.7
# After a spoken early acknowledgement ("जी, ठीक है…") the breath rung is
# skipped altogether — a person who just spoke does not then breathe audibly
# into the phone — and the voiced rungs wait at least this long after the
# acknowledgement ended (live call cv_06b9ead29d43: ack → 0.7 s → breath read
# as two fillers back to back).
_AFTER_ACK_GAP_S = 1.2
# Minimum quiet between two rungs (the next rung's schedule may be earlier):
# a breath and a "हम्म…" less than a second apart read as one stuttered noise.
_MIN_RUNG_GAP_S = 1.0


@dataclass
class _ArmedTurn:
    turn_id: int
    gender: str
    origin: float            # monotonic: when the caller stopped (or dispatch)
    fire_at: float
    language: str = ""
    engine: dict | None = None
    allow_spoken: bool = True
    rung: int = 0            # index into RUNGS of the rung being waited on
    rung_kind: str = "breath"
    playing_since: float | None = None
    clip_ms: float = 0.0
    # Streaming position: the clip being played and the byte offset of the
    # next chunk, so a cut can taper from exactly where playback stopped.
    clip: bytes = b""
    next_offset: int = 0
    rungs_played: list = field(default_factory=list)
    # Set while the bot is audibly speaking at a rung's deadline: the rung
    # waits for BotStoppedSpeakingFrame instead of being dropped.
    deferred: bool = False
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when the reply's synthesis has been requested (TTSStartedFrame):
    # its audio is a provider round-trip away, so no NEW rung may start — a
    # cue cut 200 ms in is a stray puff right before the reply.
    reply_imminent: bool = False
    # Re-armed after this turn's early acknowledgement: breath rung skipped.
    after_ack: bool = False


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

    Escalation ladder (``hmm_after_ms`` / ``spoken_after_ms``, measured like
    ``delay_ms`` from the caller's end of speech): when the breath has played
    and the reply is STILL not speaking, a short "हम्म…" in the bot's voice
    follows, then a spoken "एक सेकंड…" — each rung only if its clip is already
    rendered (``cue_library``), the spoken rung only when the brain allowed it
    for this turn (never on critical/serious content). A rung whose deadline
    falls while the bot is still audibly speaking (previous reply's tail) is
    deferred to the bot's next silence instead of being dropped; once the
    reply's synthesis has been requested (``TTSStartedFrame``) no new rung
    starts. Every rung start is noted on the clip library so the TTS router
    withholds an in-reply inhale right after a pre-reply breath.
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
        cue_library=None,
        hmm_after_ms: int | None = None,
        spoken_after_ms: int | None = None,
        emit_flush_marker: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        # Telephony transports packetize outbound PCM (200 ms) and flush a
        # partial packet only on BotStoppedSpeakingFrame, which plain audio
        # never produces: a completed clip's tail would sit there and play
        # glued to the next reply ("the breath repeats"). When set, a
        # transport message follows every completed clip (in order, via the
        # audio queue) telling the serializer to send that tail now.
        self._emit_flush_marker = bool(emit_flush_marker)
        self._delay_s = max(0.0, float(delay_ms) / 1000.0)
        self._library = library
        self._sample_rate = int(sample_rate)
        self._recorder = recorder
        self._chunk_ms = max(10, int(chunk_ms))
        self._lead_chunks = max(0, int(lead_chunks))
        # Voiced cue source (voice_runtime.voiced_cues.VoicedCueLibrary or a
        # stand-in with ``clip(engine, language, kind, rate)`` / ``warm``);
        # None → breath only, no ladder.
        self._cue_library = cue_library
        self._rung_delays_s: dict[str, float] = {"breath": self._delay_s}
        if cue_library is not None and hmm_after_ms is not None:
            self._rung_delays_s["hmm"] = max(self._delay_s, float(hmm_after_ms) / 1000.0)
        if cue_library is not None and spoken_after_ms is not None:
            self._rung_delays_s["wait"] = max(
                self._rung_delays_s.get("hmm", self._delay_s), float(spoken_after_ms) / 1000.0
            )
        # Optional ``callable(active: bool)`` told when a VOICED cue starts and
        # ends, so the caller audio gate can shield its echo the way it does
        # for backchannels (the breath is too quiet to matter).
        self.cue_window_hook = None
        self._armed: _ArmedTurn | None = None
        self._task: asyncio.Task | None = None
        self._bot_speaking = False
        self.fillers_played = 0
        # Rungs played per kind across the call.
        self.rungs_played: dict[str, int] = {kind: 0 for kind in RUNGS}
        # Armed turns whose reply audio arrived before the first rung — the
        # common case, and the number that says whether the delay is tuned.
        self.fillers_unneeded = 0

    # -- state ---------------------------------------------------------

    @property
    def delay_ms(self) -> int:
        return int(round(self._delay_s * 1000.0))

    @property
    def ladder_enabled(self) -> bool:
        return len(self._rung_delays_s) > 1

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
        language: str = "",
        engine: dict | None = None,
        allow_spoken: bool = True,
        resume: bool = False,
    ) -> None:
        """A reply is now in flight for ``turn_id``.

        The wait is measured from the caller's end of speech when the latency
        tracker knows it (that is when the caller started waiting), else from
        dispatch; a deadline already in the past fires at once. ``resume``
        marks a re-arm after the turn's early acknowledgement finished
        speaking: the schedule keeps the caller's true wait as its origin, the
        breath rung is skipped (the bot just spoke) and the voiced rungs are
        held at least ``_AFTER_ACK_GAP_S`` from now. Without a ladder there is
        nothing left to play, so a resume then arms nothing.
        """
        await self._cut("rearmed")
        if resume and not self.ladder_enabled:
            return
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
            language=language or "",
            engine=dict(engine) if engine else None,
            allow_spoken=bool(allow_spoken),
            after_ack=bool(resume),
        )
        self._armed = armed
        if self._cue_library is not None and self.ladder_enabled:
            try:
                # Renders (once per voice) in the background so the cues are
                # ready by the time a slow reply needs them.
                self._cue_library.warm(armed.engine, armed.language)
            except Exception:  # noqa: BLE001 — decoration must never break a turn
                logger.debug("latency-filler: cue warm-up failed", exc_info=True)
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
            if reason == "tts_audio" and not armed.rungs_played:
                self.fillers_unneeded += 1
            return
        if reason == "tts_audio":
            # The reply is about to speak: taper the clip over ONE more
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
        self._end_cue_window(armed)
        played_ms = (time.monotonic() - armed.playing_since) * 1000.0
        self._event(
            "latency_filler_cut",
            turn=armed.turn_id, reason=reason, rung=armed.rung_kind,
            played_ms=round(played_ms, 1), clip_ms=round(armed.clip_ms, 1),
        )
        logger.info(
            "turn[%s] latency filler %s cut after %.0f ms (reason=%s turn=%d)",
            self._session(), armed.rung_kind, played_ms, reason, armed.turn_id,
        )

    def _rung_clip(self, armed: _ArmedTurn, kind: str) -> bytes:
        if kind == "breath":
            return self._library.clip(armed.gender, self._sample_rate)
        if self._cue_library is None:
            return b""
        return self._cue_library.clip(armed.engine, armed.language, kind, self._sample_rate)

    def _begin_cue_window(self, armed: _ArmedTurn) -> None:
        if armed.rung_kind != "breath" and self.cue_window_hook is not None:
            try:
                self.cue_window_hook(True)
            except Exception:  # noqa: BLE001
                logger.debug("latency-filler: cue window hook failed", exc_info=True)

    def _end_cue_window(self, armed: _ArmedTurn) -> None:
        if armed.rung_kind != "breath" and self.cue_window_hook is not None:
            try:
                self.cue_window_hook(False)
            except Exception:  # noqa: BLE001
                logger.debug("latency-filler: cue window hook failed", exc_info=True)

    async def _run(self, armed: _ArmedTurn) -> None:
        try:
            for index, kind in enumerate(RUNGS):
                if kind not in self._rung_delays_s:
                    return
                armed.rung, armed.rung_kind = index, kind
                if kind == "breath" and armed.after_ack:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="after_early_ack",
                    )
                    continue
                if index > 0:
                    armed.fire_at = max(
                        armed.origin + self._rung_delays_s[kind],
                        time.monotonic() + (
                            _AFTER_ACK_GAP_S if armed.after_ack and not armed.rungs_played
                            else _MIN_RUNG_GAP_S
                        ),
                    )
                if kind == "wait" and not armed.allow_spoken:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="spoken_withheld",
                    )
                    return
                if not await self._wait_for_rung(armed):
                    return
                if armed.reply_imminent:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="reply_imminent",
                    )
                    return
                clip = self._rung_clip(armed, kind)
                if not clip:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="no_clip", gender=armed.gender,
                    )
                    continue
                armed.clip_ms = len(clip) / (self._sample_rate * 2) * 1000.0
                armed.clip = clip
                armed.next_offset = 0
                armed.playing_since = time.monotonic()
                note_played = getattr(self._library, "note_played", None)
                if note_played is not None:
                    note_played(armed.playing_since)
                self.fillers_played += 1
                self.rungs_played[kind] = self.rungs_played.get(kind, 0) + 1
                waited_ms = (armed.playing_since - armed.origin) * 1000.0
                self._event(
                    "latency_filler_played",
                    turn=armed.turn_id, gender=armed.gender, rung=kind,
                    waited_ms=round(waited_ms, 1), clip_ms=round(armed.clip_ms, 1),
                )
                logger.info(
                    "turn[%s] latency filler %s playing (turn=%d gender=%s waited=%.0fms clip=%.0fms)",
                    self._session(), kind, armed.turn_id, armed.gender, waited_ms, armed.clip_ms,
                )
                self._begin_cue_window(armed)
                try:
                    await self._stream(armed)
                finally:
                    self._end_cue_window(armed)
                if self._emit_flush_marker:
                    await self.push_frame(
                        OutputTransportMessageFrame(message={"type": AUDIO_FLUSH_MESSAGE_TYPE})
                    )
                self._event(
                    "latency_filler_completed", turn=armed.turn_id, rung=kind,
                    played_ms=round(armed.clip_ms, 1),
                )
                armed.rungs_played.append(kind)
                armed.playing_since = None
                armed.clip = b""
        finally:
            if self._armed is armed:
                self._armed = None
                self._task = None

    async def _wait_for_rung(self, armed: _ArmedTurn) -> bool:
        """Sleep until the rung's deadline; while the bot is audibly speaking
        at that moment, wait for its silence plus a short gap instead. False
        when the turn was disarmed meanwhile."""
        while True:
            delay = armed.fire_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._armed is not armed:
                return False
            if not self._bot_speaking or armed.reply_imminent:
                return True
            # Dispatched while the previous reply's tail was still audible:
            # nothing to fill yet. Hold the rung until the bot falls silent.
            if not armed.deferred:
                self._event(
                    "latency_filler_deferred", turn=armed.turn_id, rung=armed.rung_kind,
                    reason="bot_speaking",
                )
            armed.deferred = True
            armed.resume.clear()
            await armed.resume.wait()
            if self._armed is not armed:
                return False
            armed.deferred = False
            armed.fire_at = time.monotonic() + _RESUME_GAP_S

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
        elif isinstance(frame, TTSStartedFrame):
            # Synthesis of the reply was just requested: its first audio is
            # one provider round-trip (~200-400 ms) away. A rung already
            # playing runs on until that audio cuts it; a rung not yet
            # started stays unstarted — a "हम्म…" chopped after 200 ms is a
            # grunt, and a breath chopped that early a puff, right before the
            # reply. Nothing is cut here: the reply may still stall.
            if self._armed is not None:
                self._armed.reply_imminent = True
                if self._armed.playing_since is None and self._armed.deferred:
                    self._armed.resume.set()
        elif isinstance(frame, InterruptionFrame):
            await self._cut("interruption")
        elif isinstance(frame, UserStartedSpeakingFrame):
            await self._cut("caller_speech")
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            await self._cut("bot_speaking")
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            armed = self._armed
            if armed is not None and armed.deferred and armed.playing_since is None:
                # The previous reply's tail is over; the held rung may play
                # after a short gap (see _wait_for_rung).
                armed.resume.set()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._cut("pipeline_end")
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self._cut("cleanup")
        await super().cleanup()
