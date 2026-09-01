"""Authoritative runtime and admin contract for tenant turn detection.

The realtime worker consumes the resolved numeric maps, while the backend and
Studio UI consume the field metadata and bounds below. Defining both from the
same schema prevents defaults, validation and labels from drifting apart.
"""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any


TURN_DETECTION_SCHEMA_VERSION = 1
TURN_DETECTION_TRANSPORTS = (
    {"id": "browser", "label": "Browser", "description": "Web microphone and browser audio sessions."},
    {"id": "telephony", "label": "Telephony", "description": "PSTN/SIP calls through FreeSWITCH."},
)
TURN_DETECTION_SECTIONS = (
    {"id": "speech_detection", "label": "Speech Detection", "description": "How quickly and confidently caller speech is recognised."},
    {"id": "end_of_turn", "label": "End-of-turn / Silence", "description": "When a caller pause is considered the end of their turn."},
    {"id": "interruption", "label": "Interruption / Barge-in", "description": "How caller speech interrupts a bot reply without reacting to noise."},
    {"id": "timing_debounce", "label": "Timing / Debounce", "description": "Short transcript-finalisation windows before response generation."},
    {"id": "noise_suppression", "label": "Noise Suppression", "description": "Adaptive energy gate for microphone and line noise."},
    {"id": "speech_buffering", "label": "Speech Timing / Buffering", "description": "Speech confirmation, trailing audio and leading-audio preservation."},
    {"id": "echo_protection", "label": "Echo Protection", "description": "Extra confirmation while or shortly after the bot is speaking."},
)
TURN_DETECTION_MODES = (
    {"id": "system_default", "label": "System Default", "description": "Use current runtime defaults with no tenant overrides."},
    {"id": "recommended", "label": "Recommended", "description": "Balanced production settings tuned for each audio transport."},
    {"id": "custom", "label": "Custom", "description": "Use validated tenant overrides on top of runtime defaults."},
)
_MODE_IDS = {mode["id"] for mode in TURN_DETECTION_MODES}
_TRANSPORT_IDS = tuple(transport["id"] for transport in TURN_DETECTION_TRANSPORTS)


# Every runtime-safe setting supported by this module. Internal state-machine
# constants deliberately do not appear here. Recommended values deliberately
# do not chase every minimum: browser endpoints can be faster on clean audio,
# while telephony keeps longer speech/pause/echo confirmation for PSTN noise,
# codec artifacts and jitter. Endpoint windows and the barge-in VAD fallback
# were relaxed after live testing showed the bot starting its reply inside a
# caller's natural mid-thought pause and then being hard to interrupt.
TURN_DETECTION_FIELDS: tuple[dict[str, Any], ...] = (
    {"group": "turn_detection", "key": "confidence", "section": "speech_detection", "label": "VAD confidence", "description": "Minimum voice-activity confidence required to classify audio as speech.", "input": "slider", "valueType": "number", "unit": "ratio", "min": 0.3, "max": 0.95, "step": 0.01, "default": {"browser": 0.7, "telephony": 0.6}, "recommended": {"browser": 0.65, "telephony": 0.58}},
    {"group": "turn_detection", "key": "start_secs", "section": "speech_detection", "label": "Speech-start confirmation", "description": "Continuous speech required before a new caller turn starts.", "input": "number", "valueType": "number", "unit": "s", "min": 0.1, "max": 1.0, "step": 0.05, "default": {"browser": 0.3, "telephony": 0.2}, "recommended": {"browser": 0.2, "telephony": 0.25}},
    {"group": "turn_detection", "key": "min_volume", "section": "speech_detection", "label": "Minimum VAD volume", "description": "Minimum normalized volume supplied to voice activity detection.", "input": "slider", "valueType": "number", "unit": "ratio", "min": 0.0, "max": 1.0, "step": 0.05, "default": {"browser": 0.6, "telephony": 0.4}, "recommended": {"browser": 0.5, "telephony": 0.35}},
    {"group": "turn_detection", "key": "stop_secs", "section": "end_of_turn", "label": "Speech-end confirmation", "description": "Initial silence required before VAD marks caller speech as stopped.", "input": "number", "valueType": "number", "unit": "s", "min": 0.1, "max": 2.0, "step": 0.05, "default": {"browser": 0.2, "telephony": 0.2}, "recommended": {"browser": 0.2, "telephony": 0.25}},
    {"group": "turn_detection", "key": "user_speech_timeout", "section": "end_of_turn", "label": "Natural pause window", "description": "Additional silence allowed for an incomplete thought before closing the turn.", "input": "number", "valueType": "number", "unit": "s", "min": 0.2, "max": 3.0, "step": 0.05, "default": {"browser": 1.2, "telephony": 0.7}, "recommended": {"browser": 1.2, "telephony": 1.3}},
    {"group": "turn_detection", "key": "complete_endpoint", "section": "end_of_turn", "label": "Complete-thought endpoint", "description": "Silence used when the transcript already reads as a finished thought.", "input": "number", "valueType": "number", "unit": "s", "min": 0.1, "max": 1.5, "step": 0.05, "default": {"browser": 0.35, "telephony": 0.2}, "recommended": {"browser": 0.55, "telephony": 0.6}},
    {"group": "turn_detection", "key": "short_reply_endpoint", "section": "end_of_turn", "label": "Short-reply endpoint", "description": "Silence used for self-contained replies such as yes, no or okay.", "input": "number", "valueType": "number", "unit": "s", "min": 0.0, "max": 1.0, "step": 0.05, "default": {"browser": 0.12, "telephony": 0.1}, "recommended": {"browser": 0.25, "telephony": 0.3}},
    {"group": "turn_detection", "key": "barge_in_min_words", "section": "interruption", "label": "Barge-in word threshold", "description": "Transcript words required before caller speech interrupts a bot reply; zero disables the word gate.", "input": "number", "valueType": "integer", "unit": "words", "min": 0.0, "max": 10.0, "step": 1.0, "default": {"browser": 2.0, "telephony": 2.0}, "recommended": {"browser": 2.0, "telephony": 2.0}},
    {"group": "turn_detection", "key": "barge_in_vad_fallback_secs", "section": "interruption", "label": "Barge-in VAD fallback", "description": "Sustained gated speech that confirms interruption when no interim transcript arrives; zero disables it.", "input": "number", "valueType": "number", "unit": "s", "min": 0.0, "max": 5.0, "step": 0.05, "default": {"browser": 1.0, "telephony": 1.0}, "recommended": {"browser": 0.5, "telephony": 0.8}},
    {"group": "turn_detection", "key": "finalize_grace", "section": "timing_debounce", "label": "Transcript finalization grace", "description": "Maximum wait for late final transcript fragments before routing begins.", "input": "number", "valueType": "number", "unit": "s", "min": 0.0, "max": 1.5, "step": 0.05, "default": {"browser": 0.3, "telephony": 0.12}, "recommended": {"browser": 0.18, "telephony": 0.2}},
    {"group": "turn_detection", "key": "finalize_settle", "section": "timing_debounce", "label": "Transcript settle window", "description": "How recently a final transcript may arrive before the finalization debounce is skipped.", "input": "number", "valueType": "number", "unit": "s", "min": 0.0, "max": 1.0, "step": 0.05, "default": {"browser": 0.15, "telephony": 0.1}, "recommended": {"browser": 0.1, "telephony": 0.12}},
    {"group": "noise_gate", "key": "enabled", "section": "noise_suppression", "label": "Adaptive noise gate", "description": "Reject low-energy noise before it reaches voice activity detection.", "input": "toggle", "valueType": "boolean", "unit": "on/off", "min": 0.0, "max": 1.0, "step": 1.0, "default": {"browser": 1.0, "telephony": 1.0}, "recommended": {"browser": 1.0, "telephony": 1.0}},
    {"group": "noise_gate", "key": "noise_margin_db", "section": "noise_suppression", "label": "Noise-floor margin", "description": "Required loudness above the learned background-noise floor.", "input": "number", "valueType": "number", "unit": "dB", "min": 3.0, "max": 24.0, "step": 0.5, "default": {"browser": 10.0, "telephony": 8.0}, "recommended": {"browser": 9.0, "telephony": 8.0}},
    {"group": "noise_gate", "key": "min_threshold_dbfs", "section": "noise_suppression", "label": "Minimum speech threshold", "description": "Absolute lower energy threshold; more negative values admit quieter audio.", "input": "number", "valueType": "number", "unit": "dBFS", "min": -70.0, "max": -20.0, "step": 1.0, "default": {"browser": -50.0, "telephony": -50.0}, "recommended": {"browser": -50.0, "telephony": -52.0}},
    {"group": "noise_gate", "key": "min_speech_ms", "section": "speech_buffering", "label": "Minimum speech duration", "description": "Continuous above-threshold audio required to open the gate while the bot is quiet.", "input": "number", "valueType": "number", "unit": "ms", "min": 40.0, "max": 500.0, "step": 10.0, "default": {"browser": 120.0, "telephony": 120.0}, "recommended": {"browser": 100.0, "telephony": 140.0}},
    {"group": "noise_gate", "key": "hangover_ms", "section": "speech_buffering", "label": "Trailing speech buffer", "description": "How long the gate remains open after energy drops, preserving quiet word endings.", "input": "number", "valueType": "number", "unit": "ms", "min": 100.0, "max": 1500.0, "step": 10.0, "default": {"browser": 320.0, "telephony": 350.0}, "recommended": {"browser": 300.0, "telephony": 380.0}},
    {"group": "noise_gate", "key": "preroll_ms", "section": "speech_buffering", "label": "Leading audio buffer", "description": "Audio retained before gate-open so the beginning of the first word is not clipped.", "input": "number", "valueType": "number", "unit": "ms", "min": 0.0, "max": 600.0, "step": 10.0, "default": {"browser": 160.0, "telephony": 160.0}, "recommended": {"browser": 180.0, "telephony": 220.0}},
    {"group": "noise_gate", "key": "echo_min_speech_ms", "section": "echo_protection", "label": "Echo speech confirmation", "description": "Longer speech confirmation used while bot audio could be echoing into the caller channel.", "input": "number", "valueType": "number", "unit": "ms", "min": 40.0, "max": 800.0, "step": 10.0, "default": {"browser": 180.0, "telephony": 200.0}, "recommended": {"browser": 180.0, "telephony": 220.0}},
    {"group": "noise_gate", "key": "echo_margin_db", "section": "echo_protection", "label": "Echo energy margin", "description": "Additional loudness required while the bot is speaking to avoid self-interruption.", "input": "number", "valueType": "number", "unit": "dB", "min": 0.0, "max": 24.0, "step": 0.5, "default": {"browser": 6.0, "telephony": 5.0}, "recommended": {"browser": 6.0, "telephony": 5.0}},
    {"group": "noise_gate", "key": "echo_tail_ms", "section": "echo_protection", "label": "Echo tail window", "description": "How long echo protection remains active after bot playback stops.", "input": "number", "valueType": "number", "unit": "ms", "min": 0.0, "max": 1500.0, "step": 10.0, "default": {"browser": 250.0, "telephony": 300.0}, "recommended": {"browser": 250.0, "telephony": 320.0}},
)


def _group_profile(group: str, profile: str) -> dict[str, dict[str, float]]:
    return {
        transport["id"]: {
            field["key"]: float(field[profile][transport["id"]])
            for field in TURN_DETECTION_FIELDS if field["group"] == group
        }
        for transport in TURN_DETECTION_TRANSPORTS
    }


TURN_DETECTION_DEFAULTS = _group_profile("turn_detection", "default")
TURN_DETECTION_RECOMMENDED = _group_profile("turn_detection", "recommended")
NOISE_GATE_DEFAULTS = _group_profile("noise_gate", "default")
NOISE_GATE_RECOMMENDED = _group_profile("noise_gate", "recommended")
TURN_DETECTION_BOUNDS = {
    field["key"]: (float(field["min"]), float(field["max"]))
    for field in TURN_DETECTION_FIELDS if field["group"] == "turn_detection"
}
NOISE_GATE_BOUNDS = {
    field["key"]: (float(field["min"]), float(field["max"]))
    for field in TURN_DETECTION_FIELDS if field["group"] == "noise_gate"
}
_FIELDS_BY_GROUP = {
    group: {field["key"]: field for field in TURN_DETECTION_FIELDS if field["group"] == group}
    for group in ("turn_detection", "noise_gate")
}


def validate_turn_detection(value: Any, *, prefix: str = "Turn detection") -> list[str]:
    """Validate the legacy ``stt_settings.turn_detection`` object."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{prefix}: must be an object."]
    return _validate_bounded(value, TURN_DETECTION_BOUNDS, prefix=prefix)


def validate_noise_gate(value: Any, *, prefix: str = "Noise gate") -> list[str]:
    """Validate the legacy ``stt_settings.noise_gate`` object."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{prefix}: must be an object."]
    return _validate_bounded(value, NOISE_GATE_BOUNDS, prefix=prefix, allow_enabled_bool=True)


def _validate_bounded(
    value: dict,
    bounds_by_key: dict[str, tuple[float, float]],
    *,
    prefix: str,
    allow_enabled_bool: bool = False,
) -> list[str]:
    errors: list[str] = []
    for key in value:
        if key not in bounds_by_key:
            errors.append(f"{prefix}: unknown parameter '{key}'.")
    for key, raw in value.items():
        bounds = bounds_by_key.get(key)
        if bounds is None:
            continue
        if isinstance(raw, bool):
            if allow_enabled_bool and key == "enabled":
                continue
            errors.append(f"{prefix}: '{key}' must be a number.")
            continue
        if not isinstance(raw, (int, float)):
            errors.append(f"{prefix}: '{key}' must be a number.")
            continue
        low, high = bounds
        if not low <= float(raw) <= high:
            errors.append(f"{prefix}: '{key}' must be between {low:g} and {high:g}.")
    return errors


def validate_tenant_turn_detection(value: Any) -> list[str]:
    """Strict validation for the Tenant Admin configuration endpoint."""
    if not isinstance(value, dict):
        return ["Turn detection configuration must be an object."]
    unknown_top = set(value) - {"mode", "overrides"}
    errors = [f"Turn detection: unknown property '{key}'." for key in sorted(unknown_top)]
    mode = value.get("mode", "system_default")
    if mode not in _MODE_IDS:
        errors.append("Turn detection mode must be system_default, recommended or custom.")
    overrides = value.get("overrides", {})
    if not isinstance(overrides, dict):
        return errors + ["Turn detection overrides must be an object."]
    for transport, groups in overrides.items():
        if transport not in _TRANSPORT_IDS:
            errors.append(f"Turn detection: unknown transport '{transport}'.")
            continue
        if not isinstance(groups, dict):
            errors.append(f"Turn detection {transport}: must be an object.")
            continue
        for group, values in groups.items():
            fields = _FIELDS_BY_GROUP.get(group)
            if fields is None:
                errors.append(f"Turn detection {transport}: unknown group '{group}'.")
                continue
            if not isinstance(values, dict):
                errors.append(f"Turn detection {transport}.{group}: must be an object.")
                continue
            for key, raw in values.items():
                spec = fields.get(key)
                prefix = f"Turn detection {transport}.{group}.{key}"
                if spec is None:
                    errors.append(f"{prefix}: unknown parameter.")
                    continue
                if spec["valueType"] == "boolean":
                    if not isinstance(raw, bool) and raw not in (0, 1, 0.0, 1.0):
                        errors.append(f"{prefix}: must be true or false.")
                    continue
                if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    errors.append(f"{prefix}: must be a number.")
                    continue
                if spec["valueType"] == "integer" and not float(raw).is_integer():
                    errors.append(f"{prefix}: must be a whole number.")
                    continue
                if not float(spec["min"]) <= float(raw) <= float(spec["max"]):
                    errors.append(f"{prefix}: must be between {spec['min']:g} and {spec['max']:g}.")
    return errors


def normalize_tenant_turn_detection(value: dict[str, Any]) -> dict[str, Any]:
    """Return a sparse, JSON-safe storage document after strict validation."""
    mode = value.get("mode", "system_default")
    if mode != "custom":
        return {"mode": mode}
    canonical: dict[str, Any] = {"mode": "custom", "overrides": {}}
    for transport, groups in (value.get("overrides") or {}).items():
        transport_out: dict[str, Any] = {}
        for group, values in groups.items():
            group_out: dict[str, Any] = {}
            for key, raw in values.items():
                spec = _FIELDS_BY_GROUP[group][key]
                if spec["valueType"] == "boolean":
                    group_out[key] = bool(raw)
                elif spec["valueType"] == "integer":
                    group_out[key] = int(raw)
                else:
                    group_out[key] = float(raw)
            if group_out:
                transport_out[group] = group_out
        if transport_out:
            canonical["overrides"][transport] = transport_out
    return canonical


def sanitize_tenant_turn_detection(value: Any) -> dict[str, Any]:
    """Best-effort migration path for partial, invalid or older stored JSON.

    Known in-range values survive; unknown, malformed and out-of-range values
    are removed so resolution falls back to the current field default instead
    of carrying a corrupted value into a call.
    """
    if not isinstance(value, dict) or value.get("mode") not in _MODE_IDS:
        return {"mode": "system_default"}
    mode = value["mode"]
    if mode != "custom":
        return {"mode": mode}
    safe: dict[str, Any] = {"mode": "custom", "overrides": {}}
    overrides = value.get("overrides")
    if not isinstance(overrides, dict):
        return safe
    for transport, groups in overrides.items():
        if transport not in _TRANSPORT_IDS or not isinstance(groups, dict):
            continue
        for group, values in groups.items():
            fields = _FIELDS_BY_GROUP.get(group)
            if fields is None or not isinstance(values, dict):
                continue
            for key, raw in values.items():
                spec = fields.get(key)
                if spec is None:
                    continue
                if spec["valueType"] == "boolean":
                    if not isinstance(raw, bool) and raw not in (0, 1, 0.0, 1.0):
                        continue
                    normalized: bool | int | float = bool(raw)
                elif isinstance(raw, bool) or not isinstance(raw, (int, float)):
                    continue
                elif spec["valueType"] == "integer" and not float(raw).is_integer():
                    continue
                elif not float(spec["min"]) <= float(raw) <= float(spec["max"]):
                    continue
                elif spec["valueType"] == "integer":
                    normalized = int(raw)
                else:
                    normalized = float(raw)
                transport_out = safe["overrides"].setdefault(transport, {})
                group_out = transport_out.setdefault(group, {})
                group_out[key] = normalized
    return safe


def resolve_tenant_turn_detection(value: Any) -> dict[str, dict[str, dict[str, float]]]:
    """Resolve tenant JSON to per-session values, safely falling back per key."""
    canonical = sanitize_tenant_turn_detection(value)
    mode = canonical["mode"]
    overrides = canonical.get("overrides", {})
    result: dict[str, dict[str, dict[str, float]]] = {}
    for transport in _TRANSPORT_IDS:
        groups = overrides.get(transport) if isinstance(overrides.get(transport), dict) else {}
        if mode == "recommended":
            turn_defaults = TURN_DETECTION_RECOMMENDED[transport]
            gate_defaults = NOISE_GATE_RECOMMENDED[transport]
        else:
            turn_defaults = TURN_DETECTION_DEFAULTS[transport]
            gate_defaults = NOISE_GATE_DEFAULTS[transport]
        result[transport] = {
            "turn_detection": resolve_bounded(groups.get("turn_detection"), turn_defaults, TURN_DETECTION_BOUNDS) if mode == "custom" else dict(turn_defaults),
            "noise_gate": resolve_bounded(groups.get("noise_gate"), gate_defaults, NOISE_GATE_BOUNDS) if mode == "custom" else dict(gate_defaults),
        }
    return result


def tenant_turn_detection_payload(value: Any) -> dict[str, Any]:
    """Schema plus stored and effective values returned by the admin API."""
    canonical = sanitize_tenant_turn_detection(value)
    mode = canonical["mode"]
    return {
        "schemaVersion": TURN_DETECTION_SCHEMA_VERSION,
        "mode": mode,
        "overrides": canonical.get("overrides", {}),
        "effective": resolve_tenant_turn_detection(canonical),
        "transports": deepcopy(TURN_DETECTION_TRANSPORTS),
        "sections": deepcopy(TURN_DETECTION_SECTIONS),
        "modes": deepcopy(TURN_DETECTION_MODES),
        "fields": deepcopy(TURN_DETECTION_FIELDS),
    }


def resolve_bounded(
    overrides: Any,
    defaults: dict[str, float],
    bounds_by_key: dict[str, tuple[float, float]],
    *,
    on_invalid=None,
) -> dict[str, float]:
    """Merge overrides onto defaults, clamping every value to its bounds."""
    overrides = overrides if isinstance(overrides, dict) else {}
    resolved: dict[str, float] = {}
    for key, default in defaults.items():
        value = overrides.get(key, default)
        try:
            value = float(value)
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            if on_invalid is not None:
                on_invalid(key, value, default)
            value = default
        low, high = bounds_by_key[key]
        resolved[key] = min(max(value, low), high)
    return resolved
