"""Voiced latency cues: "Hmm…" / "एक सेकंड…" in the bot's own voice, pre-rendered.

The breath (voice_runtime.latency_filler) covers the first second of a long
wait; when the reply is still not speaking, the escalation ladder plays a
short cue in the SAME voice the reply will use. A per-turn TTS round-trip
would add its own latency and cost, so each (engine, language, cue) is
rendered ONCE through the provider's REST ``synthesize`` and cached — in
memory for the process and as a WAV on disk under ``filler_audio_dir/cache``
so a restart does not re-render. Rendering runs in the background from the
first call that needs a voice; a cue that is not ready yet is simply
skipped for that turn (the ladder never waits on a render).

Clips are trimmed of provider lead/tail silence, faded at both ends and
normalized to a level under the reply's (about -25 dBFS RMS, peaks capped
at -10 dBFS) — audible presence, not a reply. A failed render is remembered for a cooldown so a broken key
or provider cannot hammer the API once per turn.

Every take is checked before it is cached (:func:`assess_take`), in the
background render only: a cue is never cut while a word is still sounding,
and an acknowledgement that ends whispered, is drawn out or holds a long
hesitation is rejected and rendered again. A cue with no acceptable take in
``_MAX_RENDER_ATTEMPTS`` renders plays nothing for that voice — silence is
better than a breathy, sighing filler — and is negative-cached for
``_NEGATIVE_CACHE_S``; the next render opportunity after that tries again.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
from pathlib import Path

import numpy as np

from shared.audio.pcm import (
    apply_fade_in,
    apply_fade_out,
    pcm_to_wav_bytes,
    resample_pcm,
    wav_to_pcm,
)
from shared.orchestration.naturalness import (
    LADDER_CUE_KINDS,
    ladder_cue,
    ladder_cue_options,
    ladder_cue_text,
    selection_ids,
)

logger = logging.getLogger(__name__)

# Below the reply's level: a cue is a beat, not a sentence. Providers differ
# widely in loudness (Sarvam peaks near 0 dBFS), so cues are normalized to a
# target RMS rather than lowered by a relative amount; TTS speech sits around
# -18..-20 dBFS RMS.
CUE_TARGET_RMS_DBFS = {"hmm": -26.0, "wait": -24.0}
CUE_PEAK_CEILING_DBFS = -10.0
# Play-time level matching (LatencyFillerProcessor passes the reply's
# measured level): the rendered baseline may be raised or lowered within
# this range, peaks stay under the ceiling. Live replies measured -18..-19
# dBFS on telephony vs the fixed -26 dBFS baseline (2026-09-17 audit).
MATCH_GAIN_RANGE_DB = (-6.0, 15.0)
MATCH_PEAK_CEILING_DBFS = -3.0
# Longest a cue may run after silence trimming. A longer take is cut at the
# ceiling only when nothing voiced remains there (a breath or near-silence
# tail); a word still sounding rejects the take instead — the old blind cut
# at a 1000 ms default turned "जी, ठीक है…" into "जी, ठीक ह-" on a call
# (cv_ceb9e7e7f458).
_MAX_CUE_MS = {"hmm": 900, "wait": 1400, "ack": 1400}
_FADE_IN_MS = 10
_FADE_OUT_MS = 40
_TRIM_THRESHOLD_DBFS = -45.0
# Part of every cache key: clips made by an older render pipeline are never
# selected again (they stay on disk, unused). v2 = acknowledgement text sent
# without its trailing ellipsis, the take gate, never-cut-voice ceilings.
_RENDER_VERSION = 2
_FAILURE_COOLDOWN_S = 300.0
# A background render whose every take was rejected is not repeated for this
# long — recorded on disk, so neither later calls nor a restarted worker
# re-bill the provider — and the cue is eligible again afterwards: Eleven v3
# takes are stochastic, and four unlucky ones must not disable a cue for good.
_NEGATIVE_CACHE_S = 12 * 3600.0
_RENDER_TIMEOUT_S = 12.0
# Cues are rendered once at the highest rate both streaming providers
# synthesize natively and resampled (anti-aliased) to each call's rate. The
# old 16 kHz renders reached the 8 kHz telephony leg through a linear
# interpolation that aliased everything above 4 kHz back into the band —
# the "thinner" cue timbre heard on phone calls (2026-09-17 audit).
CUE_RENDER_SAMPLE_RATE = 24000
# Synthesis parameters that make a cue sound like the reply; anything else
# on the engine dict (buffer sizes, completion events) is transport plumbing
# and must not fork the cache.
_VOICE_PARAM_KEYS = (
    "pace", "speed", "temperature", "pitch", "loudness", "enable_preprocessing",
    "stability", "similarity_boost", "style", "use_speaker_boost", "dict_id",
)


def _accepts_sample_rate(renderer) -> bool:
    """Whether ``renderer`` takes a ``sample_rate`` keyword (explicitly or via
    ``**kwargs``). Unknown signatures are assumed positional-only."""
    try:
        parameters = inspect.signature(renderer).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        p.name == "sample_rate" or p.kind is inspect.Parameter.VAR_KEYWORD
        for p in parameters
    )


def voice_params(engine: dict | None) -> dict:
    """The subset of ``engine["params"]`` that shapes the rendered voice."""
    params = (engine or {}).get("params") or {}
    return {
        key: params[key] for key in _VOICE_PARAM_KEYS
        if params.get(key) is not None
    }


def trim_silence(pcm: bytes, sample_rate: int, *, threshold_dbfs: float = _TRIM_THRESHOLD_DBFS,
                 keep_ms: int = 40) -> bytes:
    """Drop leading/trailing near-silence (10 ms windows under ``threshold_dbfs``),
    keeping ``keep_ms`` of quiet on each side so the cue is not clipped."""
    if len(pcm) < 4 or sample_rate <= 0:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float32)
    win = max(1, int(sample_rate * 0.01))
    n = samples.size // win
    if n == 0:
        return pcm
    rms = np.sqrt((samples[: n * win].reshape(n, win) ** 2).mean(axis=1)) / 32767.0
    loud = np.flatnonzero(rms > 10 ** (threshold_dbfs / 20.0))
    if loud.size == 0:
        return b""
    keep = int(sample_rate * keep_ms / 1000)
    start = max(0, int(loud[0]) * win - keep)
    end = min(samples.size, (int(loud[-1]) + 1) * win + keep)
    return samples[start:end].astype("<i2").tobytes()


def normalize_level(pcm: bytes, *, target_rms_dbfs: float,
                    peak_ceiling_dbfs: float = CUE_PEAK_CEILING_DBFS) -> bytes:
    """Scale 16-bit PCM to ``target_rms_dbfs`` RMS, then cap its peak."""
    if len(pcm) < 4:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float32)
    rms = float(np.sqrt(np.mean(samples**2)))
    if rms <= 0.0:
        return b""
    samples *= (10 ** (target_rms_dbfs / 20.0) * 32767.0) / rms
    ceiling = 10 ** (peak_ceiling_dbfs / 20.0) * 32767.0
    peak = float(np.max(np.abs(samples)))
    if peak > ceiling:
        samples *= ceiling / peak
    return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()


# ── take gate ───────────────────────────────────────────────────────────────
# A cue is rendered once per voice and replayed on every call, so one bad
# take is heard forever. Thresholds come from a 78-take A/B of Eleven v3
# acknowledgements against the same voice's reply speech (2026-09-29).
_MAX_RENDER_ATTEMPTS = 4
_ANALYSIS_RATE = 16000
_FRAME_MS, _HOP_MS = 40, 10
# Voiced = normalized autocorrelation peak over 60-400 Hz lags at least this.
_VOICED_PERIODICITY = 0.45
_PITCH_RANGE_HZ = (60.0, 400.0)
# Whispered/devoiced ending: the last 250 ms of clearly audible sound (frames
# within 25 dB of the loudest) must hold at least 40 ms of clearly sounded
# voice (periodicity >= 0.75). Natural endings held 40-200 ms, whispered
# "अच्छा" endings 0-50 ms — a phoneme-neutral test ("है" ends breathy even
# when natural, so a plain voiced share would reject good takes).
_ENDING_WINDOW_MS = 250
_ENDING_AUDIBLE_RANGE_DB = 25.0
_CLEAR_VOICE_PERIODICITY = 0.75
_ENDING_MIN_CLEAR_MS = 40
# Breath or whisper trailing after the last voiced frame (natural <= 30 ms).
_MAX_UNVOICED_TAIL_MS = 100
# Sound span (first to last audible frame, pauses included) per estimated
# syllable; one-syllable words get a floor, hums twice the budget. Natural
# takes ran up to ~275 ms per syllable, sighing ones 300-490.
_DRAWN_OUT_MS_PER_SYLLABLE = 300
_DRAWN_OUT_MIN_MS = 500
# Natural comma pauses measured up to 280 ms; hesitations 310-540 ms.
_MAX_INTERNAL_PAUSE_MS = 320
# A cue whose text ends in an ellipsis is read by Eleven v3 as a trailing,
# sighing hesitation; the ellipsis is dropped from what is synthesized.
_TRAILING_ELLIPSIS_RE = re.compile(r"(?:\s*(?:\u2026|\.{2,}|\u22ef))+\s*$")
_HUM_RE = re.compile(r"^[hm\s-]+$", re.IGNORECASE)
_SILENT = "silent"  # nothing audible was rendered: a retry would not help


def synthesis_text(kind: str, text: str) -> str:
    """The text actually sent to TTS for a cue of ``kind``.

    Acknowledgements lose their trailing ellipsis: rendered alone, "अच्छा…"
    and "जी, ठीक है…" came back drawn out and breathy (a stretched final
    vowel, a 330-490 ms "जी", hesitation pauses) at either stability and on
    either ElevenLabs endpoint. The pool text — planner history, telemetry,
    UI — keeps it. Ladder cues ("hmm"/"wait") are synthesized as written.
    """
    if kind != "ack" or not text:
        return text
    return _TRAILING_ELLIPSIS_RE.sub("", text).strip()


def _devanagari_syllables(word: str) -> int:
    """Syllables of one Devanagari word: independent vowels, plus consonants
    carrying a vowel sign, a nasal sign or an inherent vowel before another
    consonant. A word-final consonant and one under virama add none (Hindi
    schwa deletion), so अच्छा = 2, ठीक = 1, सेकंड = 2."""
    chars = [c for c in word if c != "\u093c"]  # nukta belongs to its consonant
    count = 0
    for i, char in enumerate(chars):
        code = ord(char)
        if 0x0904 <= code <= 0x0914 or code in (0x0960, 0x0961):
            count += 1
        elif 0x0915 <= code <= 0x0939 or 0x0958 <= code <= 0x095F:
            following = chars[i + 1] if i + 1 < len(chars) else None
            if following is not None and following != "\u094d":
                count += 1
    return max(1, count)


def _syllable_estimate(text: str) -> int | None:
    """Rough syllable count for Devanagari and Latin text, None for any other
    script (the drawn-out check is then skipped rather than guessed)."""
    words = re.findall(r"[\u0900-\u097f]+|[A-Za-z]+|[^\W\d_]+", text or "")
    total = 0
    for word in words:
        if re.fullmatch(r"[\u0900-\u097f]+", word):
            total += _devanagari_syllables(word)
        elif re.fullmatch(r"[A-Za-z]+", word):
            total += max(1, len(re.findall(r"[aeiouy]+", word.lower())))
        else:
            return None
    return total or None


def _frame_track(pcm: bytes, rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Level (dBFS) and periodicity of 40 ms frames every 10 ms, at 16 kHz.

    Periodicity is the peak of the window-corrected normalized
    autocorrelation over 60-400 Hz lags: near 1 for a sounded vowel, low for
    breath, whisper and fricatives. One vectorized FFT pass per take.
    """
    if rate != _ANALYSIS_RATE:
        pcm = resample_pcm(pcm, rate, _ANALYSIS_RATE)
    x = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float64)
    n = _ANALYSIS_RATE * _FRAME_MS // 1000
    hop = _ANALYSIS_RATE * _HOP_MS // 1000
    if x.size < n:
        return np.zeros(0), np.zeros(0)
    count = 1 + (x.size - n) // hop
    frames = x[np.arange(n)[None, :] + hop * np.arange(count)[:, None]]
    level = 20.0 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) / 32767.0 + 1e-12)
    window = np.hanning(n)
    centred = (frames - frames.mean(axis=1, keepdims=True)) * window
    ac = np.fft.irfft(np.abs(np.fft.rfft(centred, 2 * n, axis=1)) ** 2, axis=1)[:, :n]
    wac = np.fft.irfft(np.abs(np.fft.rfft(window, 2 * n)) ** 2)[:n]
    lo = int(_ANALYSIS_RATE / _PITCH_RANGE_HZ[1])
    hi = int(_ANALYSIS_RATE / _PITCH_RANGE_HZ[0])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = (ac[:, lo:hi] / ac[:, :1]) / (wac[lo:hi] / wac[0])
    periodicity = np.nan_to_num(ratio, nan=0.0, posinf=0.0, neginf=0.0).max(axis=1)
    return level, periodicity


def _longest_run(flags: np.ndarray) -> int:
    longest = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        longest = max(longest, current)
    return longest


def assess_take(pcm: bytes, rate: int, kind: str, text: str | None = None) -> tuple[int, str | None]:
    """Judge one silence-trimmed take: ``(bytes to keep, None)`` when usable,
    ``(0, reason)`` when not.

    Every kind: a take longer than its ceiling may lose only a breath or
    near-silence tail — voiced audio reaching into the fade-out before the
    ceiling rejects it (never a word cut in half). Acknowledgements are also
    rejected for a hesitation pause inside, a drawn-out delivery (sound span
    per estimated syllable), a whispered/devoiced ending or a breath/whisper
    trailing after the last voiced sound.
    """
    if not pcm or rate <= 0:
        return 0, _SILENT
    level, periodicity = _frame_track(pcm, rate)
    if level.size == 0:
        return 0, _SILENT
    target = CUE_TARGET_RMS_DBFS.get(kind, -25.0)
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float64)
    rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
    if rms <= 0.0:
        return 0, _SILENT
    # Judge the take at the level it will be played at, not the provider's.
    active = level + (target - 20.0 * np.log10(rms / 32767.0)) >= _TRIM_THRESHOLD_DBFS
    voiced = active & (periodicity >= _VOICED_PERIODICITY)
    keep = len(pcm) - (len(pcm) % 2)
    cap_ms = _MAX_CUE_MS.get(kind, 1000)
    if keep / 2 / rate * 1000.0 > cap_ms:
        # Frames overlapping [ceiling - fade-out, end): the fade would dim
        # them, the cut would drop them.
        first = max(0, int((cap_ms - _FADE_OUT_MS - _FRAME_MS) // _HOP_MS) + 1)
        if voiced[first:].any():
            return 0, f"voiced speech continues past the {cap_ms} ms ceiling"
        keep = int(rate * cap_ms / 1000) * 2
        frames_kept = (cap_ms - _FRAME_MS) // _HOP_MS + 1
        active, voiced = active[:frames_kept], voiced[:frames_kept]
    if kind != "ack":
        return keep, None
    heard = np.flatnonzero(active)
    if heard.size == 0:
        return 0, _SILENT
    first_frame, last_frame = int(heard[0]), int(heard[-1])
    pause_ms = _longest_run(~active[first_frame:last_frame + 1]) * _HOP_MS
    if pause_ms > _MAX_INTERNAL_PAUSE_MS:
        return 0, f"hesitation pause of {pause_ms} ms"
    syllables = _syllable_estimate(text or "")
    if syllables:
        budget = max(_DRAWN_OUT_MIN_MS, _DRAWN_OUT_MS_PER_SYLLABLE * syllables)
        if _HUM_RE.match(text or ""):
            budget *= 2
        span_ms = (last_frame - first_frame) * _HOP_MS + _FRAME_MS
        if span_ms > budget:
            return 0, f"drawn out: {span_ms} ms for ~{syllables} syllable(s) (limit {budget} ms)"
    loud = np.flatnonzero(level[: active.size] >= level[: active.size].max() - _ENDING_AUDIBLE_RANGE_DB)
    ending = loud[-(_ENDING_WINDOW_MS // _HOP_MS):]
    clear_ms = int((periodicity[ending] >= _CLEAR_VOICE_PERIODICITY).sum()) * _HOP_MS
    if clear_ms < _ENDING_MIN_CLEAR_MS:
        return 0, f"whispered/devoiced ending ({clear_ms} ms clearly voiced in its last {_ENDING_WINDOW_MS} ms)"
    sounded = np.flatnonzero(voiced)
    tail_ms = int(active[int(sounded[-1]) + 1:].sum()) * _HOP_MS if sounded.size else 0
    if tail_ms > _MAX_UNVOICED_TAIL_MS:
        return 0, f"breath/whisper trailing {tail_ms} ms after the last voiced sound"
    return keep, None


def _safe_token(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-")[:48] or "x"


class VoicedCueLibrary:
    """Per-(engine, language, kind) cue clips, rendered once and cached.

    ``renderer`` is ``async (engine: dict, language: str, text: str) ->
    (pcm16 bytes, sample_rate)``; the default one goes through the REST
    ``TTSProvider`` for the engine (see :func:`default_renderer`).
    """

    # Duck-typed capability the filler processor checks before asking for a
    # level-matched clip (test stubs and older libraries return raw clips).
    supports_level_matching = True

    def __init__(self, cache_dir: str | Path | None = None, *, renderer=None) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._renderer = renderer or default_renderer
        # (key, rate, target dB bucket) -> level-matched PCM
        self._matched: dict[tuple[str, int, int], bytes] = {}
        # Test/legacy renderers take (engine, language, text); the default
        # one also accepts the render rate. Detected from the signature and
        # confirmed at the first call (a wrapper may hide its parameters).
        self._renderer_takes_rate = _accepts_sample_rate(self._renderer)
        # key -> (pcm, native_rate); b"" marks "rendered, nothing usable".
        self._clips: dict[str, tuple[bytes, int]] = {}
        self._resampled: dict[tuple[str, int], bytes] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._failed_at: dict[str, float] = {}
        # key -> wall-clock time until which the cue is negative-cached (all
        # takes of its last render rejected). Wall clock, not monotonic: the
        # deadline is persisted in the marker and must survive a restart.
        self._negative_until: dict[str, float] = {}
        self._clock = time.time
        # The cue id most recently handed out (telemetry).
        self.last_cue_id: str | None = None
        self.renders = 0
        self.render_failures = 0
        # Take gate telemetry: takes rejected, renders that ended negative-
        # cached, and per key the outcome of its last background render.
        self.rejected_takes = 0
        self.skipped_cues = 0
        self.render_log: dict[str, dict] = {}

    def new_session(self) -> VoicedCueSession:
        """A call's selection history, sharing only audio/render caches."""
        return VoicedCueSession(self)

    # -- keys -----------------------------------------------------------

    @staticmethod
    def engine_key(engine: dict | None, language: str) -> str:
        engine = engine or {}
        return "_".join(
            _safe_token(v) for v in (
                engine.get("provider"), engine.get("model"), engine.get("voice"),
                (language or "").lower(),
            )
        )

    def _key(self, engine: dict | None, language: str, kind: str, text: str | None = None) -> str:
        text = ladder_cue(language, kind) if text is None else text
        # Identity = what is actually synthesized (an acknowledgement without
        # its trailing ellipsis) plus the voice parameters: a bot at speed
        # 1.2 with temperature 0.01 must not play a cue cached for defaults,
        # and two bots sharing a speaker but not a delivery must not share
        # clips. The render version retires clips of an older pipeline.
        spoken = synthesis_text(kind, text)
        params = voice_params(engine)
        material = spoken if not params else spoken + "\x1f" + json.dumps(params, sort_keys=True, default=str)
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
        suffix = "" if not params else "_p"
        return f"{self.engine_key(engine, language)}_{kind}_{digest}{suffix}_r{_RENDER_VERSION}"

    @staticmethod
    def cue_choices(language: str, kind: str, selection: dict | None = None) -> list[tuple[str, str]]:
        """``[(cue_id, text), …]`` in preference order: the ``selection``
        handed down for the "hmm" rung (the planner's per-turn ranking of the
        bot's allowed cues, best first; ids unknown to the language are
        dropped), else the language default alone. Wait options are the
        language's allowed wait pool (currently one phrase per language)."""
        options = ladder_cue_options(language, kind)
        if not options:
            return []
        if kind != "hmm":
            return [(option["id"], option["text"]) for option in options]
        if not selection:
            # No selection → the language default only (the planner supplies
            # the default rotation, see SpeechNaturalnessPlanner.cue_selection_for).
            return [(options[0]["id"], options[0]["text"])]
        picked = [
            (cue_id, ladder_cue_text(language, kind, cue_id))
            for cue_id in selection_ids(selection)
            if ladder_cue_text(language, kind, cue_id)
        ]
        return picked or [(options[0]["id"], options[0]["text"])]

    def _disk_path(self, key: str) -> Path | None:
        return (self._cache_dir / f"{key}.wav") if self._cache_dir is not None else None

    def _skip_path(self, key: str) -> Path | None:
        """Negative-cache marker of a cue whose last render had no acceptable
        take: no audio is stored, and until its ``retry_after`` neither a
        later turn nor a restart renders it again. A different synthesized
        text, voice parameters or render version is a different key and so
        never inherits the marker."""
        return (self._cache_dir / f"{key}.skip.json") if self._cache_dir is not None else None

    def _negative_active(self, key: str) -> bool:
        """Whether ``key`` is inside its negative-cache window (an expired
        window is forgotten: the cue is eligible for a render again)."""
        until = self._negative_until.get(key)
        if until is None:
            return False
        if self._clock() < until:
            return True
        self._negative_until.pop(key, None)
        return False

    def _marker_retry_after(self, marker: Path) -> float:
        """The marker's ``retry_after``; a marker without a readable one (an
        older permanent skip) counts from its file time."""
        try:
            value = json.loads(marker.read_text(encoding="utf-8")).get("retry_after")
            if isinstance(value, (int, float)):
                return float(value)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            return marker.stat().st_mtime + _NEGATIVE_CACHE_S
        except OSError:
            return 0.0

    # -- public API -----------------------------------------------------

    def acknowledgement_clip(
        self, engine: dict | None, language: str, text: str, sample_rate: int,
        target_rms_dbfs: float | None = None,
    ) -> bytes:
        """A planned acknowledgement from the existing background-render cache.

        A cache miss starts rendering and returns immediately. This must never
        use the reply's streaming TTS connection or wait for synthesis.
        ``target_rms_dbfs`` (the reply's level minus a margin) places the
        clip at the reply's loudness instead of the rendered baseline.
        """
        if not text or sample_rate <= 0:
            return b""
        return self._cached_clip(
            engine, language, "ack", text, sample_rate, target_rms_dbfs=target_rms_dbfs,
        )

    def level_matched(self, key: str, pcm: bytes, sample_rate: int, target_rms_dbfs: float) -> bytes:
        """``pcm`` scaled toward ``target_rms_dbfs`` (bounded gain, capped
        peaks), cached per 1 dB bucket so a call re-scales nothing per turn."""
        if not pcm:
            return pcm
        bucket = int(round(target_rms_dbfs))
        cached = self._matched.get((key, sample_rate, bucket))
        if cached is not None:
            return cached
        samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype="<i2").astype(np.float32)
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        if rms <= 1.0:
            return pcm
        current = 20.0 * np.log10(rms / 32767.0)
        low, high = MATCH_GAIN_RANGE_DB
        gain = min(high, max(low, float(bucket) - current))
        out = normalize_level(
            pcm, target_rms_dbfs=current + gain, peak_ceiling_dbfs=MATCH_PEAK_CEILING_DBFS,
        ) or pcm
        self._matched[(key, sample_rate, bucket)] = out
        return out

    def clip(
        self, engine: dict | None, language: str, kind: str, sample_rate: int,
        selection: dict | None = None, target_rms_dbfs: float | None = None,
    ) -> bytes:
        """The best READY cue clip for this turn at ``sample_rate``, or b""
        when none is ready.

        ``selection`` is a preference order (the planner's ranking for the
        turn, best first): the first cue already rendered plays; unrendered
        ones are skipped, never waited for. Never blocks: missing clips
        schedule a background render (once) and this turn gets nothing when
        none of the preferred cues is ready.
        """
        if kind not in LADDER_CUE_KINDS or sample_rate <= 0:
            return b""
        for cue_id, text in self.cue_choices(language, kind, selection):
            pcm = self._cached_clip(
                engine, language, kind, text, sample_rate, target_rms_dbfs=target_rms_dbfs,
            )
            if pcm:
                self.last_cue_id = cue_id
                return pcm
        return b""

    def _cached_clip(
        self, engine: dict | None, language: str, kind: str, text: str, sample_rate: int,
        target_rms_dbfs: float | None = None,
    ) -> bytes:
        key = self._key(engine, language, kind, text)
        cached = self._clips.get(key)
        if cached is None:
            if self._negative_active(key):
                return b""  # negative-cached: no render until the window ends
            cached = self._load_from_disk(key)
        if cached is None:
            self._schedule_render(key, engine, language, kind, text)
            return b""
        pcm, rate = cached
        if not pcm:
            return b""
        if rate != sample_rate:
            out = self._resampled.get((key, sample_rate))
            if out is None:
                out = resample_pcm(pcm, rate, sample_rate)
                self._resampled[(key, sample_rate)] = out
            pcm = out
        if target_rms_dbfs is not None:
            pcm = self.level_matched(key, pcm, sample_rate, target_rms_dbfs)
        return pcm

    def ready(self, engine: dict | None, language: str, kind: str, cue_id: str | None = None) -> bool:
        text = ladder_cue_text(language, kind, cue_id) if cue_id else ladder_cue(language, kind)
        if not text:
            return False
        key = self._key(engine, language, kind, text)
        cached = self._clips.get(key)
        if cached is None:
            cached = self._load_from_disk(key)
        return bool(cached and cached[0])

    def warm(self, engine: dict | None, language: str, selection: dict | None = None) -> None:
        """Start rendering every cue of ``language`` the bot may play for
        ``engine`` that is not cached yet (fire-and-forget; safe per turn)."""
        for kind in LADDER_CUE_KINDS:
            for _cue_id, text in self.cue_choices(language, kind, selection):
                self._cached_clip(engine, language, kind, text, 16000)

    async def wait_ready(
        self, engine: dict | None, language: str, timeout: float = 15.0,
        selection: dict | None = None,
    ) -> None:
        """Await pending renders for ``language`` (tests / warm-up scripts)."""
        keys = [
            self._key(engine, language, kind, text)
            for kind in LADDER_CUE_KINDS
            for _cue_id, text in self.cue_choices(language, kind, selection)
        ]
        pending = [self._tasks[k] for k in keys if k in self._tasks and not self._tasks[k].done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def render_now(
        self, engine: dict | None, language: str, kind: str, cue_id: str | None,
        sample_rate: int, timeout: float = _RENDER_TIMEOUT_S + 3.0,
    ) -> bytes:
        """The clip for one cue option, rendering it first if needed (preview
        path: the very bytes the runtime will play, from the same cache)."""
        text = ladder_cue_text(language, kind, cue_id) if cue_id else ladder_cue(language, kind)
        if kind not in LADDER_CUE_KINDS or not text or sample_rate <= 0:
            return b""
        pcm = self._cached_clip(engine, language, kind, text, sample_rate)
        if pcm:
            return pcm
        key = self._key(engine, language, kind, text)
        task = self._tasks.get(key)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                return b""
        return self._cached_clip(engine, language, kind, text, sample_rate)

    # -- rendering ------------------------------------------------------

    def _load_from_disk(self, key: str) -> tuple[bytes, int] | None:
        path = self._disk_path(key)
        if path is not None and path.is_file():
            try:
                pcm, rate = wav_to_pcm(path.read_bytes())
            except (OSError, ValueError):
                logger.warning("voiced-cues: unreadable cache file %s; re-rendering", path)
                pcm, rate = b"", 0
            if pcm and rate > 0:
                self._clips[key] = (pcm, rate)
                return self._clips[key]
        skip = self._skip_path(key)
        if skip is not None and skip.is_file():
            retry_after = self._marker_retry_after(skip)
            if self._clock() < retry_after:
                # Negative-cached (possibly by another process): no audio and
                # no render until the window ends; an expired marker is ignored.
                self._negative_until[key] = retry_after
        return None

    def _schedule_render(
        self, key: str, engine: dict | None, language: str, kind: str, text: str | None = None,
    ) -> None:
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return
        if self._negative_active(key):
            return  # every take of its last render was rejected: wait out the window
        failed_at = self._failed_at.get(key)
        if failed_at is not None and time.monotonic() - failed_at < _FAILURE_COOLDOWN_S:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller outside the pipeline): nothing to do
        self._tasks[key] = loop.create_task(
            self._render(key, dict(engine or {}), language, kind, text)
        )

    async def _render_once(self, engine: dict, language: str, text: str) -> tuple[bytes, int]:
        if self._renderer_takes_rate:
            try:
                render = self._renderer(
                    engine, language, text, sample_rate=CUE_RENDER_SAMPLE_RATE
                )
            except TypeError:
                # The renderer does not take the rate after all (binding
                # fails before anything runs): fall back for good.
                self._renderer_takes_rate = False
                render = self._renderer(engine, language, text)
        else:
            render = self._renderer(engine, language, text)
        pcm, rate = await asyncio.wait_for(render, timeout=_RENDER_TIMEOUT_S)
        return pcm or b"", int(rate or 0)

    async def _render(
        self, key: str, engine: dict, language: str, kind: str, text: str | None = None,
    ) -> None:
        """Background render of one cue, gated take by take.

        Runs only in this background task — a call never waits on it and a
        turn whose cue is not ready simply gets none. A rejected take is
        rendered again, up to ``_MAX_RENDER_ATTEMPTS`` renders; with no
        acceptable take nothing is stored (no breathy or cut clip) and the cue
        is negative-cached for ``_NEGATIVE_CACHE_S`` — a marker with its
        ``retry_after`` — after which a later render opportunity tries again.
        An accepted take clears any negative state.
        """
        text = ladder_cue(language, kind) if text is None else text
        spoken = synthesis_text(kind, text)
        reasons: list[str] = []
        rate = 0
        for attempt in range(1, _MAX_RENDER_ATTEMPTS + 1):
            try:
                pcm, rate = await self._render_once(engine, language, spoken)
            except Exception:  # noqa: BLE001 — a cue is decoration; never fatal
                self.render_failures += 1
                self._failed_at[key] = time.monotonic()
                logger.warning(
                    "voiced-cues: render failed for %s (%s %s/%s)",
                    key, kind, engine.get("provider"), engine.get("voice"), exc_info=True,
                )
                return
            self.renders += 1
            # Tens of ms of numpy per take: off the event loop that paces
            # the live call's audio.
            clip, reason = await asyncio.to_thread(self._finish_take, pcm, rate, kind, spoken)
            if clip:
                self._clips[key] = (clip, rate)
                self.render_log[key] = {"attempts": attempt, "accepted": True,
                                        "rejections": reasons, "ms": round(len(clip) / (rate * 2) * 1000.0)}
                path = self._disk_path(key)
                if path is not None:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(pcm_to_wav_bytes(clip, sample_rate=rate))
                    except OSError:
                        logger.debug("voiced-cues: could not cache %s", path, exc_info=True)
                # An accepted take ends any negative state of this cue.
                self._negative_until.pop(key, None)
                skip = self._skip_path(key)
                if skip is not None:
                    try:
                        skip.unlink(missing_ok=True)
                    except OSError:
                        logger.debug("voiced-cues: could not clear %s", skip, exc_info=True)
                logger.info(
                    "voiced-cues: rendered %s (%.0f ms, take %d/%d)",
                    key, len(clip) / (rate * 2) * 1000.0, attempt, _MAX_RENDER_ATTEMPTS,
                )
                return
            if reason == _SILENT:
                # Nothing audible came back (mock provider, empty audio): a
                # retry would not change that.
                self._clips[key] = (b"", rate)
                self.render_log[key] = {"attempts": attempt, "accepted": False,
                                        "rejections": reasons + [reason], "ms": 0}
                logger.info("voiced-cues: %s rendered to silence; cue disabled for this voice", key)
                return
            reasons.append(reason)
            self.rejected_takes += 1
            logger.info(
                "voiced-cues: rejected take %d/%d for %s: %s",
                attempt, _MAX_RENDER_ATTEMPTS, key, reason,
            )
        # Nothing stored or played; negative-cached, not disabled for good.
        now = self._clock()
        retry_after = now + _NEGATIVE_CACHE_S
        self._negative_until[key] = retry_after
        self.skipped_cues += 1
        self.render_log[key] = {"attempts": _MAX_RENDER_ATTEMPTS, "accepted": False,
                                "rejections": reasons, "ms": 0, "retry_after": retry_after}
        skip = self._skip_path(key)
        if skip is not None:
            try:
                skip.parent.mkdir(parents=True, exist_ok=True)
                skip.write_text(json.dumps({
                    "kind": kind, "text": spoken, "attempts": _MAX_RENDER_ATTEMPTS,
                    "rejections": reasons, "render_version": _RENDER_VERSION,
                    "rejected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
                    "retry_after": retry_after,
                    "retry_after_local": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(retry_after)),
                }, ensure_ascii=False, indent=1))
            except OSError:
                logger.debug("voiced-cues: could not record the negative cache for %s", key, exc_info=True)
        logger.warning(
            "voiced-cues: no acceptable take for %s in %d renders (%s); not rendered again for %.0f h",
            key, _MAX_RENDER_ATTEMPTS, "; ".join(reasons), _NEGATIVE_CACHE_S / 3600.0,
        )

    @staticmethod
    def _finish_take(pcm: bytes, rate: int, kind: str, text: str | None = None) -> tuple[bytes, str | None]:
        """Trim, gate, cap, fade and level one rendered take.

        ``(clip, None)`` for a usable take, ``(b"", reason)`` otherwise —
        ``reason`` is ``"silent"`` when nothing audible was rendered.
        """
        if not pcm or rate <= 0:
            return b"", _SILENT
        pcm = trim_silence(pcm, rate)
        if not pcm:
            return b"", _SILENT
        keep, reason = assess_take(pcm, rate, kind, text)
        if reason is not None:
            return b"", reason
        pcm = pcm[:keep]
        pcm = apply_fade_in(pcm, sample_rate=rate, fade_ms=_FADE_IN_MS)
        pcm = apply_fade_out(pcm, sample_rate=rate, fade_ms=_FADE_OUT_MS)
        return normalize_level(
            pcm, target_rms_dbfs=CUE_TARGET_RMS_DBFS.get(kind, -25.0)
        ), None

    @staticmethod
    def _finish(pcm: bytes, rate: int, kind: str, text: str | None = None) -> bytes:
        """The finished clip of one take, or b"" when it is unusable."""
        return VoicedCueLibrary._finish_take(pcm, rate, kind, text)[0]


class VoicedCueSession:
    """Selection state owned by one call; rendering stays on the library.

    Cache methods are bound to the shared library, so background render
    tasks neither hold this session alive nor mutate another call's picks.
    """

    _session_local = True

    def __init__(self, library: VoicedCueLibrary) -> None:
        self._library = library
        self.last_cue_id: str | None = None
        self._last_cues: dict[tuple[str, str], str] = {}

    def __getattr__(self, name: str):
        return getattr(self._library, name)

    def clear_history(self) -> None:
        self.last_cue_id = None
        self._last_cues.clear()

    def clip(
        self, engine: dict | None, language: str, kind: str, sample_rate: int,
        selection: dict | None = None, target_rms_dbfs: float | None = None,
    ) -> bytes:
        if kind not in LADDER_CUE_KINDS or sample_rate <= 0:
            return b""
        choices = self._library.cue_choices(language, kind, selection)
        history_key = (self._library.engine_key(engine, language), kind)
        previous = self._last_cues.get(history_key)
        # Keep the planner's contextual preference order. A repeated cue is
        # still valid if every alternative is unavailable; never wait for a
        # render or substitute a cue excluded by this turn's context.
        choices = [pair for pair in choices if pair[0] != previous] + [
            pair for pair in choices if pair[0] == previous
        ]
        for cue_id, text in choices:
            pcm = self._library._cached_clip(
                engine, language, kind, text, sample_rate, target_rms_dbfs=target_rms_dbfs,
            )
            if pcm:
                self.last_cue_id = cue_id
                self._last_cues[history_key] = cue_id
                return pcm
        return b""


async def default_renderer(
    engine: dict, language: str, text: str, *, sample_rate: int = CUE_RENDER_SAMPLE_RATE,
) -> tuple[bytes, int]:
    """Render ``text`` through the engine's REST TTS provider (one call).

    ``engine["params"]`` carries the reply's synthesis parameters (resolved
    by :func:`shared.providers.tts.delivery.resolve_engine_params`): the
    same pace/speed, temperature, stability… the streamed reply uses, so the
    cue is unmistakably the same voice. Without them the REST render fell
    back to provider defaults (pace 1.0, default temperature) while the
    reply streamed at the bot's speed with temperature 0.01.
    """
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_tts_provider
    from shared.providers.tts.delivery import speed_param_name

    provider_name = engine.get("provider") or "sarvam"
    if provider_name == "mock":
        return b"", 0  # never bill or fake a cue for the mock provider
    params = voice_params(engine)
    speed_key = speed_param_name(provider_name, engine.get("model") or "")
    speed = params.get(speed_key) if speed_key else None
    try:
        speed = float(speed) if speed is not None else 1.0
    except (TypeError, ValueError):
        speed = 1.0
    extra = {key: value for key, value in params.items() if key != speed_key}
    if sample_rate and sample_rate > 0:
        extra["output_sample_rate"] = int(sample_rate)
    provider = get_tts_provider(
        ProviderConfig(
            provider=provider_name,
            model=engine.get("model") or "",
            voice=engine.get("voice") or "",
            language=language or "en",
            api_key_reference=engine.get("api_key_reference") or "",
            extra=extra,
        )
    )
    result = await provider.synthesize(
        text, voice=engine.get("voice") or None, language=language or None, speed=speed,
    )
    return result.audio, int(result.sample_rate)


_library: VoicedCueLibrary | None = None


def get_voiced_cue_library() -> VoicedCueLibrary:
    """Process-wide library (disk cache under ``filler_audio_dir/cache``)."""
    global _library
    if _library is None:
        from shared.config import get_settings

        _library = VoicedCueLibrary(Path(get_settings().filler_audio_dir) / "cache")
    return _library
