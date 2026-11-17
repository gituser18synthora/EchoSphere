"""Passive AI latency observations from existing, bounded turn telemetry.

No provider request or runtime-path mutation is involved. Missing spans are
unobserved, and latency measurements never establish provider availability.
"""

import asyncio
import math
from datetime import datetime, timedelta, timezone

from shared.config import get_settings
from shared.db.mongo import Mongo

WINDOW_MINUTES = 15
MAX_EVENTS = 1000
MIN_SAMPLES = 3
QUERY_TIMEOUT_SECONDS = 2.0
SERVER_MAX_TIME_MS = 1500

_STAGES = (
    ("llm-first-token", "LLM first token (p95)", "llm_ttft", "llm"),
    ("stt-finalization", "STT finalization (p95)", "stt_final", "stt"),
    ("tts-first-byte", "TTS first byte (p95)", "tts_ttfb", "tts"),
)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _observed_time(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value if now - timedelta(minutes=WINDOW_MINUTES) <= value <= now else None


def _p95(samples: list[float]) -> float | None:
    return sorted(samples)[math.ceil(len(samples) * .95) - 1] if samples else None


async def _read_events(now: datetime) -> list[dict]:
    async with asyncio.timeout(QUERY_TIMEOUT_SECONDS):
        cursor = Mongo.voice_events().find(
            {"kind": "turn_timing", "at": {"$gte": now - timedelta(minutes=WINDOW_MINUTES), "$lte": now}},
            {"_id": 0, "at": 1, "data.spans_ms": 1},
        ).sort("at", -1).limit(MAX_EVENTS).max_time_ms(SERVER_MAX_TIME_MS)
        try:
            return await cursor.to_list(length=MAX_EVENTS)
        finally:
            # A failed query must not leave a cursor alive. Keep cleanup bounded
            # even if the outer deadline has already cancelled the read.
            try:
                await asyncio.wait_for(cursor.close(), timeout=.1)
            except Exception:  # noqa: BLE001 - cleanup never hides the query result
                pass


async def probe_ai_latency() -> list[dict]:
    """Three HealthMetric-compatible p95 cards from at most 1000 recent events."""
    now = datetime.now(timezone.utc)
    settings = get_settings()
    error = None
    try:
        events = await _read_events(now)
    except Exception as exc:  # noqa: BLE001 - telemetry failure must not break other probes
        # Never return driver exception text, DSNs, credentials or raw events.
        error = type(exc).__name__
        events = []
    samples = {stage: [] for _, _, stage, _ in _STAGES}
    last_seen = {}
    for event in events[:MAX_EVENTS]:
        if not isinstance(event, dict):
            continue
        observed_at = _observed_time(event.get("at"), now)
        data = event.get("data")
        spans = data.get("spans_ms") if isinstance(data, dict) else None
        if observed_at is None or not isinstance(spans, dict):
            continue
        for stage in samples:
            value = spans.get(stage)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                numeric = float(value)
            except (OverflowError, ValueError):
                continue
            if math.isfinite(numeric) and numeric >= 0:
                samples[stage].append(numeric)
                last_seen[stage] = max(last_seen.get(stage, observed_at), observed_at)
    result = []
    events_read = min(len(events), MAX_EVENTS)
    for key, name, stage, setting_prefix in _STAGES:
        values = samples[stage]
        p95 = _p95(values)
        warning = getattr(settings, f"monitoring_ai_{setting_prefix}_warning_ms")
        critical = getattr(settings, f"monitoring_ai_{setting_prefix}_critical_ms")
        status = "neutral"
        if error:
            status, value = "warning", "Telemetry unavailable"
            detail = f"Recent turn telemetry could not be read ({error}). Provider availability is unobserved."
        elif p95 is None:
            value = "No data"
            detail = "No valid latency spans observed; provider availability is unobserved."
        else:
            value = f"{p95:.0f} ms"
            detail = f"p95 from {len(values)} valid latency spans."
            if len(values) < MIN_SAMPLES:
                detail += f" Insufficient samples for threshold classification (minimum {MIN_SAMPLES})."
            else:
                status = "critical" if p95 >= critical else "warning" if p95 >= warning else "good"
            detail += " Passive latency only; does not establish provider availability."
        detail += (f" Last {WINDOW_MINUTES} minutes; latest {events_read} turn events read (maximum {MAX_EVENTS})."
                   " Missing spans are not counted as failures.")
        if events_read == MAX_EVENTS:
            detail += " Sample cap reached; earlier turns in the window may be excluded."
        result.append({
            "key": key, "name": name, "group": "ai", "status": status, "value": value,
            "target": "Observed voice turns", "spark": [], "detail": detail,
            "checkedAt": _iso(now), "latencyMs": round(p95, 2) if p95 is not None else None,
            "observations": {"sampleSize": len(values), "minimumSamples": MIN_SAMPLES,
                "percentile": 95, "windowMinutes": WINDOW_MINUTES, "eventsRead": events_read,
                "eventLimit": MAX_EVENTS, "sampleCapped": events_read == MAX_EVENTS,
                "lastObservedAt": _iso(last_seen[stage]) if stage in last_seen else None,
                "warningThresholdMs": warning, "criticalThresholdMs": critical},
        })
    return result
