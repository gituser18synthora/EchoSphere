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
# Longest a cue may run; a provider that pads a short text with a long tail
# is trimmed to this after silence trimming.
_MAX_CUE_MS = {"hmm": 900, "wait": 1400}
_TRIM_THRESHOLD_DBFS = -45.0
_FAILURE_COOLDOWN_S = 300.0
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
        # The cue id most recently handed out (telemetry).
        self.last_cue_id: str | None = None
        self.renders = 0
        self.render_failures = 0

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
        # The voice parameters are part of the identity: a bot at speed 1.2
        # with temperature 0.01 must not play a cue cached for defaults, and
        # two bots sharing a speaker but not a delivery must not share clips.
        params = voice_params(engine)
        material = text if not params else text + "\x1f" + json.dumps(params, sort_keys=True, default=str)
        digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
        suffix = "" if not params else "_p"
        return f"{self.engine_key(engine, language)}_{kind}_{digest}{suffix}"

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
        if path is None or not path.is_file():
            return None
        try:
            pcm, rate = wav_to_pcm(path.read_bytes())
        except (OSError, ValueError):
            logger.warning("voiced-cues: unreadable cache file %s; re-rendering", path)
            return None
        if not pcm or rate <= 0:
            return None
        self._clips[key] = (pcm, rate)
        return self._clips[key]

    def _schedule_render(
        self, key: str, engine: dict | None, language: str, kind: str, text: str | None = None,
    ) -> None:
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return
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

    async def _render(
        self, key: str, engine: dict, language: str, kind: str, text: str | None = None,
    ) -> None:
        text = ladder_cue(language, kind) if text is None else text
        try:
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
        except Exception:  # noqa: BLE001 — a cue is decoration; never fatal
            self.render_failures += 1
            self._failed_at[key] = time.monotonic()
            logger.warning(
                "voiced-cues: render failed for %s (%s %s/%s)",
                key, kind, engine.get("provider"), engine.get("voice"), exc_info=True,
            )
            return
        pcm = self._finish(pcm or b"", int(rate or 0), kind)
        self._clips[key] = (pcm, int(rate or 0))
        self.renders += 1
        if not pcm:
            logger.info("voiced-cues: %s rendered to silence; cue disabled for this voice", key)
            return
        path = self._disk_path(key)
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(pcm_to_wav_bytes(pcm, sample_rate=int(rate)))
            except OSError:
                logger.debug("voiced-cues: could not cache %s", path, exc_info=True)
        logger.info(
            "voiced-cues: rendered %s (%.0f ms)", key, len(pcm) / (int(rate) * 2) * 1000.0
        )

    @staticmethod
    def _finish(pcm: bytes, rate: int, kind: str) -> bytes:
        if not pcm or rate <= 0:
            return b""
        pcm = trim_silence(pcm, rate)
        if not pcm:
            return b""
        limit = int(rate * _MAX_CUE_MS.get(kind, 1000) / 1000) * 2
        if len(pcm) > limit:
            pcm = pcm[:limit]
        pcm = apply_fade_in(pcm, sample_rate=rate, fade_ms=10)
        pcm = apply_fade_out(pcm, sample_rate=rate, fade_ms=40)
        return normalize_level(
            pcm, target_rms_dbfs=CUE_TARGET_RMS_DBFS.get(kind, -25.0)
        )


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
