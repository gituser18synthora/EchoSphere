"""Passive latency checks use fake telemetry cursors, never a provider/datastore."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.core import ai_health as ai
from shared.db import mongo


class Cursor:
    def __init__(self, events, *, error=None, stall=False):
        self.events, self.error, self.stall = events, error, stall
        self.sorting = self.limit_value = self.max_time = self.read_length = None
        self.closed = False

    def sort(self, name, direction):
        self.sorting = (name, direction)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def max_time_ms(self, value):
        self.max_time = value
        return self

    async def to_list(self, length):
        self.read_length = length
        if self.stall:
            await asyncio.Event().wait()
        if self.error:
            raise self.error
        return self.events

    async def close(self):
        self.closed = True


class Events:
    def __init__(self, cursor):
        self.cursor = cursor
        self.query = self.projection = None

    def find(self, query, projection):
        self.query, self.projection = query, projection
        return self.cursor


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    thresholds = {f"monitoring_ai_{stage}_{level}_ms": value
                  for stage in ("llm", "stt", "tts") for level, value in (("warning", 100), ("critical", 200))}
    monkeypatch.setattr(ai, "get_settings", lambda: SimpleNamespace(**thresholds))


def event(spans=None, *, at=None, **extra):
    return {"at": at or datetime.now(timezone.utc) - timedelta(minutes=1),
            "data": {"spans_ms": spans or {}}, **extra}


def source(monkeypatch, events, **options):
    cursor = Cursor(events, **options)
    collection = Events(cursor)
    monkeypatch.setattr(ai.Mongo, "voice_events", lambda: collection)
    return collection, cursor


async def test_read_is_projected_bounded_recent_and_does_not_expose_event_identity(monkeypatch):
    collection, cursor = source(monkeypatch, [event({"llm_ttft": 25}, tenant_id="PRIVATE_TENANT", session_id="PRIVATE_SESSION",
                                                  caller="PRIVATE_CALLER")])
    before = datetime.now(timezone.utc)
    rows = await ai.probe_ai_latency()
    after = datetime.now(timezone.utc)
    assert collection.query["kind"] == "turn_timing"
    assert before - timedelta(minutes=15) <= collection.query["at"]["$gte"] <= after - timedelta(minutes=15)
    assert before <= collection.query["at"]["$lte"] <= after
    assert collection.projection == {"_id": 0, "at": 1, "data.spans_ms": 1}
    assert cursor.sorting == ("at", -1)
    assert cursor.limit_value == cursor.read_length == 1000
    assert cursor.max_time == 1500 and cursor.closed
    assert {row["key"] for row in rows} == {"llm-first-token", "stt-finalization", "tts-first-byte"}
    assert all(row["group"] == "ai" and row["spark"] == [] for row in rows)
    assert "PRIVATE" not in json.dumps(rows)


async def test_nearest_rank_p95_is_per_stage_and_exposes_sample_sizes(monkeypatch):
    events = [event({"llm_ttft": i * 10, "stt_final": i, "tts_ttfb": i * 100}) for i in range(1, 21)]
    source(monkeypatch, events)
    llm, stt, tts = await ai.probe_ai_latency()
    assert (llm["latencyMs"], stt["latencyMs"], tts["latencyMs"]) == (190, 19, 1900)
    assert (llm["status"], stt["status"], tts["status"]) == ("warning", "good", "critical")
    assert llm["observations"]["sampleSize"] == 20
    assert llm["observations"]["percentile"] == 95
    assert "does not establish provider availability" in llm["detail"]
    assert "Missing spans are not counted as failures" in llm["detail"]


@pytest.mark.parametrize("latency,status", [(99, "good"), (100, "warning"), (199, "warning"), (200, "critical")])
async def test_threshold_boundaries_require_three_observations(monkeypatch, latency, status):
    source(monkeypatch, [event({"llm_ttft": latency}) for _ in range(3)])
    llm, _, _ = await ai.probe_ai_latency()
    assert llm["status"] == status
    assert llm["observations"]["warningThresholdMs"] == 100
    assert llm["observations"]["criticalThresholdMs"] == 200


async def test_few_samples_keep_measured_p95_but_do_not_claim_healthy_or_outage(monkeypatch):
    source(monkeypatch, [event({"llm_ttft": 9000, "stt_final": 0}), event({"llm_ttft": 8000})])
    llm, stt, tts = await ai.probe_ai_latency()
    assert llm["status"] == stt["status"] == tts["status"] == "neutral"
    assert llm["latencyMs"] == 9000 and "Insufficient samples" in llm["detail"]
    assert stt["latencyMs"] == 0 and stt["observations"]["sampleSize"] == 1
    assert tts["latencyMs"] is None and tts["value"] == "No data"


async def test_missing_invalid_and_nonfinite_spans_are_unobserved(monkeypatch):
    invalid = [True, False, "500", None, -1, float("inf"), float("nan"), 10 ** 1000]
    rows = [event({"llm_ttft": value, "stt_final": value, "tts_ttfb": value}) for value in invalid]
    rows += [None, {}, {"at": datetime.now(timezone.utc), "data": []}, event()]
    source(monkeypatch, rows)
    result = await ai.probe_ai_latency()
    assert all(row["status"] == "neutral" and row["latencyMs"] is None for row in result)
    assert all(row["observations"]["sampleSize"] == 0 for row in result)
    assert all("provider availability is unobserved" in row["detail"] for row in result)


async def test_out_of_window_and_future_rows_are_excluded_and_naive_utc_is_supported(monkeypatch):
    now = datetime.now(timezone.utc)
    source(monkeypatch, [event({"llm_ttft": 9999}, at=now - timedelta(minutes=16)),
                         event({"llm_ttft": 9999}, at=now + timedelta(minutes=1)),
                         event({"llm_ttft": 50}, at=(now - timedelta(minutes=1)).replace(tzinfo=None)),
                         event({"llm_ttft": 50}, at=(now - timedelta(minutes=1)).astimezone(timezone(timedelta(hours=5, minutes=30)))),
                         event({"llm_ttft": 50}, at=now - timedelta(seconds=30))])
    llm, _, _ = await ai.probe_ai_latency()
    assert llm["latencyMs"] == 50 and llm["status"] == "good"
    assert llm["observations"]["sampleSize"] == 3
    assert llm["observations"]["lastObservedAt"].endswith("Z")


async def test_sample_cap_is_explicit_and_old_excess_rows_cannot_affect_p95(monkeypatch):
    source(monkeypatch, [event({"llm_ttft": 50}) for _ in range(1000)] + [event({"llm_ttft": 99999}) for _ in range(100)])
    llm, _, _ = await ai.probe_ai_latency()
    assert llm["latencyMs"] == 50
    assert llm["observations"]["sampleSize"] == llm["observations"]["eventsRead"] == 1000
    assert llm["observations"]["sampleCapped"] is True
    assert "earlier turns in the window may be excluded" in llm["detail"]


async def test_empty_telemetry_is_neutral_not_a_provider_failure(monkeypatch):
    source(monkeypatch, [])
    rows = await ai.probe_ai_latency()
    assert all(row["status"] == "neutral" and row["value"] == "No data" for row in rows)
    assert all(row["observations"]["eventsRead"] == 0 for row in rows)


async def test_query_failure_is_visible_and_driver_secrets_are_not_exposed(monkeypatch):
    _, cursor = source(monkeypatch, [], error=RuntimeError("mongodb://user:VERY_PRIVATE@internal/private"))
    rows = await ai.probe_ai_latency()
    assert all(row["status"] == "warning" and row["value"] == "Telemetry unavailable" for row in rows)
    assert all("RuntimeError" in row["detail"] and row["latencyMs"] is None for row in rows)
    assert "VERY_PRIVATE" not in json.dumps(rows) and cursor.closed


async def test_stalled_read_has_a_deadline_and_cursor_is_closed(monkeypatch):
    _, cursor = source(monkeypatch, [], stall=True)
    monkeypatch.setattr(ai, "QUERY_TIMEOUT_SECONDS", .02)
    rows = await asyncio.wait_for(ai.probe_ai_latency(), timeout=.5)
    assert all(row["status"] == "warning" and "TimeoutError" in row["detail"] for row in rows)
    assert cursor.closed


async def test_mongo_bootstrap_defines_index_matching_the_existing_turn_event_shape(monkeypatch):
    class Collection:
        def __init__(self):
            self.indexes = []

        async def create_index(self, fields, **options):
            self.indexes.append((fields, options))

    collections = {name: Collection() for name in (mongo.COLLECTION_TRANSCRIPTS, mongo.COLLECTION_VOICE_EVENTS)}
    monkeypatch.setattr(mongo.Mongo, "db", lambda: collections)
    await mongo.create_indexes()
    assert ([("kind", 1), ("at", -1)], {}) in collections[mongo.COLLECTION_VOICE_EVENTS].indexes
