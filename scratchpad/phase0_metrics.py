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
- Phase 3: ``orchestration_decision`` / ``_fallback`` carry ``model``,
  ``service_tier`` (reported), ``service_tier_requested``; decisions also
  ``latency_ms``, ``input_tokens``, ``output_tokens``, ``cached_tokens``,
  ``has_response_text``. ``turn_timing.tts_model`` = provider/model of the
  turn's first audio; the call document's ``models`` = configured engines
  (stt/tts/llm/decision) and ``usage.llm_cached_tokens`` / ``llm_input_tokens``
  the reply calls' prompt-cache share. ``tts_cold_connect`` (``reason``,
  ``waited_ms``) and ``filler_only_dropped`` are counted.
- Phase 4: ``decision_skipped_deterministic`` (resolver, ask_kind),
  ``speculative_reply_kept`` / ``speculative_reply_discarded`` (reason,
  generated_chars), ``extraction_ahead`` (outcome), ``workflow_timing`` (ms of
  the workflow step after the decision), ``workflow_reply_grounded`` /
  ``workflow_grounded_fallback`` (``first_ms`` = first spoken sentence of a
  grounded rewrite), ``early_ack_played`` (``early`` = the bot's early_ack_ms
  slot) and ``early_ack_not_ready``. The response is split by the path the
  turn took; the MDND stage split covers bot_59a84478f155 (``--mdnd-bot``).

Usage:
    env/bin/python scratchpad/phase0_metrics.py --days 14
    env/bin/python scratchpad/phase0_metrics.py --since 2026-10-22T00:00:00+05:30   # overrides --days
    env/bin/python scratchpad/phase0_metrics.py --days 7 --mongo-uri mongodb://host:27017/?directConnection=true --db voice_bot
    env/bin/python scratchpad/phase0_metrics.py --days 14 --channel all   # include browser sessions
    env/bin/python scratchpad/phase0_metrics.py --days 2 --bot bot_59a84478f155   # one bot

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
ENDPOINT_KINDS = ("smart_turn", "pause_timeout", "lexical_shortcut", "finalize_grace", "transcript_timeout", "unknown")
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


PATHS = ("deterministic", "LLM decision", "speculative kept", "speculative discarded",
         "decision fallback")


def _turn_path(bucket: list[dict]) -> str | None:
    """The path a turn took, from the events between two turn_timing records."""
    kinds = {e.get("kind") for e in bucket}
    if "decision_skipped_deterministic" in kinds or "deterministic_fast_path" in kinds:
        return "deterministic"
    if "speculative_reply_kept" in kinds:
        return "speculative kept"
    if "speculative_reply_discarded" in kinds:
        return "speculative discarded"
    if "orchestration_decision" in kinds:
        return "LLM decision"
    if any(e.get("kind") == "orchestration_fallback"
           and e.get("reason") not in ("engine_disabled", "empty_turn") for e in bucket):
        return "decision fallback"
    return None


async def collect(uri: str, database: str, days: int, channel: str,
                  since: datetime | None = None, bot: str | None = None,
                  mdnd_bot: str = "bot_59a84478f155") -> dict:
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
        if bot:
            query["bot_id"] = bot
        projection = {"session_id": 1, "channel": 1, "bot_id": 1, "events": 1,
                      "turns.role": 1, "turns.text": 1, "turns.route": 1,
                      "models": 1, "usage": 1, "tts_usage": 1}
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
    # Phase 3 — models.
    decision_ms: dict[str, list[float]] = {}        # "model @ tier" -> decision latency
    decision_timeouts: dict[str, int] = {}          # "model @ tier" -> timeout fallbacks
    decision_out_tokens: list[float] = []
    decision_in = decision_cached = 0
    decisions_with_text = decisions_total = 0
    reply_in = reply_cached = 0
    ttfb_by_model: dict[str, list[float]] = {}
    stt_models: dict[str, int] = {}
    cold_connects: dict[str, list[float]] = {}      # reason -> waited_ms
    filler_only_dropped = 0
    # Phase 4.
    path_response: dict[str, list[float]] = {name: [] for name in PATHS}
    skipped_by: dict[str, int] = {}                 # resolver/ask_kind -> turns
    decision_turns = 0                              # turns that awaited or skipped a decision
    discard_reasons: dict[str, int] = {}
    discard_chars = 0
    kept_lead: list[float] = []
    ahead_outcomes: dict[str, int] = {}
    early_acks = early_acks_slot = early_ack_not_ready = 0
    out_tokens_by_schema: dict[str, list[float]] = {"with reason": [], "without reason": []}
    mdnd_stages: dict[str, list[float]] = {
        "decision": [], "workflow after decision": [], "rewrite first sentence": [],
        "tts first audio": [], "response": [],
    }

    for doc in docs:
        channels[str(doc.get("channel"))] = channels.get(str(doc.get("channel")), 0) + 1
        events = doc.get("events") or []
        models = doc.get("models") or {}
        stt_label = models.get("stt") or "unrecorded (pre-Phase 3 document)"
        stt_models[stt_label] = stt_models.get(stt_label, 0) + 1
        usage = doc.get("usage") or {}
        reply_in += int(usage.get("llm_input_tokens") or 0)
        reply_cached += int(usage.get("llm_cached_tokens") or 0)
        # Older documents carry no per-turn TTS model: fall back to the engine
        # that spoke the most characters on the call.
        call_tts = None
        if doc.get("tts_usage"):
            top = max(doc["tts_usage"], key=lambda e: e.get("characters") or 0)
            call_tts = f"{top.get('provider')}/{top.get('model')}"
        first_attempt: str | None = None
        decided = False
        disabled = False
        attempted = False
        bucket: list[dict] = []
        for ev in events:
            kind = ev.get("kind")
            if kind != "turn_timing":
                bucket.append(ev)
            if kind == "turn_timing":
                turn_events, bucket = bucket, []
                ms = ev.get("spans_ms") or {}
                path = _turn_path(turn_events)
                if (ev.get("turn_id") or 0) >= 1 and path is not None:
                    decision_turns += 1
                    if isinstance(ms.get("response"), (int, float)):
                        path_response[path].append(float(ms["response"]))
                for e in turn_events:
                    if e.get("kind") == "decision_skipped_deterministic":
                        key = f"{e.get('resolver')}/{e.get('ask_kind')}"
                        skipped_by[key] = skipped_by.get(key, 0) + 1
                if doc.get("bot_id") == mdnd_bot and (ev.get("turn_id") or 0) >= 1:
                    dispatched = ev.get("turn_dispatched_at") or 0
                    classified = ev.get("classify_completed_at") or 0
                    if dispatched and classified:
                        mdnd_stages["decision"].append(classified - dispatched)
                    for e in turn_events:
                        if e.get("kind") == "workflow_timing" and isinstance(e.get("ms"), (int, float)):
                            mdnd_stages["workflow after decision"].append(float(e["ms"]))
                        if e.get("kind") in ("workflow_reply_grounded", "workflow_grounded_fallback") \
                                and isinstance(e.get("first_ms"), (int, float)):
                            mdnd_stages["rewrite first sentence"].append(float(e["first_ms"]))
                    if ev.get("tts_started_at") and ev.get("first_audio_sent_at"):
                        mdnd_stages["tts first audio"].append(
                            ev["first_audio_sent_at"] - ev["tts_started_at"])
                    if isinstance(ms.get("response"), (int, float)):
                        mdnd_stages["response"].append(float(ms["response"]))
            elif kind == "speculative_reply_discarded":
                reason = str(ev.get("reason") or "unknown")
                discard_reasons[reason] = discard_reasons.get(reason, 0) + 1
                discard_chars += int(ev.get("generated_chars") or 0)
            elif kind == "speculative_reply_kept":
                if isinstance(ev.get("lead_ms"), (int, float)):
                    kept_lead.append(float(ev["lead_ms"]))
            elif kind == "extraction_ahead":
                outcome = str(ev.get("outcome"))
                ahead_outcomes[outcome] = ahead_outcomes.get(outcome, 0) + 1
            elif kind == "early_ack_played":
                early_acks += 1
                early_acks_slot += bool(ev.get("early"))
            elif kind == "early_ack_not_ready":
                early_ack_not_ready += 1
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
                if isinstance(ms.get("tts_ttfb"), (int, float)):
                    label = ev.get("tts_model") or call_tts or "unknown"
                    ttfb_by_model.setdefault(label, []).append(float(ms["tts_ttfb"]))
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
                engine = (f"{ev.get('model') or 'unrecorded'} @ "
                          f"{ev.get('service_tier') or ev.get('service_tier_requested') or 'default'}")
                if isinstance(ev.get("latency_ms"), (int, float)):
                    decision_ms.setdefault(engine, []).append(float(ev["latency_ms"]))
                if isinstance(ev.get("output_tokens"), (int, float)):
                    decision_out_tokens.append(float(ev["output_tokens"]))
                    out_tokens_by_schema["with reason" if ev.get("reason") else "without reason"].append(
                        float(ev["output_tokens"]))
                    decision_in += int(ev.get("input_tokens") or 0)
                    decision_cached += int(ev.get("cached_tokens") or 0)
                decisions_total += 1
                decisions_with_text += bool(ev.get("has_response_text"))
            elif kind == "orchestration_fallback":
                reason = str(ev.get("reason") or "")
                if reason in ("engine_disabled", "empty_turn"):
                    continue
                attempted = True
                if reason in TIMEOUT_REASONS:
                    engine = (f"{ev.get('model') or 'unrecorded'} @ "
                              f"{ev.get('service_tier_requested') or 'default'}")
                    decision_timeouts[engine] = decision_timeouts.get(engine, 0) + 1
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
            elif kind == "tts_cold_connect":
                cold_connects.setdefault(str(ev.get("reason") or "unknown"), []).append(
                    float(ev.get("waited_ms") or 0))
            elif kind == "filler_only_dropped":
                filler_only_dropped += 1
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
        "decision_ms": decision_ms, "decision_timeouts": decision_timeouts,
        "decision_out_tokens": decision_out_tokens,
        "decision_in": decision_in, "decision_cached": decision_cached,
        "decisions_total": decisions_total, "decisions_with_text": decisions_with_text,
        "reply_in": reply_in, "reply_cached": reply_cached,
        "ttfb_by_model": ttfb_by_model, "stt_models": stt_models,
        "cold_connects": cold_connects, "filler_only_dropped": filler_only_dropped,
        "path_response": path_response, "skipped_by": skipped_by, "decision_turns": decision_turns,
        "discard_reasons": discard_reasons, "discard_chars": discard_chars, "kept_lead": kept_lead,
        "ahead_outcomes": ahead_outcomes, "early_acks": early_acks, "early_acks_slot": early_acks_slot,
        "early_ack_not_ready": early_ack_not_ready, "out_tokens_by_schema": out_tokens_by_schema,
        "mdnd_stages": mdnd_stages,
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
    print("Phase 3 — models")
    print(f"{'decision latency (ms) by model @ tier':<44}{'p50':>9}{'p90':>9}{'n':>8}{'timeouts':>10}")
    engines = sorted(set(r["decision_ms"]) | set(r["decision_timeouts"]),
                     key=lambda k: -len(r["decision_ms"].get(k, [])))
    for engine in engines:
        values = sorted(r["decision_ms"].get(engine, []))
        print(f"  {engine:<42}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}"
              f"{len(values):>8}{r['decision_timeouts'].get(engine, 0):>10}")
    out = sorted(r["decision_out_tokens"])
    print(f"{'decision output tokens p50 / p90':<44}{_fmt(_pct(out, 0.5)):>9}{_fmt(_pct(out, 0.9)):>9}{len(out):>8}")
    print(f"{'decisions that wrote response_text':<44}{_share(r['decisions_with_text'], r['decisions_total'])}")
    print(f"{'decision prompt tokens served from cache':<44}{_share(r['decision_cached'], r['decision_in'])}")
    print(f"{'reply prompt tokens served from cache':<44}{_share(r['reply_cached'], r['reply_in'])}")
    print(f"{'tts_ttfb (ms) by TTS model':<44}{'p50':>9}{'p90':>9}{'n':>8}")
    for label in sorted(r["ttfb_by_model"], key=lambda k: -len(r["ttfb_by_model"][k])):
        values = sorted(r["ttfb_by_model"][label])
        print(f"  {label:<42}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
    print(f"{'calls by STT model':<44}" + ", ".join(
        f"{k}={v}" for k, v in sorted(r["stt_models"].items(), key=lambda kv: -kv[1])))
    total_cold = sum(len(v) for v in r["cold_connects"].values())
    print(f"{'tts_cold_connect events (waited ms p50)':<44}{total_cold}  " + ", ".join(
        f"{k}={len(v)} ({_fmt(_pct(sorted(v), 0.5))} ms)" for k, v in sorted(r["cold_connects"].items())))
    print(f"{'filler_only_dropped events':<44}{r['filler_only_dropped']}")
    print()
    print("Phase 4 — decision off the critical path")
    print(f"{'response (ms) by path':<44}{'p50':>9}{'p90':>9}{'n':>8}")
    for name in PATHS:
        values = sorted(r["path_response"][name])
        print(f"  {name:<42}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
    skipped = sum(r["skipped_by"].values())
    print(f"{'decision_skipped_deterministic share':<44}{_share(skipped, r['decision_turns'])}  "
          + ", ".join(f"{k}={v}" for k, v in sorted(r["skipped_by"].items(), key=lambda kv: -kv[1])))
    for schema, values in r["out_tokens_by_schema"].items():
        values = sorted(values)
        print(f"{'decision output tokens ' + schema:<44}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
    kept = len(r["kept_lead"])
    discarded = sum(r["discard_reasons"].values())
    print(f"{'speculative reply kept':<44}{_share(kept, kept + discarded)}  lead p50 "
          f"{_fmt(_pct(sorted(r['kept_lead']), 0.5))} ms")
    print(f"{'speculative reply discarded (reasons)':<44}{discarded}  " + ", ".join(
        f"{k}={v}" for k, v in sorted(r["discard_reasons"].items(), key=lambda kv: -kv[1]))
          + f"  ~{r['discard_chars'] // 4} output tokens")
    print(f"{'extraction_ahead outcomes':<44}" + (", ".join(
        f"{k}={v}" for k, v in sorted(r["ahead_outcomes"].items())) or "-"))
    print(f"{'early_ack_played (in the early_ack_ms slot)':<44}{r['early_acks']} ({r['early_acks_slot']})"
          f"   early_ack_not_ready={r['early_ack_not_ready']}")
    print(f"{'MDND stage split (ms)':<44}{'p50':>9}{'p90':>9}{'n':>8}")
    for name, values in r["mdnd_stages"].items():
        values = sorted(values)
        print(f"  {name:<42}{_fmt(_pct(values, 0.5)):>9}{_fmt(_pct(values, 0.9)):>9}{len(values):>8}")
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
    parser.add_argument("--bot", default=None, help="only this bot id")
    parser.add_argument("--mdnd-bot", default="bot_59a84478f155",
                        help="bot whose turns make up the MDND stage split")
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
    report = asyncio.run(collect(uri, database, args.days, args.channel, since=since,
                                 bot=args.bot, mdnd_bot=args.mdnd_bot))
    print_report(report, args.channel)
    print(f"notes: 1st-turn timeout counts reasons {TIMEOUT_REASONS}; the first orchestration event of a call is its first turn's attempt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
