"""Generate the background ambience presets used by ``voice_runtime.ambience``
(Natural Conversation → Call environment).

Every preset is synthesized procedurally from its own fixed seed, so the
audio is our own (no third-party recording, no licence) and byte-for-byte
reproducible. The building blocks:

- room tone: band-limited pink noise with a slow, subtle level drift;
- distant murmur: formant-synthesized talkers (glottal source, vowel formant
  glides at a syllabic rate, talk spurts and pauses) smeared by a diffuse
  reverb. There are no consonants, so no word can ever be made out;
- keyboard typing from several desks at different distances;
- office events: footsteps, chair rolls, paper rustles.

Presets differ in which blocks they use, how many talkers, how near, how
reverberant the room, how busy the typing and the events (see ``PRESETS``).

Each preset is written as a seamless loop (equal-power crossfade of the tail
into the head), mono 16-bit PCM WAV, stored at -30 dBFS RMS (the runtime
scales to the configured level). ``office`` keeps its three pre-rendered
rates (the accepted baseline, byte-identical); the other presets are stored
once at 16 kHz — their content sits below 8 kHz by construction — and the
runtime resamples them once at load for 8 / 22.05 / 24 kHz.

    env/bin/python scripts/generate_ambience_asset.py [--out DIR] [--preset NAME ...]

It also prints each preset's loudness trim (see
``shared.audio.ambience_presets``): the dB that makes the preset as loud as
``office`` over the phone band at the same volume setting.
"""

from __future__ import annotations

import argparse
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import butter, fftconvolve, lfilter, resample_poly, sosfilt

MASTER_RATE = 24_000
CROSSFADE_SECONDS = 1.0
STORED_RMS_DBFS = -30.0
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "voice_runtime" / "assets" / "ambience"

# (F1, F2, F3) in Hz — rough adult vowel targets; the talkers glide between
# them at a syllabic rate, which is what makes the murmur read as speech.
_VOWELS = np.array([
    (730, 1090, 2440),  # a
    (530, 1840, 2480),  # e
    (270, 2290, 3010),  # i
    (570, 840, 2410),   # o
    (300, 870, 2240),   # u
    (500, 1500, 2500),  # schwa
], dtype=np.float64)
_BANDWIDTHS = (90.0, 110.0, 160.0)


def _db(value: float) -> float:
    return 10 ** (value / 20.0)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x)) + 1e-20))


def _pink(n: int, rng: np.random.Generator) -> np.ndarray:
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1.0 / MASTER_RATE)
    freqs[0] = freqs[1]
    spectrum /= np.sqrt(freqs)
    out = np.fft.irfft(spectrum, n)
    return out / _rms(out)


def _bandpass(x: np.ndarray, lo: float, hi: float, order: int = 2) -> np.ndarray:
    sos = butter(order, [lo, hi], btype="band", fs=MASTER_RATE, output="sos")
    return sosfilt(sos, x)


def _lowpass(x: np.ndarray, cutoff: float, order: int = 2) -> np.ndarray:
    sos = butter(order, cutoff, btype="low", fs=MASTER_RATE, output="sos")
    return sosfilt(sos, x)


def _highpass(x: np.ndarray, cutoff: float, order: int = 2) -> np.ndarray:
    sos = butter(order, cutoff, btype="high", fs=MASTER_RATE, output="sos")
    return sosfilt(sos, x)


def _diffuse(x: np.ndarray, rng: np.random.Generator, rt60: float, wet: float) -> np.ndarray:
    """A short diffuse room: exponentially decaying noise impulse response."""
    ir_len = int(rt60 * MASTER_RATE)
    decay = np.exp(-6.9 * np.arange(ir_len) / ir_len)
    ir = rng.standard_normal(ir_len) * decay
    ir /= np.sqrt(np.sum(ir ** 2))
    return (1.0 - wet) * x + wet * fftconvolve(x, ir)[: len(x)]


# ── building blocks ──────────────────────────────────────────────────────

def room_tone(
    n: int, rng: np.random.Generator, *,
    band: tuple[float, float] = (180.0, 5000.0),
    dark: float = 0.4, dark_cutoff: float = 1600.0,
    drift: tuple[float, float] = (0.06, 0.04),
    drift_hz: tuple[float, float] = (0.071, 0.133),
) -> np.ndarray:
    """Soft broadband air: pink noise, band-limited, darker above ``dark_cutoff``.

    Nothing below ~180 Hz: the phone band starts at 300 Hz and small
    speakers do not reproduce it, so low rumble would only inflate the RMS
    the runtime levels against without being heard.
    """
    x = _bandpass(_pink(n, rng), *band)
    x = (1.0 - dark) * x + dark * _lowpass(x, dark_cutoff, order=1)
    t = np.arange(n) / MASTER_RATE
    level = (
        1.0
        + drift[0] * np.sin(2 * np.pi * drift_hz[0] * t + rng.uniform(0, 2 * np.pi))
        + drift[1] * np.sin(2 * np.pi * drift_hz[1] * t + rng.uniform(0, 2 * np.pi))
    )
    return x * level


def ventilation(n: int, rng: np.random.Generator) -> np.ndarray:
    """Air-handler hush: a broad, slowly breathing hump around 250–700 Hz."""
    x = _bandpass(_pink(n, rng), 250.0, 700.0, order=1)
    t = np.arange(n) / MASTER_RATE
    return x * (1.0 + 0.05 * np.sin(2 * np.pi * 0.021 * t + rng.uniform(0, 2 * np.pi)))


def _resonator(freq: float, bandwidth: float) -> tuple[np.ndarray, np.ndarray]:
    r = np.exp(-np.pi * bandwidth / MASTER_RATE)
    theta = 2 * np.pi * freq / MASTER_RATE
    a = np.array([1.0, -2.0 * r * np.cos(theta), r * r])
    b = np.array([1.0 - r])  # rough gain normalisation
    return b, a


def _talker(
    n: int, rng: np.random.Generator, *,
    talk: tuple[float, float] = (1.2, 4.5),
    pause: tuple[float, float] = (0.4, 3.0),
    first: tuple[float, float] = (0.0, 2.5),
) -> np.ndarray:
    """One talker: talk spurts of formant-filtered glottal pulses."""
    female = rng.random() < 0.5
    f0_base = rng.uniform(175.0, 235.0) if female else rng.uniform(95.0, 140.0)
    formant_scale = 1.12 if female else 1.0

    # Talk spurts / pauses → a 0..1 activity mask with soft edges.
    activity = np.zeros(n)
    pos = int(rng.uniform(*first) * MASTER_RATE)
    while pos < n:
        spurt = int(rng.uniform(*talk) * MASTER_RATE)
        activity[pos:pos + spurt] = 1.0
        pos += spurt + int(rng.uniform(*pause) * MASTER_RATE)
    edge = int(0.08 * MASTER_RATE)
    activity = np.convolve(activity, np.hanning(edge) / np.hanning(edge).sum(), mode="same")

    # Syllables: vowel targets + per-syllable amplitude envelope.
    syllable_env = np.zeros(n)
    targets = np.zeros((n, 3))
    pos = 0
    previous = _VOWELS[rng.integers(len(_VOWELS))]
    while pos < n:
        length = int(rng.uniform(0.13, 0.26) * MASTER_RATE)
        vowel = _VOWELS[rng.integers(len(_VOWELS))] * formant_scale
        end = min(n, pos + length)
        span = end - pos
        glide = np.linspace(0.0, 1.0, span)[:, None]
        targets[pos:end] = previous + (vowel - previous) * np.minimum(1.0, glide * 2.5)
        shape = np.sin(np.pi * np.linspace(0.0, 1.0, span)) ** 0.7
        syllable_env[pos:end] = shape * rng.uniform(0.55, 1.0)
        previous = vowel
        pos = end

    # Glottal source: sawtooth from a jittered, slowly intonated f0 + breath.
    t = np.arange(n) / MASTER_RATE
    intonation = 1.0 + 0.12 * np.sin(2 * np.pi * rng.uniform(0.15, 0.4) * t + rng.uniform(0, 6.3))
    jitter = 1.0 + 0.01 * _lowpass(rng.standard_normal(n), 30.0, order=1) * 8
    phase = np.cumsum(f0_base * intonation * jitter / MASTER_RATE)
    source = 2.0 * (phase % 1.0) - 1.0
    source = _lowpass(source, 900.0, order=1)  # glottal spectral tilt
    source += 0.08 * _highpass(rng.standard_normal(n), 1500.0)

    # Time-varying formant cascade, 5 ms blocks with carried filter state.
    block = int(0.005 * MASTER_RATE)
    out = np.empty(n)
    states = [np.zeros(2) for _ in range(3)]
    for start in range(0, n, block):
        stop = min(n, start + block)
        seg = source[start:stop]
        centre = targets[(start + stop) // 2]
        for i in range(3):
            b, a = _resonator(float(centre[i]), _BANDWIDTHS[i])
            seg, states[i] = lfilter(b, a, seg, zi=states[i])
        out[start:stop] = seg
    return out * syllable_env * activity


def distant_murmur(
    n: int, rng: np.random.Generator, talkers: int = 8, *,
    level_range: tuple[float, float] = (-11.0, -3.0),
    band: tuple[float, float] = (180.0, 3400.0),
    lowpass: float = 1800.0,
    rt60: float = 0.45, wet: float = 0.65,
    talk: tuple[float, float] = (1.2, 4.5),
    pause: tuple[float, float] = (0.4, 3.0),
    first: tuple[float, float] = (0.0, 2.5),
) -> np.ndarray:
    mix = np.zeros(n)
    for _ in range(talkers):
        voice = _talker(n, rng, talk=talk, pause=pause, first=first)
        voice /= _rms(voice) or 1.0
        mix += voice * _db(rng.uniform(*level_range))
    # Distance: muffled + a short diffuse room.
    mix = _lowpass(_bandpass(mix, *band), lowpass)
    ir_len = int(rt60 * MASTER_RATE)
    decay = np.exp(-6.9 * np.arange(ir_len) / ir_len)
    ir = rng.standard_normal(ir_len) * decay
    ir /= np.sqrt(np.sum(ir ** 2))
    reverberant = fftconvolve(mix, ir)[:n]
    return (1.0 - wet) * mix + wet * reverberant


def _keystroke(rng: np.random.Generator, *, space: bool) -> np.ndarray:
    length = int((0.05 if space else 0.035) * MASTER_RATE)
    t = np.arange(length) / MASTER_RATE
    click = _highpass(rng.standard_normal(length), 2200.0) * np.exp(-t / 0.0025)
    thock = _bandpass(rng.standard_normal(length), 350.0, 1100.0) * np.exp(-t / (0.018 if space else 0.011))
    ring_f = rng.uniform(2000.0, 3800.0)
    ring = np.sin(2 * np.pi * ring_f * t) * np.exp(-t / 0.004)
    stroke = click * (0.5 if space else 1.0) + thock * (1.0 if space else 0.6) + 0.15 * ring
    return stroke / (np.max(np.abs(stroke)) or 1.0)


_OFFICE_DESKS = ((-2.0, 6000.0), (-7.0, 3500.0), (-12.0, 2200.0))


def keyboard(
    n: int, rng: np.random.Generator, *,
    desks: tuple[tuple[float, float], ...] = _OFFICE_DESKS,
    keys_per_run: tuple[int, int] = (5, 26),
    first: tuple[float, float] = (0.5, 6.0),
    gap: tuple[float, float] = (3.5, 10.0),
) -> np.ndarray:
    """Desks at different distances: typing runs at human rhythm, separated
    by quiet stretches."""
    out = np.zeros(n)
    for gain_db, cutoff in desks:
        desk = np.zeros(n)
        pos = int(rng.uniform(*first) * MASTER_RATE)
        while pos < n:
            keys = int(rng.integers(*keys_per_run))
            for k in range(keys):
                space = rng.random() < 0.18
                stroke = _keystroke(rng, space=space) * rng.uniform(0.55, 1.0)
                end = min(n, pos + len(stroke))
                desk[pos:end] += stroke[: end - pos]
                release = pos + int(rng.uniform(0.06, 0.09) * MASTER_RATE)
                if release < n:
                    tail = _keystroke(rng, space=False) * rng.uniform(0.15, 0.3)
                    rend = min(n, release + len(tail))
                    desk[release:rend] += tail[: rend - release]
                pos += int(rng.lognormal(np.log(0.15), 0.35) * MASTER_RATE)
                if pos >= n:
                    break
            pos += int(rng.uniform(*gap) * MASTER_RATE)
        out += _lowpass(desk, cutoff) * _db(gain_db)
    return out


def _place(track: np.ndarray, clip: np.ndarray, pos: int) -> None:
    if pos >= len(track):
        return
    end = min(len(track), pos + len(clip))
    track[pos:end] += clip[: end - pos]


def _footstep(rng: np.random.Generator) -> np.ndarray:
    length = int(0.09 * MASTER_RATE)
    t = np.arange(length) / MASTER_RATE
    thump = _bandpass(rng.standard_normal(length), 60.0, 380.0) * np.exp(-t / 0.018)
    scuff = _bandpass(rng.standard_normal(length), 900.0, 3000.0) * np.exp(-t / 0.008) * 0.25
    step = thump + scuff
    return step / (np.max(np.abs(step)) or 1.0)


def _chair_roll(rng: np.random.Generator) -> np.ndarray:
    length = int(rng.uniform(0.5, 1.3) * MASTER_RATE)
    t = np.arange(length) / MASTER_RATE
    rumble = _bandpass(rng.standard_normal(length), 150.0, 900.0)
    clatter = 1.0 + 0.5 * np.sin(2 * np.pi * rng.uniform(8.0, 14.0) * t)
    envelope = np.sin(np.pi * np.linspace(0.0, 1.0, length)) ** 0.6
    roll = rumble * clatter * envelope
    return roll / (np.max(np.abs(roll)) or 1.0)


def _paper_rustle(rng: np.random.Generator) -> np.ndarray:
    length = int(rng.uniform(0.3, 0.9) * MASTER_RATE)
    hiss = _bandpass(rng.standard_normal(length), 1500.0, 6500.0)
    crackle = np.zeros(length)
    crackle[rng.integers(0, length, size=max(3, length // 400))] = rng.uniform(0.3, 1.0, size=max(3, length // 400))
    crackle = np.convolve(crackle, np.hanning(int(0.012 * MASTER_RATE)), mode="same")
    envelope = np.sin(np.pi * np.linspace(0.0, 1.0, length)) ** 0.8
    rustle = hiss * (0.35 + crackle) * envelope
    return rustle / (np.max(np.abs(rustle)) or 1.0)


def office_events(
    n: int, rng: np.random.Generator, *,
    walks_per_min: float = 4.0, chairs_per_min: float = 3.0, papers_per_min: float = 5.0,
    distance_lowpass: float = 3000.0, rt60: float = 0.5,
) -> np.ndarray:
    """Footsteps passing by, chairs rolling, paper — at desk distance."""
    track = np.zeros(n)
    seconds = n / MASTER_RATE

    def times(per_min: float) -> list[int]:
        count = rng.poisson(per_min * seconds / 60.0)
        return sorted(int(v) for v in rng.uniform(0.0, n, size=count))

    for start in times(walks_per_min):
        steps = int(rng.integers(6, 15))
        interval = rng.uniform(0.45, 0.6)
        level = rng.uniform(0.35, 1.0)
        for i in range(steps):
            near = np.sin(np.pi * (i + 0.5) / steps)  # approaching, passing, leaving
            jitter = int(rng.normal(0.0, 0.012) * MASTER_RATE)
            _place(track, _footstep(rng) * level * (0.25 + 0.75 * near),
                   start + int(i * interval * MASTER_RATE) + jitter)
    for start in times(chairs_per_min):
        _place(track, _chair_roll(rng) * rng.uniform(0.3, 0.8), start)
    for start in times(papers_per_min):
        _place(track, _paper_rustle(rng) * rng.uniform(0.25, 0.7), start)
    return _diffuse(_lowpass(track, distance_lowpass), rng, rt60, 0.4)


def seamless_loop(x: np.ndarray, loop_n: int, fade_n: int) -> np.ndarray:
    """``x`` holds ``loop_n + fade_n`` samples; fold the tail into the head
    with an equal-power crossfade so sample ``loop_n - 1`` flows into 0."""
    out = x[:loop_n].copy()
    ramp = np.linspace(0.0, np.pi / 2, fade_n, endpoint=False)
    out[:fade_n] = x[:fade_n] * np.sin(ramp) + x[loop_n:loop_n + fade_n] * np.cos(ramp)
    return out


def _unit(x: np.ndarray) -> np.ndarray:
    return x / (_rms(x) or 1.0)


# ── presets ──────────────────────────────────────────────────────────────

def _office(n: int, rng: np.random.Generator) -> np.ndarray:
    """The accepted baseline (unchanged): steady air, a few distant
    colleagues, keyboard typing from three desks."""
    room = room_tone(n, rng)
    murmur = distant_murmur(n, rng)
    keys = keyboard(n, rng)
    room /= _rms(room)
    murmur /= _rms(murmur)
    keys_rms = _rms(keys) or 1.0
    return room * _db(0.0) + murmur * _db(-3.5) + keys / keys_rms * _db(-9.0)


def _call_center(n: int, rng: np.random.Generator) -> np.ndarray:
    """A phone floor: many agents talking almost continuously at similar
    levels, in an acoustically treated room (short reverb); little typing."""
    room = room_tone(n, rng, band=(200.0, 4500.0), dark=0.5, drift=(0.03, 0.02))
    # Voices sit in the band the caller actually hears (nothing boomy below
    # ~300 Hz): nearer and brighter than the office's distant colleagues.
    murmur = distant_murmur(
        n, rng, talkers=22, level_range=(-8.0, -4.0), band=(320.0, 3800.0),
        lowpass=2900.0, rt60=0.3, wet=0.5, talk=(2.0, 7.0), pause=(0.2, 1.2),
    )
    keys = keyboard(
        n, rng, desks=((-6.0, 4000.0), (-9.0, 3000.0), (-12.0, 2200.0), (-14.0, 1800.0)),
        keys_per_run=(3, 13), gap=(5.0, 14.0),
    )
    return _unit(room) * _db(-4.0) + _unit(murmur) * _db(0.0) + _unit(keys) * _db(-13.0)


def _light_office(n: int, rng: np.random.Generator) -> np.ndarray:
    """A calm, sparsely occupied office: soft dark air, an occasional
    far-away voice, a little typing, a rare chair or page."""
    room = room_tone(n, rng, band=(160.0, 3500.0), dark=0.7, dark_cutoff=1200.0, drift=(0.04, 0.03))
    murmur = distant_murmur(
        n, rng, talkers=3, level_range=(-6.0, -1.0), band=(180.0, 3000.0),
        lowpass=1300.0, rt60=0.5, wet=0.7, talk=(0.8, 2.5), pause=(2.5, 8.0),
        first=(0.5, 6.0),
    )
    keys = keyboard(n, rng, desks=((-4.0, 4500.0), (-11.0, 2400.0)), keys_per_run=(4, 16), gap=(6.0, 15.0))
    events = office_events(n, rng, walks_per_min=0.0, chairs_per_min=1.0, papers_per_min=1.5,
                           distance_lowpass=2500.0)
    return (_unit(room) * _db(0.0) + _unit(murmur) * _db(-10.0) + _unit(keys) * _db(-12.0)
            + _unit(events) * _db(-15.0))


def _busy_office(n: int, rng: np.random.Generator) -> np.ndarray:
    """A lively open-plan floor: more talkers and two nearer desks, a harder
    (more reverberant) room, frequent typing, footsteps, chairs, paper."""
    room = room_tone(n, rng, band=(200.0, 6000.0), dark=0.3, drift=(0.08, 0.05))
    far = distant_murmur(
        n, rng, talkers=12, level_range=(-10.0, -3.0), band=(180.0, 3400.0),
        lowpass=1900.0, rt60=0.6, wet=0.65, talk=(1.0, 5.0), pause=(0.3, 2.5),
    )
    near = distant_murmur(
        n, rng, talkers=2, level_range=(-2.0, 0.0), band=(200.0, 3600.0),
        lowpass=2800.0, rt60=0.35, wet=0.4, talk=(1.5, 4.0), pause=(1.0, 4.0),
    )
    keys = keyboard(
        n, rng,
        desks=((-1.0, 6500.0), (-4.0, 5000.0), (-7.0, 3800.0), (-9.0, 3000.0), (-12.0, 2400.0), (-15.0, 1800.0)),
        keys_per_run=(6, 31), first=(0.2, 3.0), gap=(1.5, 6.0),
    )
    events = office_events(n, rng, walks_per_min=5.0, chairs_per_min=4.0, papers_per_min=6.0)
    return (_unit(room) * _db(-2.0) + _unit(far) * _db(-1.0) + _unit(near) * _db(-6.0)
            + _unit(keys) * _db(-6.0) + _unit(events) * _db(-9.0))


def _room_tone(n: int, rng: np.random.Generator) -> np.ndarray:
    """Neutral, steady room air and ventilation — no voices, no events."""
    air = room_tone(n, rng, band=(160.0, 4200.0), dark=0.55, dark_cutoff=1400.0,
                    drift=(0.025, 0.015), drift_hz=(0.043, 0.09))
    return _unit(air) * _db(0.0) + _unit(ventilation(n, rng)) * _db(-5.0)


@dataclass(frozen=True)
class Recipe:
    render: object
    seed: int
    loop_seconds: float
    rates: tuple[int, ...] = field(default=(16_000,))


PRESETS: dict[str, Recipe] = {
    # office: the baseline bytes — seed, length and three rates unchanged.
    "office": Recipe(_office, 20260928, 24.0, (8_000, 16_000, 24_000)),
    "call_center": Recipe(_call_center, 20261001, 24.0),
    "light_office": Recipe(_light_office, 20261002, 30.0),
    "busy_office": Recipe(_busy_office, 20261003, 24.0),
    "room_tone": Recipe(_room_tone, 20261004, 12.0),
}


def render_preset(name: str) -> np.ndarray:
    """The preset's seamless loop at ``MASTER_RATE``, -30 dBFS RMS."""
    recipe = PRESETS[name]
    rng = np.random.default_rng(recipe.seed)
    loop_n = int(recipe.loop_seconds * MASTER_RATE)
    fade_n = int(CROSSFADE_SECONDS * MASTER_RATE)
    mix = recipe.render(loop_n + fade_n, rng)
    mix = _highpass(mix, 180.0)
    loop = seamless_loop(mix, loop_n, fade_n)
    return loop / _rms(loop) * _db(STORED_RMS_DBFS)


def render_master(seed: int = PRESETS["office"].seed) -> np.ndarray:
    """Back-compat: the office loop."""
    assert seed == PRESETS["office"].seed
    return render_preset("office")


def to_rate(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == MASTER_RATE:
        return samples
    divisor = np.gcd(MASTER_RATE, rate)
    return resample_poly(samples, rate // divisor, MASTER_RATE // divisor)


def _k_weight(x: np.ndarray, rate: int) -> np.ndarray:
    """BS.1770 K-weighting (shelf + high-pass) at any rate."""
    a_gain = 10 ** (4.0 / 40.0)
    w0 = 2 * np.pi * 1500.0 / rate
    alpha = np.sin(w0) / (2 * (1 / np.sqrt(2)))
    cos = np.cos(w0)
    b = [a_gain * ((a_gain + 1) + (a_gain - 1) * cos + 2 * np.sqrt(a_gain) * alpha),
         -2 * a_gain * ((a_gain - 1) + (a_gain + 1) * cos),
         a_gain * ((a_gain + 1) + (a_gain - 1) * cos - 2 * np.sqrt(a_gain) * alpha)]
    a = [(a_gain + 1) - (a_gain - 1) * cos + 2 * np.sqrt(a_gain) * alpha,
         2 * ((a_gain - 1) - (a_gain + 1) * cos),
         (a_gain + 1) - (a_gain - 1) * cos - 2 * np.sqrt(a_gain) * alpha]
    y = lfilter(np.array(b) / a[0], np.array(a) / a[0], x)
    w0 = 2 * np.pi * 38.0 / rate
    alpha = np.sin(w0) / (2 * 0.5)
    cos = np.cos(w0)
    b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
    a = [1 + alpha, -2 * cos, 1 - alpha]
    return lfilter(np.array(b) / a[0], np.array(a) / a[0], y)


def phone_loudness(samples: np.ndarray, rate: int) -> float:
    """K-weighted loudness (LUFS-like, ungated) of the 300–3400 Hz phone band
    at equal RMS — the band every caller hears the room through."""
    x = samples / (_rms(samples) or 1.0)
    sos = butter(4, [300.0, 3400.0], btype="band", fs=rate, output="sos")
    y = _k_weight(sosfilt(sos, x), rate)
    return -0.691 + 10 * np.log10(np.mean(np.square(y)) + 1e-20)


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.tobytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--preset", action="append", choices=sorted(PRESETS))
    args = parser.parse_args(argv)
    loudness: dict[str, float] = {}
    for name in args.preset or list(PRESETS):
        master = render_preset(name)
        loudness[name] = phone_loudness(to_rate(master, 8000), 8000)
        for rate in PRESETS[name].rates:
            samples = to_rate(master, rate)
            path = args.out / f"{name}_ambience_{rate}.wav"
            write_wav(path, samples, rate)
            print(
                f"{path}: {len(samples) / rate:.2f} s, rms {20 * np.log10(_rms(samples)):.1f} dBFS, "
                f"peak {20 * np.log10(np.max(np.abs(samples))):.1f} dBFS"
            )
    if "office" in loudness:
        print("loudness trims vs office (phone band, equal RMS):")
        for name, value in loudness.items():
            print(f"  {name:13s} {loudness['office'] - value:+.2f} dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
