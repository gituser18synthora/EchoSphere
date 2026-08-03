"""Shared contract for per-bot end-of-turn timing.

The voice runtime consumes these values while the control-plane validates and
persists them.  Keeping the bounds here prevents the API and realtime worker
from drifting into different ideas of what constitutes a safe setting.
"""

from __future__ import annotations

from typing import Any


TURN_DETECTION_DEFAULTS: dict[str, dict[str, float]] = {
    "browser": {
        "confidence": 0.7,
        "start_secs": 0.3,
        "stop_secs": 0.2,
        "min_volume": 0.6,
        "user_speech_timeout": 1.2,
        "finalize_grace": 0.3,
    },
    "telephony": {
        "confidence": 0.6,
        "start_secs": 0.2,
        "stop_secs": 0.2,
        "min_volume": 0.4,
        "user_speech_timeout": 0.8,
        "finalize_grace": 0.3,
    },
}

TURN_DETECTION_BOUNDS: dict[str, tuple[float, float]] = {
    "confidence": (0.3, 0.95),
    "start_secs": (0.1, 1.0),
    "stop_secs": (0.1, 2.0),
    "min_volume": (0.0, 1.0),
    "user_speech_timeout": (0.2, 3.0),
    "finalize_grace": (0.0, 1.5),
}


def validate_turn_detection(value: Any, *, prefix: str = "Turn detection") -> list[str]:
    """Validate the platform-owned ``stt_settings.turn_detection`` object."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{prefix}: must be an object."]

    errors: list[str] = []
    for key in value:
        if key not in TURN_DETECTION_BOUNDS:
            errors.append(f"{prefix}: unknown parameter '{key}'.")

    for key, raw in value.items():
        bounds = TURN_DETECTION_BOUNDS.get(key)
        if bounds is None:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            errors.append(f"{prefix}: '{key}' must be a number.")
            continue
        low, high = bounds
        if not low <= float(raw) <= high:
            errors.append(
                f"{prefix}: '{key}' must be between {low:g} and {high:g}."
            )
    return errors
