#!/usr/bin/env python
"""Phase 3 first-call check (READ-ONLY): what one call actually ran on.

For the newest call of a bot since ``--since`` (default: the last 2 hours) prints:
- the configured engines (call document ``models``: stt / tts / llm / decision);
- ``decision_warmup``: ok, latency, finished before the first decision;
- every decision: reported model id, service tier reported / requested,
  latency, input / output / cached tokens, whether it wrote response_text;
  decision fallbacks (timeouts) with model and tier;
- ``stt_keyterms``: model, sent, source, count;
- per turn: the TTS engine of the first audio (``turn_timing.tts_model``),
  tts_ttfb and response time;
- ``tts_cold_connect`` (reason, waited ms) and ``filler_only_dropped`` events;
- usage counters for reply and decision calls.

Usage (on the server, from the checkout):
    env/bin/python scratchpad/phase3_first_call.py --bot bot_59a84478f155
    env/bin/python scratchpad/phase3_first_call.py --bot bot_450a4f7b4832 --since 2026-10-27T10:00:00+05:30
Connection: MONGODB_URI / MONGODB_DATABASE from .env unless --mongo-uri / --db are given.
Only ``find`` queries are issued.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient  # noqa: E402


def _ms(value) -> str:
    return "-" if not isinstance(value, (int, float)) else f"{value:,.0f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--bot", required=True)
    parser.add_argument("--since", default=None, help="ISO datetime; default: 2 hours ago")
    parser.add_argument("--mongo-uri", default=None)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    uri, database = args.mongo_uri, args.db
    if uri is None or database is None:
        from shared.config import get_settings  # reads .env

        settings = get_settings()
        uri = uri or settings.mongodb_uri
        database = database or settings.mongodb_database
    since = (datetime.fromisoformat(args.since) if args.since
             else datetime.now(timezone.utc) - timedelta(hours=2))
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        doc = client[database]["conversation_transcripts"].find_one(
            {"bot_id": args.bot, "started_at": {"$gte": since.astimezone(timezone.utc)}},
            sort=[("started_at", -1)],
        )
    finally:
        client.close()
    if doc is None:
        print(f"no call for {args.bot} since {since.isoformat()}")
        return 1

    print(f"call {doc.get('control_plane_id')} / {doc.get('session_id')}  channel={doc.get('channel')}  "
          f"started={doc.get('started_at')}  duration={doc.get('duration_sec')} s  end={doc.get('end_reason')}")
    models = doc.get("models") or {}
    print("models:", ", ".join(f"{k}={v}" for k, v in models.items()) or "(not recorded: pre-Phase 3 build)")

    events = doc.get("events") or []
    by_kind: dict[str, list[dict]] = {}
    for ev in events:
        by_kind.setdefault(str(ev.get("kind")), []).append(ev)

    for ev in by_kind.get("decision_warmup", []):
        print(f"decision_warmup: ok={ev.get('ok')} latency={_ms(ev.get('latency_ms'))} ms "
              f"before_first_decision={ev.get('before_first_decision')} error={ev.get('error')}")
    decisions = by_kind.get("orchestration_decision", [])
    print(f"decisions: {len(decisions)}")
    for i, ev in enumerate(decisions, 1):
        print(f"  #{i} model={ev.get('model')} tier={ev.get('service_tier')} "
              f"requested={ev.get('service_tier_requested')} latency={_ms(ev.get('latency_ms'))} ms "
              f"tokens in/out/cached={ev.get('input_tokens')}/{ev.get('output_tokens')}/{ev.get('cached_tokens')} "
              f"response_text={ev.get('has_response_text')} signal={ev.get('signal')} intent={ev.get('intent')}")
    for ev in by_kind.get("orchestration_fallback", []):
        print(f"  fallback reason={ev.get('reason')} model={ev.get('model')} "
              f"requested_tier={ev.get('service_tier_requested')}")
    for ev in by_kind.get("stt_keyterms", []):
        print(f"stt_keyterms: model={ev.get('model')} sent={ev.get('sent')} source={ev.get('source')} "
              f"count={ev.get('count')} reason={ev.get('reason')}")
    print("turns (first-audio engine, tts_ttfb, response):")
    for ev in by_kind.get("turn_timing", []):
        spans = ev.get("spans_ms") or {}
        print(f"  turn {ev.get('turn_id')}: tts_model={ev.get('tts_model')} "
              f"tts_ttfb={_ms(spans.get('tts_ttfb'))} ms response={_ms(spans.get('response'))} ms "
              f"endpoint_by={ev.get('endpoint_by')}")
    cold = by_kind.get("tts_cold_connect", [])
    print(f"tts_cold_connect: {len(cold)}" + "".join(
        f"\n  reason={ev.get('reason')} waited={_ms(ev.get('waited_ms'))} ms model={ev.get('model')}" for ev in cold))
    dropped = by_kind.get("filler_only_dropped", [])
    print(f"filler_only_dropped: {len(dropped)}" + "".join(
        f"\n  text={ev.get('text')!r} pending={ev.get('pending_kind')}" for ev in dropped))
    usage = doc.get("usage") or {}
    print("usage: reply llm requests/in/out/cached="
          f"{usage.get('llm_requests')}/{usage.get('llm_input_tokens')}/{usage.get('llm_output_tokens')}/"
          f"{usage.get('llm_cached_tokens')}  decision requests/in/out/cached="
          f"{usage.get('decision_llm_requests')}/{usage.get('decision_llm_input_tokens')}/"
          f"{usage.get('decision_llm_output_tokens')}/{usage.get('decision_llm_cached_tokens')}")
    errors = [k for k in by_kind if k in ("tts_fatal", "tts_failure", "llm_error", "generation_failed")]
    if errors:
        print("ERRORS:", {k: len(by_kind[k]) for k in errors})
    return 0


if __name__ == "__main__":
    sys.exit(main())
