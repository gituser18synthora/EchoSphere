"""Voiced latency cues: "हम्म…" / "एक सेकंड…" in the bot's own voice, pre-rendered.

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
from shared.orchestration.naturalness import LADDER_CUE_KINDS, ladder_cue

logger = logging.getLogger(__name__)

# Below the reply's level: a cue is a beat, not a sentence. Providers differ
# widely in loudness (Sarvam peaks near 0 dBFS), so cues are normalized to a
# target RMS rather than lowered by a relative amount; TTS speech sits around
# -18..-20 dBFS RMS.
CUE_TARGET_RMS_DBFS = {"hmm": -26.0, "wait": -24.0}
CUE_PEAK_CEILING_DBFS = -10.0
# Longest a cue may run; a provider that pads a short text with a long tail
# is trimmed to this after silence trimming.
_MAX_CUE_MS = {"hmm": 900, "wait": 1400}
_TRIM_THRESHOLD_DBFS = -45.0
_FAILURE_COOLDOWN_S = 300.0
_RENDER_TIMEOUT_S = 12.0


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

    def __init__(self, cache_dir: str | Path | None = None, *, renderer=None) -> None:
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._renderer = renderer or default_renderer
        # key -> (pcm, native_rate); b"" marks "rendered, nothing usable".
        self._clips: dict[str, tuple[bytes, int]] = {}
        self._resampled: dict[tuple[str, int], bytes] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._failed_at: dict[str, float] = {}
        self.renders = 0
        self.render_failures = 0

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

    def _key(self, engine: dict | None, language: str, kind: str) -> str:
        text = ladder_cue(language, kind)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        return f"{self.engine_key(engine, language)}_{kind}_{digest}"

    def _disk_path(self, key: str) -> Path | None:
        return (self._cache_dir / f"{key}.wav") if self._cache_dir is not None else None

    # -- public API -----------------------------------------------------

    def clip(self, engine: dict | None, language: str, kind: str, sample_rate: int) -> bytes:
        """The cached clip at ``sample_rate``, or b"" when none is ready.

        Never blocks: a missing clip schedules a background render (once)
        and returns nothing for this turn.
        """
        if kind not in LADDER_CUE_KINDS or sample_rate <= 0 or not ladder_cue(language, kind):
            return b""
        key = self._key(engine, language, kind)
        cached = self._clips.get(key)
        if cached is None:
            cached = self._load_from_disk(key)
        if cached is None:
            self._schedule_render(key, engine, language, kind)
            return b""
        pcm, rate = cached
        if not pcm:
            return b""
        if rate == sample_rate:
            return pcm
        out = self._resampled.get((key, sample_rate))
        if out is None:
            out = resample_pcm(pcm, rate, sample_rate)
            self._resampled[(key, sample_rate)] = out
        return out

    def ready(self, engine: dict | None, language: str, kind: str) -> bool:
        key = self._key(engine, language, kind)
        cached = self._clips.get(key)
        if cached is None:
            cached = self._load_from_disk(key)
        return bool(cached and cached[0])

    def warm(self, engine: dict | None, language: str) -> None:
        """Start rendering every cue of ``language`` for ``engine`` that is
        not cached yet (fire-and-forget; safe to call per turn)."""
        for kind in LADDER_CUE_KINDS:
            if ladder_cue(language, kind):
                self.clip(engine, language, kind, 16000)

    async def wait_ready(self, engine: dict | None, language: str, timeout: float = 15.0) -> None:
        """Await pending renders for ``language`` (tests / warm-up scripts)."""
        keys = [self._key(engine, language, kind) for kind in LADDER_CUE_KINDS]
        pending = [self._tasks[k] for k in keys if k in self._tasks and not self._tasks[k].done()]
        if pending:
            await asyncio.wait(pending, timeout=timeout)

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

    def _schedule_render(self, key: str, engine: dict | None, language: str, kind: str) -> None:
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
        self._tasks[key] = loop.create_task(self._render(key, dict(engine or {}), language, kind))

    async def _render(self, key: str, engine: dict, language: str, kind: str) -> None:
        text = ladder_cue(language, kind)
        try:
            pcm, rate = await asyncio.wait_for(
                self._renderer(engine, language, text), timeout=_RENDER_TIMEOUT_S
            )
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


async def default_renderer(engine: dict, language: str, text: str) -> tuple[bytes, int]:
    """Render ``text`` through the engine's REST TTS provider (one call)."""
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_tts_provider

    provider_name = engine.get("provider") or "sarvam"
    if provider_name == "mock":
        return b"", 0  # never bill or fake a cue for the mock provider
    provider = get_tts_provider(
        ProviderConfig(
            provider=provider_name,
            model=engine.get("model") or "",
            voice=engine.get("voice") or "",
            language=language or "en",
            api_key_reference=engine.get("api_key_reference") or "",
        )
    )
    result = await provider.synthesize(
        text, voice=engine.get("voice") or None, language=language or None
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
