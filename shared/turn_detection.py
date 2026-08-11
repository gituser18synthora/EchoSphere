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
        "barge_in_min_words": 2.0,
        "barge_in_vad_fallback_secs": 1.0,
        "user_speech_timeout": 1.2,
        "finalize_grace": 0.3,
        "finalize_settle": 0.15,
        "complete_endpoint": 0.35,
        "short_reply_endpoint": 0.12,
    },
    # Telephony runs tighter endpoints than the browser: PSTN callers already
    # tolerate lower audio latency budgets, and every 100 ms here is dead air
    # after EVERY caller turn. The pause a caller gets mid-thought is
    # stop_secs + user_speech_timeout (0.9 s) — incomplete utterances always
    # wait the full window; only finished thoughts and short replies use the
    # complete/short endpoints below.
    "telephony": {
        "confidence": 0.6,
        "start_secs": 0.2,
        "stop_secs": 0.2,
        "min_volume": 0.4,
        "barge_in_min_words": 2.0,
        "barge_in_vad_fallback_secs": 1.0,
        "user_speech_timeout": 0.7,
        "finalize_grace": 0.12,
        "finalize_settle": 0.1,
        "complete_endpoint": 0.2,
        "short_reply_endpoint": 0.1,
    },
}

TURN_DETECTION_BOUNDS: dict[str, tuple[float, float]] = {
    "confidence": (0.3, 0.95),
    "start_secs": (0.1, 1.0),
    "stop_secs": (0.1, 2.0),
    "min_volume": (0.0, 1.0),
    # Words the STT must transcribe before a caller may interrupt the bot
    # mid-reply. While the bot is quiet, VAD starts the turn as always; while
    # it is speaking, VAD alone cannot — background noise and single-word
    # hallucinations otherwise cancel the reply mid-sentence, which the caller
    # hears as chopped, stuttering audio. 0 disables the word gate entirely
    # (any voice activity interrupts instantly, the pre-2026-08 behaviour).
    "barge_in_min_words": (0.0, 10.0),
    # Sustained gated VAD speech that confirms a barge-in with NO transcript
    # (providers without interim transcripts cannot produce one while the
    # caller keeps talking). 0 disables the fallback.
    "barge_in_vad_fallback_secs": (0.0, 5.0),
    "user_speech_timeout": (0.2, 3.0),
    "finalize_grace": (0.0, 1.5),
    # How stale the newest STT final must be, at the moment the turn controller
    # closes the turn, for the finalize debounce to be skipped entirely. The
    # debounce exists to let straggler finals join; once they have demonstrably
    # stopped arriving, waiting again is pure dead time stacked on top of the
    # pause window that just elapsed.
    "finalize_settle": (0.0, 1.0),
    # Endpoint used when the utterance reads as a finished thought (a
    # self-contained short reply, or a sentence closed by terminal punctuation
    # with no trailing continuation cue). Applied INSTEAD of waiting out the
    # full pause window; an over-eager firing is absorbed by the brain's
    # late-final merge, never by talking over the caller.
    "complete_endpoint": (0.1, 1.5),
    # Endpoint for the narrower class of SELF-CONTAINED short replies
    # ("haan", "ji", "nahi", "ok", "ठीक है"). A closed sentence can still be
    # the first half of a longer thought, so it keeps complete_endpoint; a
    # one-word acknowledgement cannot, which is why it can fire sooner. This
    # is the turn the caller feels most: a fixed window makes the bot seem to
    # think hard about the word "yes".
    "short_reply_endpoint": (0.0, 1.0),
}

# ── caller-audio noise gate (voice_runtime.audio_gate) ───────────────────────
# Energy gating in front of the VAD. Values are per transport because PSTN
# audio is quieter, band-limited and noisier than a browser microphone.

NOISE_GATE_DEFAULTS: dict[str, dict[str, float]] = {
    "browser": {
        "enabled": 1.0,
        "noise_margin_db": 10.0,
        "min_speech_ms": 120.0,
        "echo_min_speech_ms": 180.0,
        "hangover_ms": 320.0,
        "preroll_ms": 160.0,
        "echo_margin_db": 6.0,
        "echo_tail_ms": 250.0,
        "min_threshold_dbfs": -50.0,
    },
    "telephony": {
        "enabled": 1.0,
        # A quieter, noisier line needs a slightly narrower margin so genuine
        # low-energy speech ("हाँ" on a bad handset) still opens the gate.
        "noise_margin_db": 8.0,
        "min_speech_ms": 120.0,
        "echo_min_speech_ms": 200.0,
        "hangover_ms": 350.0,
        "preroll_ms": 160.0,
        # The sustained-speech requirement (echo_min_speech_ms) already
        # filters echo blips; stacking a full 8 dB on the noise margin made
        # normal-volume callers inaudible while the bot spoke — the exact
        # "you must shout to interrupt" symptom.
        "echo_margin_db": 5.0,
        "echo_tail_ms": 300.0,
        # Must not be STRICTER than the (louder) browser medium: callers
        # between -50 and -45 dBFS were permanently inaudible on PSTN.
        "min_threshold_dbfs": -50.0,
    },
}

NOISE_GATE_BOUNDS: dict[str, tuple[float, float]] = {
    "enabled": (0.0, 1.0),
    "noise_margin_db": (3.0, 24.0),
    "min_speech_ms": (40.0, 500.0),
    "echo_min_speech_ms": (40.0, 800.0),
    "hangover_ms": (100.0, 1500.0),
    "preroll_ms": (0.0, 600.0),
    "echo_margin_db": (0.0, 24.0),
    "echo_tail_ms": (0.0, 1500.0),
    "min_threshold_dbfs": (-70.0, -20.0),
}


def validate_turn_detection(value: Any, *, prefix: str = "Turn detection") -> list[str]:
    """Validate the platform-owned ``stt_settings.turn_detection`` object."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{prefix}: must be an object."]
    return _validate_bounded(value, TURN_DETECTION_BOUNDS, prefix=prefix)


def validate_noise_gate(value: Any, *, prefix: str = "Noise gate") -> list[str]:
    """Validate the platform-owned ``stt_settings.noise_gate`` object."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"{prefix}: must be an object."]
    return _validate_bounded(value, NOISE_GATE_BOUNDS, prefix=prefix)


def _validate_bounded(
    value: dict, bounds_by_key: dict[str, tuple[float, float]], *, prefix: str
) -> list[str]:
    errors: list[str] = []
    for key in value:
        if key not in bounds_by_key:
            errors.append(f"{prefix}: unknown parameter '{key}'.")

    for key, raw in value.items():
        bounds = bounds_by_key.get(key)
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


def resolve_bounded(
    overrides: Any,
    defaults: dict[str, float],
    bounds_by_key: dict[str, tuple[float, float]],
    *,
    on_invalid=None,
) -> dict[str, float]:
    """Merge overrides onto defaults, clamping every value to its bounds.

    Shared by the runtime resolvers so a misconfigured (or maliciously large)
    value can never produce an unusable call — a 30 s endpoint or a gate that
    never opens. Junk values fall back to the default and are reported through
    ``on_invalid(key, value, default)`` for logging.
    """
    overrides = overrides if isinstance(overrides, dict) else {}
    resolved: dict[str, float] = {}
    for key, default in defaults.items():
        value = overrides.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            if on_invalid is not None:
                on_invalid(key, value, default)
            value = default
        low, high = bounds_by_key[key]
        resolved[key] = min(max(value, low), high)
    return resolved
