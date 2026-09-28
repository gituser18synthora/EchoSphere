"""Background ambience under the outbound call audio.

Natural Conversation → Background ambience (human_speech key
``background_ambience``, default off). A quiet, looping office / call-centre
room sound the caller hears under the bot's speech AND through the silences
between turns, so the call sounds like it comes from a live environment.

Provider-agnostic by construction: nothing here knows which TTS produced the
audio. The mixer works on the output transport's outbound PCM
(``voice_runtime.filler_transport.FillerWebsocketOutputTransport``):

- Bot audio (reply TTS, latency fillers) is mixed with the next slice of the
  loop inside the transport's ``_write_frame`` — AFTER the echo reference
  has been fed the clean frame, BEFORE the serializer encodes it. The echo
  reference never sees synthetic room sound, and the recorder (downstream of
  the transport) keeps receiving the clean bot audio it records today.
- While no bot audio is playing, the transport's media sender emits
  ambience-only frames (:class:`voice_runtime.frames.AmbienceAudioRawFrame`)
  a short lead ahead of playout, on the playout-horizon clock tracked here.
  They ride the tagged-filler wire path: their own packets on telephony
  (never merged into a reply's packet buffer or first-packet ramp), a
  clearable ``filler_audio`` stream on the browser client. They are created
  inside the transport and never enter the pipeline, so they are neither
  recorded nor fed to the echo reference.
- Caller/STT input is never touched.

Which room and how loud come from the call's settings (preset id + 0–100
volume, ``shared.audio.ambience_presets``) and are resolved ONCE, when the
call's mixer is built: the loop is cached per (preset, rate) and per level,
so a call only slices an in-memory int16 array that already carries its
gain. Per chunk the work is one slice, one int32 add and one peak check.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import wave
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from shared.audio.ambience_presets import (
    AMBIENCE_DEFAULT_DB,
    AMBIENCE_PRESETS,
    DEFAULT_AMBIENCE_PRESET,
    ambience_volume_db,
    resolve_ambience_preset,
    resolve_ambience_volume,
)
from shared.audio.pcm import resample_pcm
from voice_runtime.frames import AmbienceAudioRawFrame, FillerAudioOwner

logger = logging.getLogger(__name__)

# ── assets ───────────────────────────────────────────────────────────────
# Procedurally generated, seeded, our own (scripts/generate_ambience_asset.py):
# mono 16-bit PCM WAV, ``<preset asset>_<rate>.wav``. A preset needs only one
# canonical rate; any other rate the output transport runs at (telephony
# 8 kHz; browser 16/22.05/24 kHz) is resampled ONCE at load from the
# highest-rate file. ``office`` also ships pre-rendered 8/16/24 kHz files.
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "ambience"
ASSET_NAME = AMBIENCE_PRESETS[DEFAULT_AMBIENCE_PRESET].asset

# ── level ────────────────────────────────────────────────────────────────
# "Normal speech" on our outbound path: active-speech RMS of the bot channel
# measured over 58 local call recordings (2026-09-28: energy mean p50
# -17.2 dBFS, median 100 ms window -19.4 dBFS; ElevenLabs and Sarvam).
SPEECH_REFERENCE_DBFS = -18.0
# Default ambience level relative to that speech (volume 50); the volume
# scale lives in shared.audio.ambience_presets.
AMBIENCE_LEVEL_DB = AMBIENCE_DEFAULT_DB
AMBIENCE_TARGET_DBFS = SPEECH_REFERENCE_DBFS + AMBIENCE_LEVEL_DB

# ── pacing ───────────────────────────────────────────────────────────────
# Ambience-only chunk: 20 ms is one telephony frame (320 bytes at 8 kHz — no
# padding on any serializer) and keeps the room audio queued ahead of a reply
# on a line that cannot be cleared to at most one chunk plus the lead.
CHUNK_MS = 20
# How far ahead of playout ambience-only audio is kept. Telephony queues
# cannot be selectively cleared, so whatever is queued there plays before the
# reply's first audio: keep it to one frame. The browser client drops queued
# room audio the moment a reply starts (filler_clear), so it can afford a
# lead that rides out network jitter without re-anchoring gaps.
LEAD_MS = {"browser": 60, "telephony": 20}
# Bot audio just finished writing: give the next queued chunk this long to
# arrive before room audio takes over (zero-lead latency-filler clips stream
# chunk by chunk; this keeps room audio from slipping between them).
IDLE_CONFIRM_S = 0.015
# Start once the session's first frame reaches the wire (the browser's
# session_config / the greeting) or after this long, whichever comes first.
START_AFTER_S = 1.0
# The room fades in at call start instead of switching on.
FADE_IN_MS = 400
# Loop closure applied at load time (any asset loops without a click).
LOOP_CROSSFADE_MS = 150
# Chunks up to this long are sliced without wrap handling.
_MAX_FAST_SLICE_S = 0.5
_TIMING_SAMPLES = 4096
# Level-scaled loops kept ready (most calls share a few preset/volume pairs;
# each entry is ~0.4–1.2 MB).
_BED_CACHE_SIZE = 12


@dataclass(frozen=True)
class AmbienceBed:
    """One decoded, level-scaled, seamlessly looping ambience track."""

    samples: np.ndarray        # int16 loop at the target level
    padded: np.ndarray         # loop + its head, for wrap-free slicing
    sample_rate: int
    source: str
    rms_dbfs: float
    preset: str = DEFAULT_AMBIENCE_PRESET

    @property
    def loop_seconds(self) -> float:
        return len(self.samples) / self.sample_rate


# (asset_dir, asset, rate) -> (loop-closed float32 samples at the stored
# level, file name): decoded/resampled once; tiny key space.
_SOURCE_CACHE: dict[tuple, tuple[np.ndarray, str]] = {}
# (asset_dir, preset, rate, target dBFS) -> AmbienceBed, least recently used
# evicted first.
_BED_CACHE: OrderedDict[tuple, AmbienceBed] = OrderedDict()
_BED_LOCK = threading.Lock()


def _read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        if wav.getcomptype() != "NONE" or wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ValueError(f"{path.name}: ambience must be mono 16-bit PCM WAV")
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    if len(pcm) < rate * 2:
        raise ValueError(f"{path.name}: ambience loop shorter than one second")
    return pcm, rate


def _asset_for(asset_dir: Path, name: str, sample_rate: int) -> Path:
    exact = asset_dir / f"{name}_{sample_rate}.wav"
    if exact.is_file():
        return exact
    candidates = []
    for path in asset_dir.glob(f"{name}_*.wav"):
        try:
            candidates.append((int(path.stem.rsplit("_", 1)[1]), path))
        except ValueError:
            continue
    if not candidates:
        raise FileNotFoundError(f"no {name}_<rate>.wav ambience asset in {asset_dir}")
    return max(candidates)[1]


def _close_loop(samples: np.ndarray, fade: int) -> np.ndarray:
    """Equal-power crossfade of the tail into the head: the last sample of
    the result flows into its first, for any source recording."""
    if fade <= 0 or len(samples) <= 2 * fade:
        return samples
    head = samples[:fade]
    tail = samples[-fade:]
    ramp = np.linspace(0.0, np.pi / 2, fade, endpoint=False)
    out = samples[:-fade].copy()
    out[:fade] = head * np.sin(ramp) + tail * np.cos(ramp)
    return out


def _load_source(asset_dir: Path, name: str, sample_rate: int) -> tuple[np.ndarray, str]:
    """A preset's loop at ``sample_rate``, decoded, resampled if needed and
    loop-closed — once per process (call with ``_BED_LOCK`` held)."""
    key = (str(asset_dir), name, sample_rate)
    cached = _SOURCE_CACHE.get(key)
    if cached is not None:
        return cached
    path = _asset_for(asset_dir, name, sample_rate)
    pcm, rate = _read_wav(path)
    if rate != sample_rate:
        pcm = resample_pcm(pcm, rate, sample_rate)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    samples = _close_loop(samples, int(sample_rate * LOOP_CROSSFADE_MS / 1000))
    if float(np.sqrt(np.mean(np.square(samples)))) <= 0.0:
        raise ValueError(f"{path.name}: ambience asset is silent")
    cached = (samples.astype(np.float32), path.name)
    _SOURCE_CACHE[key] = cached
    return cached


def load_ambience_bed(
    sample_rate: int,
    *,
    preset: str = DEFAULT_AMBIENCE_PRESET,
    level_db: float = AMBIENCE_LEVEL_DB,
    level_dbfs: float | None = None,
    asset_dir: Path = ASSET_DIR,
) -> AmbienceBed:
    """The ``preset`` loop at ``sample_rate``, at ``level_db`` below normal
    speech (plus the preset's loudness trim) — or at exactly ``level_dbfs``
    RMS when given. Unknown presets load ``office``.

    Decoding and resampling happen once per (preset, rate); a new level only
    rescales that cached source. Raises OSError/ValueError/wave.Error when
    the asset is missing or unusable (the call then runs without ambience).
    """
    spec = resolve_ambience_preset(preset)
    rate = int(sample_rate)
    target = (
        float(level_dbfs) if level_dbfs is not None
        else SPEECH_REFERENCE_DBFS + float(level_db) + spec.trim_db
    )
    key = (str(asset_dir), spec.id, rate, round(target, 2))
    with _BED_LOCK:
        bed = _BED_CACHE.get(key)
        if bed is not None:
            _BED_CACHE.move_to_end(key)
            return bed
        samples, source = _load_source(asset_dir, spec.asset, rate)
        rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
        gain = (32768.0 * 10 ** (target / 20.0)) / rms
        loop = np.clip(np.rint(samples * gain), -32768, 32767).astype(np.int16)
        pad = min(len(loop), int(rate * _MAX_FAST_SLICE_S))
        padded = np.concatenate([loop, loop[:pad]])
        actual = 20 * np.log10(np.sqrt(np.mean(np.square(loop.astype(np.float64)))) / 32768.0)
        bed = AmbienceBed(
            samples=loop, padded=padded, sample_rate=rate,
            source=source, rms_dbfs=round(float(actual), 2), preset=spec.id,
        )
        _BED_CACHE[key] = bed
        while len(_BED_CACHE) > _BED_CACHE_SIZE:
            _BED_CACHE.popitem(last=False)
        logger.info(
            "background ambience loaded: %s (%s) → %d Hz, %.1f s loop, %.1f dBFS RMS",
            spec.id, source, rate, bed.loop_seconds, bed.rms_dbfs,
        )
        return bed


def ambience_enabled(human_speech: dict | None) -> bool:
    """Bot/tenant setting on AND the Human speech layer on (the layer is the
    master of every Natural Conversation behaviour). Absent key = off."""
    settings = human_speech or {}
    return settings.get("enabled", True) is not False and settings.get("background_ambience") is True


class AmbienceMixer:
    """One call's position in the loop, its playout clock and its stats.

    Used only from the output transport's event-loop task(s); not
    thread-safe (it does not need to be).
    """

    def __init__(
        self,
        bed: AmbienceBed,
        *,
        lead_s: float,
        transport_kind: str = "",
        chunk_ms: int = CHUNK_MS,
        fade_in_ms: int = FADE_IN_MS,
        start_after_s: float = START_AFTER_S,
        start_offset: int | None = None,
        recorder=None,
        clock=time.monotonic,
        volume: int | None = None,
        level_db: float | None = None,
    ) -> None:
        self.bed = bed
        self.sample_rate = bed.sample_rate
        self.transport_kind = transport_kind
        self.volume = volume
        self.level_db = AMBIENCE_LEVEL_DB if level_db is None else float(level_db)
        self.chunk_samples = max(1, int(self.sample_rate * chunk_ms / 1000))
        self.chunk_seconds = self.chunk_samples / self.sample_rate
        self.lead_s = max(0.0, float(lead_s))
        self._loop_len = len(bed.samples)
        self._cursor = (
            random.randrange(self._loop_len) if start_offset is None
            else int(start_offset) % self._loop_len
        )
        self._fade_total = int(self.sample_rate * fade_in_ms / 1000)
        self._faded = 0
        self._start_after_s = float(start_after_s)
        self._start_deadline: float | None = None
        self._clock = clock
        self._recorder = recorder
        self.owner = FillerAudioOwner(turn_id=-1)
        self.running = True
        self.armed = False
        # Playout horizon: when the far end finishes everything sent so far.
        self.horizon = 0.0
        # Ambience-only audio went out since the last bot audio (a browser
        # client can drop it the moment a reply starts).
        self.idle_since_audio = False
        # Bot audio was written since the last ambience-only chunk (a
        # packetizing telephony serializer may still hold its tail).
        self.audio_since_idle = False
        self._stop_reason = ""
        # stats
        self._mixed_chunks = 0
        self._idle_chunks = 0
        self._handoffs = 0
        self._remnant_flushes = 0
        self._interruptions = 0
        self._saturated = 0
        # Chunks where the room was lowered so speech near full scale plus
        # room could not clip (loud TTS, top volumes).
        self._guarded = 0
        # Room chunks that reached the far end after it had run dry (an
        # audible gap in the room sound), and how much silence that was.
        self._late_chunks = 0
        self._late_ms = 0.0
        self._restart = True   # the next room chunk follows a flush/start
        self._mix_ns: deque[int] = deque(maxlen=_TIMING_SAMPLES)
        self._mix_ns_max = 0
        self._mix_ns_total = 0
        self._started_at = clock()

    # ── lifecycle ────────────────────────────────────────────────────────
    def begin(self) -> None:
        """The transport's audio task started: arm by the start deadline."""
        if self._start_deadline is None:
            self._start_deadline = self._clock() + self._start_after_s

    def arm(self) -> None:
        """The session's first frame reached the wire."""
        self.armed = True

    def stop(self, reason: str = "end") -> None:
        """No more room audio from here on (call ending). Idempotent."""
        if not self.running:
            return
        self.running = False
        self._stop_reason = reason
        if self._recorder is not None:
            try:
                self._recorder.add_event("background_ambience", **self.stats())
            except Exception:  # noqa: BLE001 — telemetry must never break teardown
                logger.debug("ambience stats event failed", exc_info=True)

    # ── playout clock ────────────────────────────────────────────────────
    def note_sent(self, seconds: float, now: float | None = None) -> None:
        """``seconds`` of audio left for the far end right now."""
        if seconds <= 0:
            return
        now = self._clock() if now is None else now
        self.horizon = max(self.horizon, now) + seconds

    def note_room_sent(self, seconds: float, now: float | None = None) -> None:
        """An ambience-only chunk left now; counts it if the far end had
        already run dry (never for the first chunk after a start or flush)."""
        now = self._clock() if now is None else now
        dry_for = now - self.horizon
        if not self._restart and dry_for > 0.002:
            self._late_chunks += 1
            self._late_ms += dry_for * 1000.0
        self._restart = False
        self.note_sent(seconds, now)

    def note_flushed(self, now: float | None = None) -> None:
        """The far end dropped everything queued (clear / interruption)."""
        self.horizon = self._clock() if now is None else now
        self._restart = True

    def note_interruption(self, now: float | None = None) -> None:
        """Barge-in: the far end's queue was cleared; the browser client also
        retires every owner it knew, so the room continues under a new one."""
        self._interruptions += 1
        self.note_flushed(now)
        self.owner = FillerAudioOwner(turn_id=-1)
        self.idle_since_audio = False
        self.audio_since_idle = False

    def note_handoff(self, cleared: bool, now: float | None = None) -> None:
        """Bot audio is about to follow room audio. ``cleared``: the client
        dropped the queued room audio, so the reply starts at once."""
        self._handoffs += 1
        self.idle_since_audio = False
        self.owner = FillerAudioOwner(turn_id=-1)  # a cleared owner stays retired
        if cleared:
            self.note_flushed(now)

    def note_remnant_flushed(self) -> None:
        self._remnant_flushes += 1

    def idle_wait(self, now: float, *, last_audio_at: float = 0.0) -> float | None:
        """Seconds until the next ambience-only chunk is due; None = none
        (stopped). Queued bot audio always wins over this deadline."""
        if not self.running:
            return None
        if not self.armed:
            if self._start_deadline is None:
                return None
            if now < self._start_deadline:
                return self._start_deadline - now
            self.armed = True
        due = max(self.horizon - self.lead_s, last_audio_at + IDLE_CONFIRM_S)
        return max(0.0, due - now)

    # ── audio ────────────────────────────────────────────────────────────
    def _take(self, n: int) -> np.ndarray:
        """The next ``n`` loop samples (int16), fade-in applied."""
        start = self._cursor
        if n <= len(self.bed.padded) - self._loop_len:
            chunk = self.bed.padded[start:start + n]
        else:
            chunk = np.take(self.bed.samples, np.arange(start, start + n), mode="wrap")
        self._cursor = (start + n) % self._loop_len
        if self._faded < self._fade_total:
            count = min(n, self._fade_total - self._faded)
            ramp = (np.arange(self._faded, self._faded + count, dtype=np.float32) + 1.0) / self._fade_total
            head = np.rint(chunk[:count].astype(np.float32) * ramp).astype(np.int16)
            chunk = np.concatenate([head, chunk[count:]])
            self._faded += count
        return chunk

    def mix(self, pcm: bytes) -> bytes:
        """``pcm`` (int16 mono at the loop rate) with the room sound added."""
        if not self.running or len(pcm) < 2:
            return pcm
        started = time.perf_counter_ns()
        n = len(pcm) // 2
        voice = np.frombuffer(pcm, dtype="<i2", count=n)
        room = self._take(n)
        out = voice.astype(np.int32)
        out += room
        if out.max() > 32767 or out.min() < -32768:
            # Speech near full scale: lower the room for this one chunk just
            # enough that the sum fits — never clip (the speech masks the
            # room completely at that level anyway).
            voice32 = voice.astype(np.int32)
            headroom = 32767 - int(np.abs(voice32).max())
            peak = int(np.abs(room.astype(np.int32)).max()) or 1
            scale = min(1.0, max(0.0, headroom / peak))
            out = voice32 + np.trunc(room.astype(np.float32) * scale).astype(np.int32)
            self._guarded += 1
            if out.max() > 32767 or out.min() < -32768:
                over = out > 32767
                under = out < -32768
                self._saturated += int(np.count_nonzero(over) + np.count_nonzero(under))
                np.clip(out, -32768, 32767, out=out)
        mixed = out.astype("<i2").tobytes()
        if len(pcm) % 2:
            mixed += pcm[-1:]
        elapsed = time.perf_counter_ns() - started
        self._mixed_chunks += 1
        self._mix_ns.append(elapsed)
        self._mix_ns_total += elapsed
        self._mix_ns_max = max(self._mix_ns_max, elapsed)
        return mixed

    def idle_frame(self, *, flush_pending: bool) -> AmbienceAudioRawFrame:
        """One ambience-only chunk (room sound over silence)."""
        self._idle_chunks += 1
        return AmbienceAudioRawFrame(
            audio=self._take(self.chunk_samples).astype("<i2").tobytes(),
            sample_rate=self.sample_rate,
            num_channels=1,
            owner=self.owner,
            flush_pending=flush_pending,
        )

    # ── telemetry ────────────────────────────────────────────────────────
    def stats(self) -> dict:
        timings = sorted(self._mix_ns)

        def pct(q: float) -> float | None:
            if not timings:
                return None
            return round(timings[min(len(timings) - 1, int(q * len(timings)))] / 1000.0, 1)

        return {
            "preset": self.bed.preset,
            "asset": self.bed.source,
            "volume": self.volume,
            "sample_rate": self.sample_rate,
            "transport": self.transport_kind,
            "level_dbfs": self.bed.rms_dbfs,
            "level_db_rel_speech": round(self.level_db, 2),
            "guarded_chunks": self._guarded,
            "lead_ms": round(self.lead_s * 1000),
            "chunk_ms": round(self.chunk_seconds * 1000),
            "mixed_chunks": self._mixed_chunks,
            "idle_chunks": self._idle_chunks,
            "handoffs": self._handoffs,
            "remnant_flushes": self._remnant_flushes,
            "interruptions": self._interruptions,
            "saturated_samples": self._saturated,
            "late_chunks": self._late_chunks,
            "late_ms": round(self._late_ms, 1),
            "mix_us_p50": pct(0.50),
            "mix_us_p95": pct(0.95),
            "mix_us_p99": pct(0.99),
            "mix_us_max": round(self._mix_ns_max / 1000.0, 1),
            "mix_us_mean": (
                round(self._mix_ns_total / self._mixed_chunks / 1000.0, 1)
                if self._mixed_chunks else None
            ),
            "seconds": round(self._clock() - self._started_at, 1),
            "stop_reason": self._stop_reason or None,
        }


def build_ambience(
    config,
    *,
    transport_kind: str,
    sample_rate: int,
    recorder=None,
) -> AmbienceMixer | None:
    """The call's ambience mixer, or None when the setting is off or the
    volume is 0 (the transport then runs exactly as it always has), or when
    the asset is unusable.

    Preset and volume are resolved here, once per call: a missing or unknown
    preset is ``office``, a missing or malformed volume is the default (the
    original level), an out-of-range one is clamped.
    """
    settings = getattr(config, "human_speech", None) or {}
    if not ambience_enabled(settings):
        return None
    volume = resolve_ambience_volume(settings.get("background_ambience_volume"))
    level_db = ambience_volume_db(volume)
    if level_db is None:
        return None  # muted: no room audio at all, not a stream of zeros
    preset = resolve_ambience_preset(settings.get("background_ambience_preset"))
    try:
        bed = load_ambience_bed(int(sample_rate), preset=preset.id, level_db=level_db)
    except (OSError, ValueError, wave.Error) as exc:
        logger.warning("background ambience unavailable (%s); call continues without it", exc)
        if recorder is not None:
            recorder.add_event("background_ambience_unavailable", reason=str(exc)[:160])
        return None
    lead_ms = LEAD_MS.get(transport_kind, LEAD_MS["telephony"])
    return AmbienceMixer(
        bed, lead_s=lead_ms / 1000.0, transport_kind=transport_kind, recorder=recorder,
        volume=volume, level_db=level_db,
    )
