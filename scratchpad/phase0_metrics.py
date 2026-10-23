#!/usr/bin/env python
"""Phase 0 latency / decision metrics over live telephony turns (READ-ONLY).

Reads the per-call documents the voice worker writes to MongoDB
(``conversation_transcripts``: one document per session with ``turns`` and
``events``, see voice_runtime/recording.py ``SessionRecorder.finalize``) and
prints a compact table for the last ``--days`` days of telephony calls.

Event shapes used (all written by voice_runtime/brain.py):
- ``turn_timing``   — ``spans_ms`` {endpoint, classify, llm_ttft, tts_ttfb,
                      response, ...} per turn (voice_runtime/turn_metrics.py
                      ``TurnLatencyTracker.structured``)
- ``turn_latency``  — the same spans flat, plus ``counts`` such as
                      ``decision_prefetched`` (prefetch finished before dispatch)
- ``orchestration_decision`` / ``orchestration_fallback`` (``reason``:
                      timeout | disabled_after_timeouts | provider_error |
                      unparseable_output | engine_disabled | empty_turn)
- ``route_decision`` — ``route`` + ``reason`` (``too_short`` = canned CLARIFY)

Usage:
    env/bin/python scratchpad/phase0_metrics.py --days 14
    env/bin/python scratchpad/phase0_metrics.py --since 2026-10-22T00:00:00+05:30   # overrides --days
    env/bin/python scratchpad/phase0_metrics.py --days 7 --mongo-uri mongodb://host:27017/?directConnection=true --db voice_bot
    env/bin/python scratchpad/phase0_metrics.py --days 14 --channel all   # include browser sessions

Connection: ``MONGODB_URI`` / ``MONGODB_DATABASE`` from .env via shared.config
unless overridden on the command line. The script only issues ``find`` queries.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

SPANS = ("response", "endpoint", "classify", "llm_ttft", "tts_ttfb")
ENDPOINT_KINDS = ("smart_turn", "pause_timeout", "lexical_shortcut", "finalize_grace", "unknown")
TIMEOUT_REASONS = ("timeout", "timeout_before_warmup")
SPAN_LABELS = {
    "response": "response (caller stops -> first bot audio)",
    "endpoint": "endpoint (speech end -> dispatch)",
    "classify": "classify (decision LLM)",
    "llm_ttft": "llm_ttft (reply first token)",
    "tts_ttfb": "tts_ttfb (TTS first byte)",
}


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return cuts[int(q * 100) - 1]


def _fmt(v: float | None, unit: str = "") -> str:
    return "-" if v is None else f"{v:,.0f}{unit}"


def _share(num: int, den: int) -> str:
    return "-" if not den else f"{100.0 * num / den:5.1f} % ({num}/{den})"


def _word_count(text: str) -> int:
    return len([w for w in (text or "").replace("।", " ").split() if w.strip(".,!?")])


async def collect(uri: str, database: str, days: int, channel: str,
                  since: datetime | None = None) -> dict:
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    try:
        db = client[database]
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=days)
        query: dict = {"started_at": {"$gte": since}}
        if channel == "telephony":
            query["channel"] = {"$ne": "browser"}
        elif channel != "all":
            query["channel"] = channel
        projection = {"session_id": 1, "channel": 1, "bot_id": 1, "events": 1,
                      "turns.role": 1, "turns.text": 1, "turns.route": 1}
        docs = [d async for d in db["conversation_transcripts"].find(query, projection)]
    finally:
        client.close()

    spans: dict[str, list[float]] = {name: [] for name in SPANS}
    turns_with_timing = 0
    understanding_turns = 0          # turns with a classify span (went through stage A)
    prefetch_hits = 0
    calls_with_engine = 0            # calls that attempted at least one decision
    calls_first_timeout = 0
    calls_never_decided = 0
    calls_disabled = 0
    short_turns = 0
    short_clarify_routes = 0
    too_short_reasons = 0
    short_answer_routes = 0          # Phase 1: <=2-word reply answered in context
    endpoint_by: dict[str, list[float]] = {k: [] for k in ENDPOINT_KINDS}   # Phase 2
    # Smart Turn sub-marks (ms after the VAD stop) on turns Smart Turn ended:
    # where the endpoint time goes — the verdict, the STT final, the dispatch.
    endpoint_marks: dict[str, list[float]] = {"verdict": [], "final": [], "dispatch": []}
    short_circuits = 0
    warmup_latencies: list[float] = []
    warmup_hits = 0
    warmup_total = 0
    filler_not_ready = 0
    channels: dict[str, int] = {}

    for doc in docs:
        channels[str(doc.get("channel"))] = channels.get(str(doc.get("channel")), 0) + 1
        events = doc.get("events") or []
        first_attempt: str | None = None
        decided = False
        disabled = False
        attempted = False
        for ev in events:
            kind = ev.get("kind")
            if kind == "turn_timing":
                turns_with_timing += 1
                ms = ev.get("spans_ms") or {}
                for name in SPANS:
                    value = ms.get(name)
                    if isinstance(value, (int, float)):
                        spans[name].append(float(value))
                if isinstance(ms.get("classify"), (int, float)):
                    understanding_turns += 1
                kind_by = str(ev.get("endpoint_by") or "unknown")
                if isinstance(ms.get("endpoint"), (int, float)):
                    endpoint_by.setdefault(kind_by, []).append(float(ms["endpoint"]))
                if kind_by == "smart_turn":
                    marks = ev.get("endpoint_marks_ms") or {}
                    for name in endpoint_marks:
                        if isinstance(marks.get(name), (int, float)):
                            endpoint_marks[name].append(float(marks[name]))
            elif kind == "smart_turn_short_circuit":
                short_circuits += 1
            elif kind == "turn_latency":
                counts = ev.get("counts") or {}
                if counts.get("decision_prefetched"):
                    prefetch_hits += 1
            elif kind == "orchestration_decision":
                attempted = True
                decided = True
                if first_attempt is None:
                    first_attempt = "decision"
            elif kind == "orchestration_fallback":
                reason = str(ev.get("reason") or "")
                if reason in ("engine_disabled", "empty_turn"):
                    continue
                attempted = True
                if first_attempt is None:
                    first_attempt = "timeout" if reason in TIMEOUT_REASONS else reason
                if reason == "disabled_after_timeouts":
                    disabled = True
            elif kind == "route_decision" and ev.get("reason") == "too_short":
                too_short_reasons += 1
            elif kind == "route_decision" and ev.get("reason") == "short_answer_to_pending":
                short_answer_routes += 1
            elif kind == "decision_warmup":
                warmup_total += 1
                if isinstance(ev.get("latency_ms"), (int, float)):
                    warmup_latencies.append(float(ev["latency_ms"]))
                if ev.get("ok") and ev.get("before_first_decision"):
                    warmup_hits += 1
            elif kind == "latency_filler_not_ready":
                filler_not_ready += 1
        if attempted:
            calls_with_engine += 1
            if first_attempt == "timeout":
                calls_first_timeout += 1
            if not decided:
                calls_never_decided += 1
            if disabled:
                calls_disabled += 1
        for turn in doc.get("turns") or []:
            if turn.get("role") != "user":
                continue
            if _word_count(turn.get("text", "")) <= 2:
                short_turns += 1
                if turn.get("route") == "clarify":
                    short_clarify_routes += 1

    return {
        "days": days, "since": since, "calls": len(docs), "channels": channels,
        "turns": turns_with_timing, "spans": spans,
        "understanding_turns": understanding_turns, "prefetch_hits": prefetch_hits,
        "calls_with_engine": calls_with_engine, "calls_first_timeout": calls_first_timeout,
        "calls_never_decided": calls_never_decided, "calls_disabled": calls_disabled,
        "short_turns": short_turns, "short_clarify_routes": short_clarify_routes,
        "too_short_reasons": too_short_reasons,
        "short_answer_routes": short_answer_routes,
        "endpoint_by": endpoint_by, "warmup_latencies": warmup_latencies,
        "endpoint_marks": endpoint_marks, "short_circuits": short_circuits,
        "warmup_hits": warmup_hits, "warmup_total": warmup_total,
        "filler_not_ready": filler_not_ready,
    }


def print_report(r: dict, channel: str) -> None:
    print(f"Phase 0 metrics — {channel} calls since {r['since']:%Y-%m-%d %H:%M} UTC ({r['days']} d)")
    print(f"calls: {r['calls']}   turns with timing: {r['turns']}   channels: "
          + ", ".join(f"{k}={v}" for k, v in sorted(r["channels"].items())))
    print()
    print(f"{'span (ms)':<44}{'p50':>9}{'p90':>9}{'n':>8}")
    for name in SPANS:
        values = sorted(r["spans"][name])
        print(f"{SPAN_LABELS[name]:<44}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
    print()
    print(f"{'endpoint (ms) by endpoint_by':<44}{'p50':>9}{'p90':>9}{'n':>8}")
    total_ended = sum(len(v) for v in r["endpoint_by"].values())
    for kind in sorted(r["endpoint_by"], key=lambda k: -len(r["endpoint_by"][k])):
        values = sorted(r["endpoint_by"][kind])
        if not values:
            continue
        print(f"  {kind:<42}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
    print(f"{'turns ended by smart_turn':<44}{_share(len(r['endpoint_by'].get('smart_turn', [])), total_ended)}")
    marks = r.get("endpoint_marks") or {}
    if any(marks.values()):
        print(f"{'smart_turn marks after VAD stop (ms)':<44}{'p50':>9}{'p90':>9}{'n':>8}")
        for name in ("verdict", "final", "dispatch"):
            values = sorted(marks.get(name, []))
            print(f"  {name:<42}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
        print(f"{'smart_turn_short_circuit events':<44}{r.get('short_circuits', 0)}")
    print()
    print(f"{'decision ready at dispatch (prefetch hit)':<44}{_share(r['prefetch_hits'], r['understanding_turns'])}")
    print(f"{'calls: 1st-turn decision timed out':<44}{_share(r['calls_first_timeout'], r['calls_with_engine'])}")
    print(f"{'decision_warmup p50 (ms)':<44}{_fmt(_pct(sorted(r['warmup_latencies']), 0.5))}   n={r['warmup_total']}")
    print(f"{'decision_warmup finished before 1st decision':<44}{_share(r['warmup_hits'], r['warmup_total'])}")
    print(f"{'latency_filler_not_ready events':<44}{r['filler_not_ready']}")
    print(f"{'calls: never got a decision':<44}{_share(r['calls_never_decided'], r['calls_with_engine'])}")
    print(f"{'calls: engine self-disabled (3 timeouts)':<44}{_share(r['calls_disabled'], r['calls_with_engine'])}")
    print(f"{'<=2-word caller turns routed to CLARIFY':<44}{_share(r['short_clarify_routes'], r['short_turns'])}")
    print(f"{'route_decision reason=too_short events':<44}{r['too_short_reasons']}")
    print(f"{'route_decision reason=short_answer_to_pending':<44}{r['short_answer_routes']}")
    print()
    print("notes: the route reason IS recorded (route_decision.reason); turns.route='clarify' on a <=2-word")
    print("       user turn is the canned too_short path outside a workflow. Medians per span do not sum to the")
    print("       response median. Decision attempts exclude engine_disabled/empty_turn fallbacks.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--since", default=None,
                        help="ISO datetime (e.g. 2026-10-22T00:00:00+05:30); overrides --days")
    parser.add_argument("--mongo-uri", default=None, help="override MONGODB_URI from .env")
    parser.add_argument("--db", default=None, help="override MONGODB_DATABASE from .env")
    parser.add_argument("--channel", default="telephony",
                        help="telephony (default: every channel except browser) | all | an exact channel name")
    args = parser.parse_args()
    uri, database = args.mongo_uri, args.db
    if uri is None or database is None:
        from shared.config import get_settings  # reads .env

        settings = get_settings()
        uri = uri or settings.mongodb_uri
        database = database or settings.mongodb_database
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    report = asyncio.run(collect(uri, database, args.days, args.channel, since=since))
    print_report(report, args.channel)
    print(f"notes: 1st-turn timeout counts reasons {TIMEOUT_REASONS}; the first orchestration event of a call is its first turn's attempt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
