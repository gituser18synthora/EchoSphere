#!/usr/bin/env python
"""Phase 3 first-call check (READ-ONLY): what one call actually ran on.

For the newest call of a bot since ``--since`` (default: the last 2 hours) prints:
- the configured engines (call document ``models``: stt / tts / llm / decision);
- the conversation engine's request options and the tiers OpenAI reported
  (``models.llm_options`` / ``models.llm_service_tiers``);
- ``decision_warmup``: ok, latency, finished before the first decision and
  while the greeting was still playing;
- every decision: reported model id, service tier reported / requested,
  latency, budget, input / output / cached tokens, whether it wrote
  response_text; decision fallbacks (timeouts) with model, tier and budget;
- ``stt_keyterms``: model, sent, source, count;
- per turn: the TTS engine of the first audio (``turn_timing.tts_model``),
  tts_ttfb and response time;
- ``tts_startup_connect`` (how long the start waited for the first socket),
  ``tts_cold_connect`` (reason, waited ms) and ``filler_only_dropped`` events;
- usage counters for reply and decision calls;
- Phase 4: per turn the path it took (deterministic / LLM decision /
  speculative kept / speculative discarded / decision fallback) next to its
  response time; ``decision_skipped_deterministic`` (resolver, ask kind),
  ``speculative_reply_kept`` (lead) / ``_discarded`` (reason, chars),
  ``extraction_ahead`` outcomes, ``workflow_timing`` (workflow step after the
  decision) and grounded-rewrite ``first_ms`` (the MDND stage split),
  ``early_ack_played`` (``early`` = the early_ack_ms slot) and
  ``early_ack_not_ready``; the speculative-discard usage counters.

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
from collections import Counter
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
              f"before_first_decision={ev.get('before_first_decision')} "
              f"greeting_playing={ev.get('greeting_playing')} error={ev.get('error')}")
    decisions = by_kind.get("orchestration_decision", [])
    print(f"decisions: {len(decisions)}")
    for i, ev in enumerate(decisions, 1):
        print(f"  #{i} model={ev.get('model')} tier={ev.get('service_tier')} "
              f"requested={ev.get('service_tier_requested')} latency={_ms(ev.get('latency_ms'))} ms "
              f"budget={_ms(ev.get('budget_ms'))} ms "
              f"tokens in/out/cached={ev.get('input_tokens')}/{ev.get('output_tokens')}/{ev.get('cached_tokens')} "
              f"response_text={ev.get('has_response_text')} signal={ev.get('signal')} intent={ev.get('intent')}")
    for ev in by_kind.get("orchestration_fallback", []):
        print(f"  fallback reason={ev.get('reason')} model={ev.get('model')} "
              f"requested_tier={ev.get('service_tier_requested')} budget={_ms(ev.get('budget_ms'))} ms")
    for ev in by_kind.get("stt_keyterms", []):
        print(f"stt_keyterms: model={ev.get('model')} sent={ev.get('sent')} source={ev.get('source')} "
              f"count={ev.get('count')} reason={ev.get('reason')}")
    print("turns (path, first-audio engine, tts_ttfb, response; MDND stages):")
    bucket: list[dict] = []
    for ev in events:
        if ev.get("kind") != "turn_timing":
            bucket.append(ev)
            continue
        turn_events, bucket = bucket, []
        kinds = {e.get("kind") for e in turn_events}
        path = ("deterministic" if kinds & {"decision_skipped_deterministic", "deterministic_fast_path"}
                else "speculative kept" if "speculative_reply_kept" in kinds
                else "speculative discarded" if "speculative_reply_discarded" in kinds
                else "LLM decision" if "orchestration_decision" in kinds
                else "decision fallback" if "orchestration_fallback" in kinds else "-")
        spans = ev.get("spans_ms") or {}
        dispatched, classified = ev.get("turn_dispatched_at") or 0, ev.get("classify_completed_at") or 0
        workflow = next((e.get("ms") for e in turn_events if e.get("kind") == "workflow_timing"), None)
        rewrite = next((e.get("first_ms") for e in turn_events if e.get("kind") in (
            "workflow_reply_grounded", "workflow_grounded_fallback")), None)
        print(f"  turn {ev.get('turn_id')}: path={path} tts_model={ev.get('tts_model')} "
              f"tts_ttfb={_ms(spans.get('tts_ttfb'))} ms response={_ms(spans.get('response'))} ms "
              f"endpoint_by={ev.get('endpoint_by')} | decision="
              f"{_ms(classified - dispatched) if dispatched and classified else '-'} "
              f"workflow={_ms(workflow)} rewrite_first={_ms(rewrite)}")
    for ev in by_kind.get("decision_skipped_deterministic", []):
        print(f"decision_skipped_deterministic: resolver={ev.get('resolver')} ask_kind={ev.get('ask_kind')} "
              f"signal={ev.get('signal')} value={ev.get('value')}")
    for ev in by_kind.get("speculative_reply_kept", []):
        print(f"speculative_reply_kept: lead={_ms(ev.get('lead_ms'))} ms "
              f"first_token_ready={ev.get('first_token_ready')} chars={ev.get('buffered_chars')}")
    for ev in by_kind.get("speculative_reply_discarded", []):
        print(f"speculative_reply_discarded: reason={ev.get('reason')} ran={_ms(ev.get('ran_ms'))} ms "
              f"chars={ev.get('generated_chars')} completed={ev.get('completed')}")
    ahead = Counter(str(ev.get("outcome")) for ev in by_kind.get("extraction_ahead", []))
    print("extraction_ahead:", dict(ahead) or "-")
    acks = by_kind.get("early_ack_played", [])
    print(f"early_ack_played: {len(acks)} (early_ack_ms slot: {sum(bool(e.get('early')) for e in acks)})"
          f"  early_ack_not_ready: {len(by_kind.get('early_ack_not_ready', []))}" + "".join(
              f"\n  turn={e.get('turn')} waited={_ms(e.get('waited_ms'))} ms early={e.get('early')} "
              f"context={e.get('context')}" for e in acks))
    for ev in by_kind.get("tts_startup_connect", []):
        print(f"tts_startup_connect: held the start {_ms(ev.get('held_ms'))} ms timed_out={ev.get('timed_out')}")
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
          f"{usage.get('decision_llm_output_tokens')}/{usage.get('decision_llm_cached_tokens')}"
          f"  speculative discarded requests/~output tokens="
          f"{usage.get('speculative_discarded_requests', 0)}/"
          f"{usage.get('speculative_discarded_output_tokens_est', 0)}")
    errors = [k for k in by_kind if k in ("tts_fatal", "tts_failure", "llm_error", "generation_failed")]
    if errors:
        print("ERRORS:", {k: len(by_kind[k]) for k in errors})
    return 0


if __name__ == "__main__":
    sys.exit(main())
