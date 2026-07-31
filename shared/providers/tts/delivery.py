"""Cross-provider delivery-tuning → provider-parameter mapping.

Single source of truth for how the bot-level Delivery tuning controls
(canonical speaking speed, Energy) translate into provider wire parameters.
Both the live voice runtime (StreamingTTSRouter / pipeline REST path) and the
backend voice preview call :func:`apply_delivery_params`, so preview and live
calls can never diverge.

Precedence rules (tested in tests/unit/test_delivery_tuning.py):

- Canonical speed is authoritative: it OVERWRITES any legacy per-provider
  ``pace``/``speed`` value still present in stored tts_settings. It is never
  applied with ``setdefault``.
- Energy is best-effort and conservative: it only fills provider fields the
  operator has NOT explicitly configured, and only fields the selected model
  documents (never Sarvam v2-only pitch/loudness on bulbul:v3, never speed on
  eleven_v3). Unrelated provider settings are passed through untouched.
"""

from __future__ import annotations

from typing import Any

# Canonical delivery-speed range (matches the voice-settings API validation).
SPEED_MIN, SPEED_MAX = 0.5, 2.0

# Documented per-provider/model speed-parameter ranges (catalog schemas).
_SARVAM_PACE_RANGE = {"bulbul:v2": (0.3, 3.0)}
_SARVAM_PACE_DEFAULT_RANGE = (0.5, 2.0)  # bulbul:v3 and newer
_ELEVEN_SPEED_RANGE = (0.7, 1.2)
# ElevenLabs models whose voice_settings reject ``speed`` (Eleven v3 alpha).
_ELEVEN_NO_SPEED_MODELS = {"eleven_v3"}
# Sarvam models that accept the v2-only pitch/loudness controls.
_SARVAM_PITCH_LOUDNESS_MODELS = {"bulbul:v2"}

# Conservative native Energy mappings, keyed by the band's upper bound.
# ElevenLabs: ``style`` is the documented expressiveness control; kept well
# below the instability zone. Sarvam v2: mild pitch/loudness shifts inside the
# documented ranges. The 41–60 band is neutral — nothing is sent.
_ELEVEN_ENERGY_STYLE = ((20, 0.0), (40, 0.0), (60, None), (80, 0.2), (100, 0.4))
_SARVAM_V2_ENERGY = (
    (20, {"pitch": -0.1, "loudness": 0.85}),
    (40, {"pitch": -0.05, "loudness": 0.95}),
    (60, None),
    (80, {"pitch": 0.05, "loudness": 1.15}),
    (100, {"pitch": 0.1, "loudness": 1.3}),
)


def clamp_speed(value: float | int | None, default: float = 1.0) -> float:
    """Clamp a canonical delivery speed into the supported range."""
    try:
        speed = float(value)
    except (TypeError, ValueError):
        return default
    return min(SPEED_MAX, max(SPEED_MIN, speed))


def clamp_level(value: int | float | None, default: int = 50) -> int:
    """Clamp a 0–100 delivery level (empathy/energy)."""
    try:
        level = int(value)
    except (TypeError, ValueError):
        return default
    return min(100, max(0, level))


def speed_param_name(provider: str, model: str = "") -> str | None:
    """The wire parameter the provider uses for speaking speed, if any."""
    if provider == "sarvam":
        return "pace"
    if provider == "elevenlabs":
        return None if model in _ELEVEN_NO_SPEED_MODELS else "speed"
    return None


def provider_speed(provider: str, model: str, speed: float) -> float:
    """Canonical speed clamped into the selected model's documented range."""
    speed = clamp_speed(speed)
    if provider == "sarvam":
        low, high = _SARVAM_PACE_RANGE.get(model, _SARVAM_PACE_DEFAULT_RANGE)
    elif provider == "elevenlabs":
        low, high = _ELEVEN_SPEED_RANGE
    else:
        return speed
    return min(high, max(low, speed))


def _band_value(bands, level: int):
    for upper, value in bands:
        if level <= upper:
            return value
    return None


def energy_params(provider: str, model: str, energy: int | None) -> dict[str, Any]:
    """Native provider parameters for a Delivery-tuning Energy level.

    Returns only fields the selected model documents; an empty dict means the
    provider has no safe native control for this level (the LLM delivery
    instruction remains the cross-provider behavior).
    """
    if energy is None:
        return {}
    level = clamp_level(energy)
    if provider == "elevenlabs":
        style = _band_value(_ELEVEN_ENERGY_STYLE, level)
        return {} if style is None else {"style": style}
    if provider == "sarvam" and model in _SARVAM_PITCH_LOUDNESS_MODELS:
        mapped = _band_value(_SARVAM_V2_ENERGY, level)
        return dict(mapped) if mapped else {}
    return {}


def apply_delivery_params(
    provider: str,
    model: str,
    params: dict[str, Any] | None,
    *,
    speed: float | None = None,
    energy: int | None = None,
) -> dict[str, Any]:
    """Overlay canonical delivery tuning onto provider synthesis parameters.

    Returns a new dict — the input is never mutated. See the module docstring
    for the precedence rules.
    """
    merged: dict[str, Any] = dict(params or {})
    # Energy first (fill-only), so an explicit operator setting always wins.
    for key, value in energy_params(provider, model, energy).items():
        if merged.get(key) is None:
            merged[key] = value
    if speed is not None:
        # Drop legacy duplicates for the OTHER naming so a stale value can
        # never shadow the canonical speed after a provider change.
        merged.pop("pace", None)
        merged.pop("speed", None)
        key = speed_param_name(provider, model)
        if key is not None:
            merged[key] = provider_speed(provider, model, speed)
    return merged
