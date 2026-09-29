"""Background ambience presets and the volume scale (Natural Conversation →
Call environment).

The one registry both sides read: settings validation
(``shared.orchestration.naturalness``) accepts only these preset ids, and the
voice runtime (``voice_runtime.ambience``) maps an id to its asset and level.
Settings persist the stable id and a 0–100 volume — never a file path, never
a raw gain.

Pure data, no audio dependencies: the API process imports it too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AmbiencePreset:
    """One selectable room sound.

    ``asset`` is the file stem under ``voice_runtime/assets/ambience``
    (``<asset>_<rate>.wav``; one canonical rate is enough — other rates are
    resampled once at load). ``loudness_trim_db`` is measured: it makes the
    preset as loud as ``office`` over the phone band at the same volume
    (scripts/generate_ambience_asset.py prints it). ``character_offset_db``
    is intentional: a sparse or perfectly steady bed at office loudness
    reads as more hiss, so those presets sit a little lower.

    A supplied recording may fade in and out at its ends; ``loop_start_s`` /
    ``loop_end_s`` pick its steady middle (None = to the end) and
    ``loop_crossfade_ms`` closes that loop.

    ``production_enabled``: offered for selection (the UI lists only these;
    the API refuses a NEW selection of any other). Every preset is enabled
    today; the flag lets one be withdrawn later without UI changes. A saved
    value of a withdrawn preset still resolves: it plays if its asset is
    installed, otherwise the call falls back to ``office``.
    """

    id: str
    label: str
    description: str
    asset: str
    loudness_trim_db: float = 0.0
    character_offset_db: float = 0.0
    loop_start_s: float = 0.0
    loop_end_s: float | None = None
    loop_crossfade_ms: int = 150
    production_enabled: bool = True

    @property
    def trim_db(self) -> float:
        return self.loudness_trim_db + self.character_offset_db


AMBIENCE_PRESETS: dict[str, AmbiencePreset] = {
    preset.id: preset
    for preset in (
        AmbiencePreset(
            "office", "Office",
            "Steady office air, a few distant colleagues talking, occasional typing.",
            "office_ambience",
        ),
        AmbiencePreset(
            "call_center", "Call Center",
            "Many agents talking almost continuously in a treated room; little typing.",
            "call_center_ambience", loudness_trim_db=0.65,
        ),
        AmbiencePreset(
            "light_office", "Light Office",
            "A calm, sparsely occupied office: soft air, the odd far-away voice, a little typing.",
            "light_office_ambience", loudness_trim_db=0.05, character_offset_db=-3.0,
        ),
        AmbiencePreset(
            "busy_office", "Busy Office",
            "A lively open-plan floor: more and nearer voices, frequent typing, footsteps, chairs, paper.",
            "busy_office_ambience", loudness_trim_db=0.30,
        ),
        AmbiencePreset(
            "room_tone", "Room Tone",
            "Neutral room air and ventilation only: no voices, no events.",
            "room_tone_ambience", loudness_trim_db=0.03, character_offset_db=-2.0,
        ),
        # Supplied recording (not generated): 18 s with ~2.5 s fades at both
        # ends, so only its steady 2.5–15.5 s middle loops. Offered by product
        # decision (2026-09-29) with two documented caveats
        # (docs/HUMAN_SPEECH_NATURALNESS.md): its licence/source is unverified,
        # and under an extreme echo (volume 100, 10 dB echo loss) Sarvam
        # transcribed its room echo as a word; at 15–25 dB echo loss nothing
        # reached STT, as for the generated presets.
        AmbiencePreset(
            "echo_ringing", "Echo Ringing",
            "A reverberant room: a warbling ring tone over a steady murmur.",
            "echo_ringing_sound", loudness_trim_db=0.72,
            loop_start_s=2.5, loop_end_s=15.5, loop_crossfade_ms=1000,
        ),
    )
}
AMBIENCE_PRESET_IDS: tuple[str, ...] = tuple(AMBIENCE_PRESETS)
PRODUCTION_AMBIENCE_PRESET_IDS: tuple[str, ...] = tuple(
    preset.id for preset in AMBIENCE_PRESETS.values() if preset.production_enabled
)
DEFAULT_AMBIENCE_PRESET = "office"
assert DEFAULT_AMBIENCE_PRESET in PRODUCTION_AMBIENCE_PRESET_IDS


def ambience_preset_catalog() -> list[dict]:
    """Every preset for the settings UI, in registry order. The UI offers
    only ``productionEnabled`` ones; the others are listed so a saved value
    can still be shown by name."""
    return [
        {
            "id": preset.id,
            "label": preset.label,
            "description": preset.description,
            "productionEnabled": preset.production_enabled,
        }
        for preset in AMBIENCE_PRESETS.values()
    ]

# ── volume scale ─────────────────────────────────────────────────────────
# A 0–100 control, resolved to dB relative to normal bot speech — never to a
# raw PCM amplitude. 0 is muted; 1–50 spans the quiet end up to the default
# (the level the feature shipped with), 50–100 the default up to a ceiling
# that stays clearly below speech. Piecewise linear IN dB, so every step is
# the same loudness ratio within each half (0.18 dB/step below 50, 0.24
# dB/step above).
AMBIENCE_VOLUME_RANGE = (0, 100)
DEFAULT_AMBIENCE_VOLUME = 50
AMBIENCE_QUIET_DB = -45.0      # volume 1
AMBIENCE_DEFAULT_DB = -36.0    # volume 50 (the original fixed level)
AMBIENCE_MAX_DB = -24.0        # volume 100


def resolve_ambience_preset(value: object) -> AmbiencePreset:
    """The preset for a stored value; anything unknown is ``office``."""
    if isinstance(value, str) and value in AMBIENCE_PRESETS:
        return AMBIENCE_PRESETS[value]
    return AMBIENCE_PRESETS[DEFAULT_AMBIENCE_PRESET]


def resolve_ambience_volume(value: object) -> int:
    """A stored volume as an int in 0..100: out-of-range values are clamped,
    anything malformed (bool, NaN, text, …) is the default."""
    if isinstance(value, bool) or value is None:
        return DEFAULT_AMBIENCE_VOLUME
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return DEFAULT_AMBIENCE_VOLUME
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return DEFAULT_AMBIENCE_VOLUME
    low, high = AMBIENCE_VOLUME_RANGE
    return int(min(high, max(low, int(value))))


def ambience_volume_db(value: object) -> float | None:
    """Ambience level in dB relative to normal speech; None = muted (0)."""
    volume = resolve_ambience_volume(value)
    if volume <= 0:
        return None
    if volume <= DEFAULT_AMBIENCE_VOLUME:
        span = (AMBIENCE_DEFAULT_DB - AMBIENCE_QUIET_DB) / (DEFAULT_AMBIENCE_VOLUME - 1)
        return AMBIENCE_QUIET_DB + (volume - 1) * span
    span = (AMBIENCE_MAX_DB - AMBIENCE_DEFAULT_DB) / (AMBIENCE_VOLUME_RANGE[1] - DEFAULT_AMBIENCE_VOLUME)
    return AMBIENCE_DEFAULT_DB + (volume - DEFAULT_AMBIENCE_VOLUME) * span
