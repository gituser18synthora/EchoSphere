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

* **Response priority.** The clip streams in owned 20 ms chunks at real-time
  pace without look-ahead. Playable response audio cuts breaths immediately.
  Adaptive voiced cues finish with a short silence before response playback;
  caller interruptions always retire their audio and any waiting response.
  Remote telephony packets already sent cannot be selectively recalled.
* **Invisible to turn bookkeeping.** Chunks subclass ``OutputAudioRawFrame``
  — pipecat's output transport flips bot-speaking state only for
  ``TTSAudioRawFrame`` / ``SpeechOutputAudioRawFrame`` — so no
  ``BotStartedSpeakingFrame`` fires: the brain's latency measurement, the
  barge-in/merge discriminator, the word-confirmed barge-in gate and the
  audio gate's echo guard all still see a quiet bot. A caller who talks over
  a breath simply opens a turn, exactly as they would over silence.
* **One opportunity per dispatched turn.** Armed by the brain at dispatch,
  disarmed by every cancellation path (barge-in, late merge, hang-up,
  teardown) and by the first reply audio. Nothing is spoken, so history,
  turn records and the client transcript never see it.
* **Two independent families on one schedule.** The BREATH (``breath_enabled``,
  config ``breathing``/``latency_fillers``) and the WORDS — the dispatch-time
  acknowledgement and the voiced/spoken ladder cues (config ``filler_words``)
  — share the deadline machinery but never imply each other: with the breath
  off the acknowledgement and the cues still play at their own times; with the
  words off only the breath plays. Neither family delays reply audio.
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
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from shared.audio.pcm import apply_fade_in, apply_fade_out, resample_pcm, wav_to_pcm
from voice_runtime.audio_gate import frame_dbfs
from shared.orchestration.naturalness import (
    FILLER_SOUND_KINDS,
    FILLER_SOUND_LABELS,
    selection_ids,
)
from voice_runtime.frames import (
    AUDIO_FLUSH_MESSAGE_TYPE, FillerAudioOwner, FillerAudioRawFrame, FillerClearFrame,
)

logger = logging.getLogger(__name__)

GENDERS = ("male", "female", "neutral")
# Clip kinds (shared.orchestration.naturalness.FILLER_SOUND_KINDS): the
# pre-reply ``breath`` (front-loaded, trailing off into the wait), the
# ``inhale`` (short, RISING into the sentence that follows — the shape a
# person makes right before speaking; also the in-reply sentence breath), the
# ``exhale`` (quick onset, long soft tail — a settling breath) and the
# composite ``inhale_exhale`` (a full quiet breath cycle). The operator
# chooses which kind covers the pre-reply gap (``latency_filler_kind``).
KINDS = FILLER_SOUND_KINDS


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
# Exhale: a quick onset and a long, soft tail (peak at ~1/4), a shade lower
# and darker than the inhale — air released, not drawn.
_EXHALE_PROFILES: dict[str, _BreathProfile] = {
    "male": _BreathProfile(0.62, 150.0, 550.0, 2200.0, 1.0, 3.2, -32.0),
    "female": _BreathProfile(0.55, 280.0, 1000.0, 3200.0, 1.0, 3.0, -33.0),
    "neutral": _BreathProfile(0.58, 220.0, 780.0, 2700.0, 1.0, 3.1, -32.5),
}
_PROFILES_BY_KIND: dict[str, dict[str, _BreathProfile]] = {
    "breath": _PROFILES,
    "inhale": _INHALE_PROFILES,
    "exhale": _EXHALE_PROFILES,
}
# The composite inhale-exhale: inhale, a short hold, exhale.
_INHALE_EXHALE_HOLD_S = 0.06
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
    if sample_rate <= 0:
        return b""
    if kind == "inhale_exhale":
        hold = b"\x00\x00" * int(sample_rate * _INHALE_EXHALE_HOLD_S)
        return (
            synthesize_breath(gender, sample_rate, variant=variant, kind="inhale")
            + hold
            + synthesize_breath(gender, sample_rate, variant=variant, kind="exhale")
        )
    profile = _PROFILES_BY_KIND[kind][gender]
    variant = int(variant) % _VARIANTS_PER_GENDER
    rng = np.random.default_rng(
        _SEEDS[gender] + 17 * variant + {"breath": 0, "inhale": 5000, "exhale": 9000}[kind]
    )
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
    """``inhale_female.wav`` → ``inhale``, ``exhale_male_2.wav`` → ``exhale``,
    ``inhale_exhale_female.wav`` (or ``inhaleexhale``) → ``inhale_exhale``;
    anything else is the pre-reply ``breath``."""
    tokens = set(re.split(r"[^a-z]+", path.stem.lower()))
    if "inhaleexhale" in tokens or ("inhale" in tokens and "exhale" in tokens):
        return "inhale_exhale"
    if "exhale" in tokens:
        return "exhale"
    return "inhale" if "inhale" in tokens else "breath"


def _pretty_label(stem: str) -> str:
    return re.sub(r"[_\-]+", " ", stem).strip().capitalize() or stem


class _FileClip:
    source = "recording"

    def __init__(self, path: Path, *, clip_id: str | None = None) -> None:
        self.path = path
        self.gender = gender_from_filename(path) or "neutral"
        self.kind = kind_from_filename(path)
        # Stable id the configuration stores (``filler_audio_selection``).
        self.clip_id = clip_id or f"file:{path.name}"
        self.label = _pretty_label(path.stem)

    def describe(self) -> str:
        return self.clip_id

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
    source = "synthesized"

    def __init__(self, gender: str, variant: int, kind: str = "breath") -> None:
        self.gender = gender
        self.variant = variant
        self.kind = kind
        self.clip_id = f"synth:{kind}:{gender}:{variant + 1}"
        self.label = f"Synthesized {variant + 1}"

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
    Recordings in ``directory/optional`` appear in the catalog and play only
    when explicitly selected by a bot; installing one cannot change defaults.
    Neutral voices never borrow gendered recordings. The directory is scanned
    lazily on first use, so a missing or unreadable directory costs nothing
    and simply means "synthesized".
    """

    def __init__(self, directory: str | Path | None = None, *, synthesize: bool = True) -> None:
        self._session_local = False
        self._cache: FillerClipLibrary | None = None
        self._directory = Path(directory) if directory else None
        self._synthesize = synthesize
        self._sources: dict[tuple[str, str], list] | None = None
        self._optional_sources: dict[tuple[str, str], list] | None = None
        # Rendered PCM per (clip id, sample rate): a clip renders once however
        # many selections include it.
        self._rendered: dict[tuple[str, int], bytes] = {}
        # Rotation cursor per (kind, gender, selected ids). Runtime consumers
        # use a new_session() view so these picks belong to one call only.
        self._cursor: dict[tuple, int] = {}
        self._selection_warned: set[tuple] = set()
        # The id of the clip most recently handed out (telemetry).
        self.last_clip_id: str | None = None
        # Monotonic time the latency filler last started a rung (any kind):
        # the TTS router consults it so an in-reply inhale never follows a
        # pre-reply breath within a couple of seconds (two breaths back to
        # back around a short first sentence read as a stutter).
        self.last_played_at: float | None = None

    def new_session(self) -> FillerClipLibrary:
        """Fresh call history over the same scanned/rendered asset cache.

        The cache never references its session views. It can keep reusable
        PCM and failed-asset results without retaining completed calls.
        """
        session = FillerClipLibrary(self._directory, synthesize=self._synthesize)
        session._cache = self._cache or self
        session._session_local = True
        session._rendered = session._cache._rendered
        session._selection_warned = session._cache._selection_warned
        return session

    def clear_history(self) -> None:
        self._cursor.clear()
        self.last_clip_id = None
        self.last_played_at = None

    def note_played(self, when: float | None = None) -> None:
        self.last_played_at = time.monotonic() if when is None else when

    def recently_played(self, within_s: float) -> bool:
        return (
            self.last_played_at is not None
            and time.monotonic() - self.last_played_at < within_s
        )

    def _scan(self, *, optional: bool = False) -> dict[tuple[str, str], list]:
        sources: dict[tuple[str, str], list] = {
            (kind, gender): [] for kind in KINDS for gender in GENDERS
        }
        if self._directory is not None:
            directory = self._directory / "optional" if optional else self._directory
            try:
                files = sorted(
                    path for path in directory.iterdir()
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
                clip_id = f"file:optional:{path.name}" if optional else f"file:{path.name}"
                sources[(kind_from_filename(path), gender)].append(_FileClip(path, clip_id=clip_id))
        if self._synthesize and not optional:
            for key in sources:
                if not sources[key]:
                    kind, gender = key
                    sources[key] = [
                        _SynthClip(gender, variant, kind)
                        for variant in range(_VARIANTS_PER_GENDER)
                    ]
        return sources

    def sources_for(self, gender: str, kind: str = "breath") -> list:
        if self._cache is not None:
            return self._cache.sources_for(gender, kind)
        if self._sources is None:
            self._sources = self._scan()
        kind = kind if kind in KINDS else "breath"
        return self._sources[(kind, normalize_gender(gender))]

    def _available_sources_for(self, gender: str, kind: str) -> list:
        """Default rotation plus recordings that require an explicit selection."""
        if self._cache is not None:
            return self._cache._available_sources_for(gender, kind)
        kind = kind if kind in KINDS else "breath"
        gender = normalize_gender(gender)
        if self._optional_sources is None:
            self._optional_sources = self._scan(optional=True)
        return self.sources_for(gender, kind) + self._optional_sources[(kind, gender)]

    def describe(self, kind: str = "breath") -> dict[str, list[str]]:
        return {
            gender: [s.describe() for s in self.sources_for(gender, kind)]
            for gender in GENDERS
        }

    def find(self, clip_id: str) -> object | None:
        """The clip source with this id (any kind/gender), or None."""
        for kind in KINDS:
            for gender in GENDERS:
                for source in self._available_sources_for(gender, kind):
                    if getattr(source, "clip_id", None) == clip_id:
                        return source
        return None

    def catalog(self, kind: str, gender: str, sample_rate: int = 16000) -> list[dict]:
        """Every clip the runtime could play for ``kind``/``gender`` — id,
        label, source (recording | synthesized) and duration — in the order
        of the default rotation followed by opt-in recordings. Clips that
        fail to render are left out (the runtime skips them too)."""
        out: list[dict] = []
        for source in self._available_sources_for(gender, kind):
            pcm = self._render_source(source, int(sample_rate))
            if not pcm:
                continue
            out.append({
                "id": source.clip_id,
                "label": source.label,
                "source": source.source,
                "kind": kind,
                "gender": normalize_gender(gender),
                "durationMs": round(len(pcm) / (int(sample_rate) * 2) * 1000.0),
                **({"requiresSelection": True} if source.clip_id.startswith("file:optional:") else {}),
            })
        return out

    def render_clip(self, clip_id: str, sample_rate: int) -> bytes:
        """The exact PCM the runtime plays for ``clip_id`` at ``sample_rate``
        (preview); b"" when the id is unknown or the file does not render."""
        source = self.find(clip_id)
        if source is None or sample_rate <= 0:
            return b""
        return self._render_source(source, int(sample_rate))

    def selected_sources(self, kind: str, gender: str, selection: dict | None) -> list:
        """The sources a bot's ``selection`` ({primary, alternates}) resolves
        to for ``kind``/``gender``, primary first; the gender's default
        rotation when nothing is selected or nothing selected resolves (an id
        of another gender or kind is never honoured — a male voice cannot be
        handed a female breath by configuration)."""
        kind = kind if kind in KINDS else "breath"
        gender = normalize_gender(gender)
        sources = self.sources_for(gender, kind)
        ids = selection_ids(selection)
        if not ids:
            return sources
        by_id = {
            getattr(source, "clip_id", None): source
            for source in self._available_sources_for(gender, kind)
        }
        chosen = [by_id[i] for i in ids if i in by_id]
        if chosen:
            return chosen
        marker = (kind, gender, tuple(ids))
        if marker not in self._selection_warned:
            self._selection_warned.add(marker)
            logger.warning(
                "latency-filler: none of the selected %s %s clips %s exist; "
                "using default rotation", kind, gender, ids,
            )
        return sources

    def clip(
        self, gender: str, sample_rate: int, *,
        kind: str = "breath", max_ms: int | None = None, gain_db: float = 0.0,
        selection: dict | None = None,
    ) -> bytes:
        """The next ``kind`` clip for ``gender`` (rotating), or b"" when none
        renders.

        With a ``selection`` ({primary, alternates}) only those clips rotate,
        the primary first in a call; without one the gender's default clips
        rotate. Operator files that fail to render (not a PCM WAV,
        unreadable) are skipped; when none of a gender's files renders, the
        synthesized variants take over so a bad upload degrades to the
        default, never to dead air where a breath was configured. ``max_ms``
        trims the clip (with a short fade-out) and ``gain_db`` lowers it.
        """
        gender = normalize_gender(gender)
        kind = kind if kind in KINDS else "breath"
        all_sources = self.sources_for(gender, kind)
        sources = self.selected_sources(kind, gender, selection)
        if sample_rate <= 0:
            return b""
        slot: tuple = (kind, gender)
        if sources is not all_sources:
            slot = (kind, gender, tuple(getattr(x, "clip_id", "") for x in sources))
        clip = self._next_rendered(slot, sources, int(sample_rate))
        if not clip and sources is not all_sources:
            # A selected file went bad: fall back to everything of that gender.
            clip = self._next_rendered((kind, gender), all_sources, int(sample_rate))
        sources = all_sources
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

    def _render_source(self, source, sample_rate: int) -> bytes:
        if self._cache is not None:
            return self._cache._render_source(source, sample_rate)
        key = (getattr(source, "clip_id", None) or source.describe(), sample_rate)
        clip = self._rendered.get(key)
        if clip is None:
            clip = source.render(sample_rate)
            self._rendered[key] = clip
        return clip

    def _next_rendered(self, slot: tuple, sources: list, sample_rate: int) -> bytes:
        if not sources:
            return b""
        start = self._cursor.get(slot, 0)
        for step in range(len(sources)):
            index = (start + step) % len(sources)
            clip = self._render_source(sources[index], sample_rate)
            if clip:
                self._cursor[slot] = index + 1
                self.last_clip_id = getattr(sources[index], "clip_id", None)
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
# Voiced cues/acknowledgements are pre-rendered at a fixed level (about
# -26 dBFS RMS). Live replies measured -18..-19 dBFS on telephony and about
# -26 dBFS in the browser: a fixed level is 6-10 dB too quiet on the phone
# and about right in the browser. The processor therefore tracks the reply
# audio it forwards and places each voiced clip just under THAT level.
_CUE_BELOW_REPLY_DB = 3.0
_REPLY_LEVEL_EMA = 0.15
_REPLY_LEVEL_MIN_DBFS = -50.0  # frames below this are silence, not level
# Minimum quiet between two rungs (the next rung's schedule may be earlier):
# a breath and a "Hmm…" less than a second apart read as one stuttered noise.
_MIN_RUNG_GAP_S = 1.0


@dataclass
class _VoicedHandoff:
    owner: FillerAudioOwner
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    failed: bool = False


@dataclass
class _ArmedTurn:
    turn_id: int
    gender: str
    origin: float            # monotonic: when the caller stopped (or dispatch)
    fire_at: float
    owner: FillerAudioOwner | None = None
    language: str = ""
    engine: dict | None = None
    allow_spoken: bool = True
    rung: int = 0            # index into RUNGS of the rung being waited on
    rung_kind: str = "breath"
    playing_since: float | None = None
    clip_ms: float = 0.0
    # Streaming position in the clip currently being generated.
    clip: bytes = b""
    next_offset: int = 0
    rungs_played: list = field(default_factory=list)
    # Set while the bot is audibly speaking at a rung's deadline: the rung
    # waits for BotStoppedSpeakingFrame instead of being dropped.
    deferred: bool = False
    resume: asyncio.Event = field(default_factory=asyncio.Event)
    # Re-armed after this turn's early acknowledgement: breath rung skipped.
    after_ack: bool = False
    # Which pre-rendered sound the first rung plays and which clips of it
    # (bot configuration); None → every clip of the gender rotates.
    filler_kind: str = "breath"
    filler_selection: dict | None = None
    # The planner's per-turn cue preference (best first) and whether a
    # voiced "hmm" cue may play at all this turn (False → breath only, the
    # spoken "wait" rung still follows ``allow_spoken``).
    cue_selection: dict | None = None
    allow_voiced: bool = True
    # Optional dispatch-planned acknowledgement. It may replace the first
    # breath only at the same deadline, and only when its cached PCM is ready.
    acknowledgement: dict | None = None
    playing_acknowledgement: bool = False
    cue_after_s: float | None = None
    reply_pending: bool = False
    cue_window_open: bool = False


class LatencyFillerProcessor(FrameProcessor):
    """Sits between the TTS service and the output transport.

    The brain arms it per dispatched turn (``arm``) and disarms it on every
    cancellation (``cancel``); reply audio, interruptions, caller speech and
    bot-speaking frames passing through disarm/cut it on their own. The
    processor leaves response PCM unchanged. With voiced-cue completion on,
    only a started word and its ordered silence can hold the first reply PCM.

    Escalation ladder (``hmm_after_ms`` / ``spoken_after_ms``, measured like
    ``delay_ms`` from the caller's end of speech): when the breath has played
    and the reply is STILL not speaking, a short "Hmm…" in the bot's voice
    follows, then a spoken "एक सेकंड…" — each rung only if its clip is already
    rendered (``cue_library``), the spoken rung only when the brain allowed it
    for this turn (never on critical/serious content). A rung whose deadline
    falls while the bot is still audibly speaking (previous reply's tail) is
    deferred to the bot's next silence instead of being dropped. Synthesis
    start alone does not suppress a rung: playable PCM retires breaths and
    stops further rungs. Started voiced cues can finish before the reply.
    Every rung start is noted on the clip library so the TTS router
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
        voiced_cue_gap_ms: int = 0,
        breath_gain_db: float = 0.0,
        breath_enabled: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        # The nonverbal breath rung (config ``breathing`` × ``latency_fillers``).
        # False → the first deadline may only play a planned acknowledgement;
        # the voiced ladder keeps its own schedule.
        self._breath_enabled = bool(breath_enabled)
        # Retain completion markers for telephony, now tagged so they cannot
        # flush a speech packet after owned filler has been cleared.
        self._emit_flush_marker = bool(emit_flush_marker)
        # Keep ownership after producer completion: its last chunks may
        # still be in a transport/browser queue when reply audio arrives.
        self._output_owners: list[FillerAudioOwner] = []
        self._voiced_cue_gap_ms = max(0, int(voiced_cue_gap_ms))
        self._voiced_handoff: _VoicedHandoff | None = None
        self._reply_handoff_owner: FillerAudioOwner | None = None
        self._delay_s = max(0.0, float(delay_ms) / 1000.0)
        self._library = (
            library.new_session()
            if hasattr(library, "new_session") and not getattr(library, "_session_local", False)
            else library
        )
        self._sample_rate = int(sample_rate)
        # Running RMS level (dBFS) of the reply audio this processor forwards,
        # and the last voiced clip's level-matching telemetry.
        self._reply_level_dbfs: float | None = None
        self._last_cue_level: dict | None = None
        self._breath_gain_db = float(breath_gain_db)
        self._recorder = recorder
        self._chunk_ms = max(10, int(chunk_ms))
        self._lead_chunks = max(0, int(lead_chunks))
        # Voiced cue source (voice_runtime.voiced_cues.VoicedCueLibrary or a
        # stand-in with ``clip(engine, language, kind, rate)`` / ``warm``);
        # None → breath only, no ladder.
        self._cue_library = (
            cue_library.new_session()
            if hasattr(cue_library, "new_session") and not getattr(cue_library, "_session_local", False)
            else cue_library
        )
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
        self.acknowledgement_hook = None
        # Optional ``callable(turn_id: int, cue_id: str | None)`` told when a
        # voiced "hmm" cue starts, so the planner never repeats that word on
        # the next turn (as an acknowledgement or as a cue).
        self.cue_played_hook = None
        self._armed: _ArmedTurn | None = None
        self._task: asyncio.Task | None = None
        self._bot_speaking = False
        self.fillers_played = 0
        # Rungs played per kind across the call.
        self.rungs_played: dict[str, int] = {kind: 0 for kind in RUNGS}
        # Armed turns whose reply audio arrived before the first rung — the
        # common case, and the number that says whether the delay is tuned.
        self.fillers_unneeded = 0
        # The voiced cue most recently played in this call (the planner keeps
        # it from leading the next wait's preference).
        self.last_cue_played: str | None = None

    # -- state ---------------------------------------------------------

    @property
    def delay_ms(self) -> int:
        return int(round(self._delay_s * 1000.0))

    @property
    def ladder_enabled(self) -> bool:
        return len(self._rung_delays_s) > 1

    @property
    def breath_enabled(self) -> bool:
        return self._breath_enabled

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
        filler_kind: str = "breath",
        filler_selection: dict | None = None,
        cue_selection: dict | None = None,
        allow_voiced: bool = True,
        acknowledgement: dict | None = None,
        cue_after_ms: int | None = None,
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
            owner=FillerAudioOwner(turn_id=int(turn_id)),
            language=language or "",
            engine=dict(engine) if engine else None,
            allow_spoken=bool(allow_spoken),
            after_ack=bool(resume),
            filler_kind=filler_kind if filler_kind in KINDS else "breath",
            filler_selection=dict(filler_selection) if filler_selection else None,
            cue_selection=dict(cue_selection) if cue_selection else None,
            allow_voiced=bool(allow_voiced),
            acknowledgement=dict(acknowledgement) if acknowledgement else None,
            cue_after_s=(
                min(2500, max(1500, cue_after_ms)) / 1000.0
                if cue_after_ms is not None and "hmm" in self._rung_delays_s and not resume
                else None
            ),
        )
        self._armed = armed
        if armed.cue_after_s is not None:
            armed.fire_at = max(origin + armed.cue_after_s, now)
        if armed.acknowledgement and self._cue_library is not None:
            # Prime the existing cache without awaiting a render or touching
            # the answer's TTS queue. Playback remains behind the deadline.
            self._acknowledgement_clip(armed)
        if self._cue_library is not None and self.ladder_enabled:
            try:
                # Renders (once per voice) in the background so the cues are
                # ready by the time a slow reply needs them.
                if armed.cue_selection is not None:
                    self._cue_library.warm(
                        armed.engine, armed.language, selection=armed.cue_selection
                    )
                else:
                    self._cue_library.warm(armed.engine, armed.language)
            except Exception:  # noqa: BLE001 — decoration must never break a turn
                logger.debug("latency-filler: cue warm-up failed", exc_info=True)
        self._task = self.create_task(self._run(armed))

    async def cancel(self, reason: str = "cancelled") -> None:
        await self._cut(reason)

    # -- internals -----------------------------------------------------

    def _retire_output_owners(self, preserve: FillerAudioOwner | None = None) -> None:
        # Shared identities also invalidate frames already handed downstream.
        # Retire before any await, including Pipecat's interruption handling.
        if self._armed is not None and self._armed.owner is not preserve:
            self._armed.owner.cancel()
        for owner in self._output_owners:
            if owner is not preserve:
                owner.cancel()
        if self._voiced_handoff is not None and self._voiced_handoff.owner is not preserve:
            self._voiced_handoff.owner.cancel()
        if self._reply_handoff_owner is not None and self._reply_handoff_owner is not preserve:
            self._reply_handoff_owner.cancel()

    async def _cut(self, reason: str, *, preserve: FillerAudioOwner | None = None) -> None:
        self._retire_output_owners(preserve)
        self._voiced_handoff = None
        owners, self._output_owners = self._output_owners, []
        armed, self._armed = self._armed, None
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            # Consume the producer's cancellation, but propagate cancellation
            # of THIS processing task. Pipecat's cancel_task helper swallows
            # both, letting a handoff interruption wait for its 1 s timeout
            # and potentially forward the interrupted response afterward.
            await asyncio.gather(task, return_exceptions=True)
        # Production may already have finished while playback is buffered.
        # Always clear those owners, even without an active task/armed turn.
        for owner in owners:
            if owner is not preserve:
                await self.push_frame(FillerClearFrame(owner))
        if armed is None:
            return
        if armed.playing_since is None:
            if reason == "tts_audio" and not armed.rungs_played:
                self.fillers_unneeded += 1
            return
        if armed.owner is preserve:
            return
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

    # -- reply level matching ------------------------------------------

    def _observe_reply_level(self, audio: bytes) -> None:
        """Fold one forwarded reply frame into the running reply level."""
        level = frame_dbfs(audio)
        if level < _REPLY_LEVEL_MIN_DBFS:
            return
        if self._reply_level_dbfs is None:
            self._reply_level_dbfs = level
        else:
            self._reply_level_dbfs += (level - self._reply_level_dbfs) * _REPLY_LEVEL_EMA

    def _cue_level_kwargs(self) -> dict:
        """``target_rms_dbfs`` for a voiced clip: just under the reply's
        measured level. Empty before any reply audio was heard, or when the
        cue library cannot level-match (stubs, older libraries return the
        rendered clip as is)."""
        if self._reply_level_dbfs is None:
            return {}
        if not getattr(self._cue_library, "supports_level_matching", False):
            return {}
        return {"target_rms_dbfs": self._reply_level_dbfs - _CUE_BELOW_REPLY_DB}

    def _note_cue_level(self, pcm: bytes, level_kwargs: dict) -> bytes:
        self._last_cue_level = None
        if pcm and level_kwargs:
            level = frame_dbfs(pcm)
            self._last_cue_level = {
                "reply_level_dbfs": round(self._reply_level_dbfs, 1),
                "cue_level_dbfs": round(level, 1),
                "cue_target_dbfs": round(level_kwargs["target_rms_dbfs"], 1),
            }
        return pcm

    def _rung_clip(self, armed: _ArmedTurn, kind: str) -> bytes:
        if kind == "breath":
            ack = self._acknowledgement_clip(armed)
            if ack:
                armed.playing_acknowledgement = True
                return ack
            if not self._breath_enabled:
                # Breathing is off for this bot: the first deadline had only
                # the acknowledgement to offer, and it is not ready.
                return b""
            gain = {"gain_db": self._breath_gain_db} if self._breath_gain_db else {}
            if armed.filler_kind == "breath" and armed.filler_selection is None:
                return self._library.clip(armed.gender, self._sample_rate, **gain)
            return self._library.clip(
                armed.gender, self._sample_rate,
                kind=armed.filler_kind, selection=armed.filler_selection,
                **gain,
            )
        if self._cue_library is None:
            return b""
        level = self._cue_level_kwargs()
        if kind == "hmm" and armed.cue_selection is not None:
            clip = self._cue_library.clip(
                armed.engine, armed.language, kind, self._sample_rate,
                selection=armed.cue_selection, **level,
            )
        else:
            clip = self._cue_library.clip(
                armed.engine, armed.language, kind, self._sample_rate, **level,
            )
        return self._note_cue_level(clip, level)

    def _acknowledgement_clip(self, armed: _ArmedTurn) -> bytes:
        get_clip = getattr(self._cue_library, "acknowledgement_clip", None)
        if not armed.acknowledgement or get_clip is None:
            return b""
        level = self._cue_level_kwargs()
        try:
            clip = get_clip(
                armed.engine, armed.language, armed.acknowledgement["text"],
                self._sample_rate, **level,
            )
        except Exception:  # Decoration failure must not hold up the reply.
            logger.debug("latency-filler: acknowledgement unavailable", exc_info=True)
            return b""
        return self._note_cue_level(clip, level)

    def _rung_sound(self, armed: _ArmedTurn, kind: str) -> dict:
        """Telemetry: which sound/clip a rung actually played."""
        if kind == "breath":
            if armed.playing_acknowledgement:
                return {"sound": "acknowledgement", **(self._last_cue_level or {})}
            return {
                "sound": armed.filler_kind,
                "clip": getattr(self._library, "last_clip_id", None),
            }
        return {
            "sound": kind, "cue": getattr(self._cue_library, "last_cue_id", None),
            **(self._last_cue_level or {}),
        }

    def _begin_cue_window(self, armed: _ArmedTurn) -> None:
        if (armed.rung_kind != "breath" or armed.playing_acknowledgement) and self.cue_window_hook is not None:
            try:
                self.cue_window_hook(True)
                armed.cue_window_open = True
            except Exception:  # noqa: BLE001
                logger.debug("latency-filler: cue window hook failed", exc_info=True)

    def _end_cue_window(self, armed: _ArmedTurn) -> None:
        if armed.cue_window_open and self.cue_window_hook is not None:
            armed.cue_window_open = False
            try:
                self.cue_window_hook(False)
            except Exception:  # noqa: BLE001
                logger.debug("latency-filler: cue window hook failed", exc_info=True)

    async def _run(self, armed: _ArmedTurn) -> None:
        try:
            # Adaptive turns have ONE initial opportunity: a ready contextual
            # acknowledgement/cue, or a breath if no eligible cue is ready.
            # Never queue a breath in front of the 1.5–2.5 s cue window.
            adaptive = armed.cue_after_s is not None
            for index, kind in enumerate(("hmm", "wait") if adaptive else RUNGS):
                if armed.reply_pending:
                    return
                adaptive_first = adaptive and index == 0
                if kind not in self._rung_delays_s:
                    return
                armed.rung, armed.rung_kind = index, kind
                armed.playing_acknowledgement = False
                if kind == "breath" and armed.after_ack:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="after_early_ack",
                    )
                    continue
                if kind == "breath" and not self._breath_enabled and not armed.acknowledgement:
                    # Breathing off and no word planned for the first
                    # deadline: nothing to wait for here; the voiced ladder
                    # (if any) keeps its own schedule.
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="breathing_off",
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
                if kind == "hmm" and not armed.allow_voiced and not adaptive_first:
                    # The planner decided this wait stays a breath (an
                    # acknowledgement already spoke, critical content, a fast
                    # reply expected, or simply not every silence gets a word).
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="voiced_withheld",
                    )
                    continue
                if kind == "wait" and not armed.allow_spoken:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason="spoken_withheld",
                    )
                    return
                if not await self._wait_for_rung(armed):
                    return
                if adaptive_first:
                    clip = self._acknowledgement_clip(armed)
                    if clip:
                        kind = "breath"  # Existing acknowledgement bookkeeping.
                        armed.playing_acknowledgement = True
                    else:
                        clip = self._rung_clip(armed, "hmm") if armed.allow_voiced else b""
                        if not clip:
                            self._event(
                                "adaptive_cue_fallback", turn=armed.turn_id,
                                reason="no_ready_cue" if armed.allow_voiced else "voiced_withheld",
                            )
                            kind = "breath"
                            clip = self._rung_clip(armed, kind)
                    armed.rung_kind = kind
                else:
                    clip = self._rung_clip(armed, kind)
                if not clip:
                    self._event(
                        "latency_filler_skipped", turn=armed.turn_id, rung=kind,
                        reason=(
                            "breathing_off"
                            if kind == "breath" and not self._breath_enabled
                            and not armed.playing_acknowledgement
                            else "no_clip"
                        ),
                        gender=armed.gender,
                    )
                    continue
                armed.clip_ms = len(clip) / (self._sample_rate * 2) * 1000.0
                armed.clip = clip
                voiced = None
                if self._voiced_cue_gap_ms and (kind != "breath" or armed.playing_acknowledgement):
                    voiced = _VoicedHandoff(armed.owner)
                    self._voiced_handoff = voiced
                    # Ordered silence travels with the cue through output
                    # queues, so the audible gap survives browser/telephony
                    # buffering. It elapses even if the answer isn't ready;
                    # a later answer never gets another sleep added to it.
                    armed.clip += b"\x00\x00" * int(self._sample_rate * self._voiced_cue_gap_ms / 1000)
                armed.next_offset = 0
                armed.playing_since = time.monotonic()
                if armed.owner not in self._output_owners:
                    self._output_owners.append(armed.owner)
                if kind == "breath" and not armed.playing_acknowledgement:
                    # Only an actual breath sound counts for the TTS router's
                    # "no in-reply inhale right after a breath" rule; a word
                    # is not a breath.
                    note_played = getattr(self._library, "note_played", None)
                    if note_played is not None:
                        note_played(armed.playing_since)
                self.fillers_played += 1
                self.rungs_played[kind] = self.rungs_played.get(kind, 0) + 1
                if kind == "hmm":
                    self.last_cue_played = getattr(self._cue_library, "last_cue_id", None)
                    if self.cue_played_hook is not None:
                        try:
                            self.cue_played_hook(armed.turn_id, self.last_cue_played)
                        except Exception:  # noqa: BLE001 — decoration never breaks a turn
                            logger.debug("latency-filler: cue hook failed", exc_info=True)
                waited_ms = (armed.playing_since - armed.origin) * 1000.0
                self._event(
                    "latency_filler_played",
                    turn=armed.turn_id, gender=armed.gender, rung=kind,
                    waited_ms=round(waited_ms, 1), clip_ms=round(armed.clip_ms, 1),
                    **self._rung_sound(armed, kind),
                )
                logger.info(
                    "turn[%s] latency filler %s playing (turn=%d gender=%s waited=%.0fms clip=%.0fms)",
                    self._session(), kind, armed.turn_id, armed.gender, waited_ms, armed.clip_ms,
                )
                self._begin_cue_window(armed)
                if armed.playing_acknowledgement:
                    # No bot-speaking frames: this remains waiting-period
                    # audio, and the answer's first audio owns reply state.
                    armed.allow_voiced = False
                    self._event(
                        "early_ack_played", turn=armed.turn_id,
                        context=armed.acknowledgement.get("context", "answer"),
                        language=armed.language, waited_ms=round(waited_ms, 1),
                    )
                    if self.acknowledgement_hook is not None:
                        self.acknowledgement_hook(armed.turn_id)
                try:
                    await self._stream(armed)
                finally:
                    self._end_cue_window(armed)
                if armed.owner.cancelled:
                    return
                if self._emit_flush_marker:
                    await self.push_frame(
                        OutputTransportMessageFrame(message={
                            "type": AUDIO_FLUSH_MESSAGE_TYPE,
                            "filler_owner": armed.owner.token,
                        })
                    )
                self._event(
                    "latency_filler_completed", turn=armed.turn_id, rung=kind,
                    played_ms=round(armed.clip_ms, 1),
                    gap_ms=self._voiced_cue_gap_ms if voiced else 0,
                )
                if voiced is not None:
                    voiced.finished.set()
                armed.rungs_played.append(kind)
                armed.playing_since = None
                armed.clip = b""
        except Exception:
            logger.warning("latency-filler: playback failed", exc_info=True)
        finally:
            voiced = self._voiced_handoff
            if voiced is not None and voiced.owner is armed.owner and not voiced.finished.is_set():
                # A failed/cancelled producer must never leave a reply waiter
                # hanging. External cancellation is still distinguished by
                # the owner's flag; a clip failure simply releases the reply.
                voiced.failed = True
                voiced.finished.set()
            # Retirement can reach a paced producer before the data-frame
            # handler reaches _cut(). Keep that turn for cancellation metrics
            # and cleanup; it did not naturally complete its clip.
            if self._armed is armed and not armed.owner.cancelled:
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
            if self._armed is not armed or armed.owner.cancelled:
                return False
            if not self._bot_speaking:
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
        """Push owned output PCM at real-time pace with no queued look-ahead."""
        rate = self._sample_rate
        chunk_bytes = self._chunk_bytes()
        clip = armed.clip
        remainder = len(clip) % (chunk_bytes * 2)
        voiced = self._voiced_handoff
        has_voiced_gap = voiced is not None and voiced.owner is armed.owner
        word_bytes = round(armed.clip_ms * rate / 1000) * 2
        word_ended = False
        if remainder and not has_voiced_gap:
            clip = clip + b"\x00" * (chunk_bytes * 2 - remainder)
            armed.clip = clip
        chunk_s = chunk_bytes / (rate * 2)
        started = time.monotonic()
        # Neither acknowledgements nor breaths pre-fill the output queue.
        lead_chunks = 0
        for index, offset in enumerate(range(0, len(clip), chunk_bytes)):
            due = started + max(0, index - lead_chunks) * chunk_s
            wait = due - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            if armed.owner.cancelled:
                return
            if has_voiced_gap and not word_ended and offset >= word_bytes:
                word_ended = True
                # Silence needs no voiced echo shield. Release the caller's
                # gate during the gap so fresh speech can interrupt promptly.
                self._end_cue_window(armed)
                self._event("voiced_cue_word_completed", turn=armed.turn_id, clip_ms=armed.clip_ms)
            armed.next_offset = offset + chunk_bytes
            await self.push_frame(
                FillerAudioRawFrame(
                    audio=clip[offset:offset + chunk_bytes], sample_rate=rate, num_channels=1,
                    owner=armed.owner,
                )
            )

    # -- pipeline plumbing ---------------------------------------------

    def _notice_reply_audio(self) -> None:
        """Freeze the ladder at PCM ingress, including before data processing."""
        if self._armed is not None:
            self._armed.reply_pending = True
        voiced = self._voiced_handoff
        if voiced is None or voiced.owner.cancelled:
            # More PCM can arrive while the first packet is being forwarded.
            # That is still the same reply, not a cancellation of its cue.
            pending = self._reply_handoff_owner
            if pending is None or pending.cancelled:
                self._retire_output_owners()

    async def _finish_voiced_handoff(self, voiced: _VoicedHandoff) -> bool:
        """Wait only for an already-started cue/gap; caller cancellation wins."""
        started = time.monotonic()
        finished = asyncio.create_task(voiced.finished.wait())
        cancelled = asyncio.create_task(voiced.owner.cancelled_event.wait())
        try:
            await asyncio.wait((finished, cancelled), return_when=asyncio.FIRST_COMPLETED)
            valid = not voiced.owner.cancelled and self._voiced_handoff is voiced
            if valid:
                self._event(
                    "voiced_cue_reply_handoff", turn=voiced.owner.turn_id,
                    held_ms=round((time.monotonic() - started) * 1000, 1),
                    gap_ms=self._voiced_cue_gap_ms,
                    cue_failed=voiced.failed,
                )
            return valid
        finally:
            finished.cancel()
            cancelled.cancel()
            await asyncio.gather(finished, cancelled, return_exceptions=True)

    async def queue_frame(self, frame, direction=FrameDirection.DOWNSTREAM, callback=None):
        # Mark readiness at ingress, before Pipecat's data-frame queue:
        # cut breaths/pending cues, but let an already-started word finish.
        if isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame, CancelFrame)):
            self._retire_output_owners()
        elif (
            direction == FrameDirection.DOWNSTREAM
            and isinstance(frame, TTSAudioRawFrame) and frame.num_frames > 0
        ):
            self._notice_reply_audio()
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame, CancelFrame)):
            self._retire_output_owners()
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            rate = getattr(frame, "audio_out_sample_rate", 0)
            if rate:
                self._sample_rate = int(rate)
        elif isinstance(frame, TTSAudioRawFrame):
            # Readiness means at least one complete PCM sample frame. An
            # empty packet or TTSStartedFrame (synthesis requested) leaves
            # the configured filler deadline intact while the provider waits.
            # Finish a started voiced cue or cut a breath before forwarding.
            if frame.num_frames > 0 and direction == FrameDirection.DOWNSTREAM:
                self._observe_reply_level(frame.audio)
                self._notice_reply_audio()
                voiced = self._voiced_handoff
                if voiced is not None:
                    ready = await self._finish_voiced_handoff(voiced)
                    if not ready or voiced.owner.cancelled or self._voiced_handoff is not voiced:
                        return  # Never forward a reply cancelled during the cue/gap.
                    # The output queue owns the finished cue and its silence.
                    # Clearing that owner here would chop buffered audio again.
                    self._reply_handoff_owner = voiced.owner
                    try:
                        await self._cut("tts_audio", preserve=voiced.owner)
                        if not voiced.owner.cancelled:
                            await self.push_frame(frame, direction)
                    finally:
                        if self._reply_handoff_owner is voiced.owner:
                            self._reply_handoff_owner = None
                    return
                else:
                    await self._cut("tts_audio")
        elif isinstance(frame, InterruptionFrame):
            # The output is being stopped globally. Do not depend on a later
            # BotStoppedSpeakingFrame to release a new turn's filler deadline.
            self._bot_speaking = False
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
            # The call is over: nothing more may play, and this call's
            # rotation/recency history is released with it.
            await self._cut("pipeline_end")
            self._release_call_history()
        await self.push_frame(frame, direction)

    def _release_call_history(self) -> None:
        for library in (self._library, self._cue_library):
            if getattr(library, "_session_local", False):
                library.clear_history()
        self.last_cue_played = None

    async def cleanup(self):
        await self._cut("cleanup")
        self._release_call_history()
        await super().cleanup()
