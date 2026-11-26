"""READ-ONLY fetch of finished test sessions (transcript, key events, final
workflow checkpoint, post-call memory) for the Haier QA harness.

args: <vs_id> [<vs_id> ...]     prints JSON between ===JSON-BEGIN/END===
Local: env/bin/python -B haier/qa/fetch_sessions.py vs_a vs_b
Demo:  ssh echosphere-live 'cd /var/www/html/EchoSphere && env/bin/python - vs_a' < haier/qa/fetch_sessions.py
Never writes: Mongo find, MySQL SELECT, checkpoint alist (no saver.setup()).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.getcwd())

from pymongo import MongoClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from shared.config import get_settings  # noqa: E402

WF = "wf_1c3e59362c51"
KEEP = {
    "runtime_context_loaded", "call_started", "orchestration_decision", "workflow_reply_heard",
    "workflow_off_script", "context_question_reply", "barge_in_confirmed", "stale_held_short_dropped",
    "silence_prompt", "call_completed_by_workflow", "workflow_closing_started", "workflow_closing_cancelled",
    "workflow_closing_followup", "client_disconnected", "identity_reask", "identity_question_rephrased",
    "decision_skipped_deterministic", "workflow_turn_rolled_back", "turn_merged_late_final",
    "unheard_reply_superseded", "unheard_reply_not_answered", "unheard_turn_merged_uncommitted",
    "language_switch_blocked", "language_detected", "stt_repeat_requested", "generation_cancelled",
    "workflow_reply_interrupted", "call_control", "orchestration_fallback", "stt_held_segment_discarded",
    "stt_held_segment_merged", "workflow_timing", "opening_greeting_interrupted", "opening_greeting_recovered",
    "pickup_hello_answered", "opening_greeting_heard", "deterministic_fast_path", "identity_rule",
    "workflow_identity_accepted", "background_suspect_discarded", "turn_latency", "hold_acknowledged",
    "workflow_answer_needs_clarification", "post_hangup_transcript_dropped", "end_call_playout_wait",
    # closing, greeting and short-answer handling (2026-11-25 review, phase 1)
    "workflow_closing_acknowledgement", "workflow_farewell_replayed", "workflow_closing_reaffirmed",
    "workflow_reopened_after_closing", "workflow_outcome_contested", "answer_option_reread",
    "stt_segment_held_during_bot_audio", "stale_held_short_recovered", "background_suspect_not_dispatched",
    "filler_only_dropped", "stt_segment_rejected", "stt_segment_appended", "pending_question_resumed",
    "early_answer_deferred", "early_answer_reasked", "short_segment_retranscribed",
    "short_segment_retranscribe_failed", "unsupported_language_retranscribe_failed", "backchannel_suppressed",
}


async def checkpoints(ids):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    st = get_settings()
    conninfo = (f"postgresql://{quote(str(st.postgres_user), safe='')}:{quote(str(st.postgres_password), safe='')}"
                f"@{st.postgres_host}:{st.postgres_port}/{st.postgres_database}")
    out = {}
    try:
        conn = await AsyncConnection.connect(conninfo, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        return {"_error": repr(exc)}
    saver = AsyncPostgresSaver(conn)
    for sid in ids:
        try:
            async for item in saver.alist({"configurable": {"thread_id": f"{sid}:{WF}"}}, limit=1):
                cv = item.checkpoint.get("channel_values", {})
                out[sid] = {k: cv.get(k) for k in ("current", "status", "trace", "slots", "awaiting")}
                break
        except Exception as exc:  # noqa: BLE001
            out[sid] = {"_error": repr(exc)}
    await conn.close()
    return out


def main() -> None:
    ids = sys.argv[1:]
    st = get_settings()
    db = MongoClient(st.mongodb_uri, serverSelectionTimeoutMS=8000)[st.mongodb_database]
    docs = {d["session_id"]: d for d in db.conversation_transcripts.find({"session_id": {"$in": ids}})}
    cks = asyncio.run(checkpoints(ids))
    eng = create_engine(st.mysql_url)
    mem = {}
    cvs = [d.get("control_plane_id") for d in docs.values() if d.get("control_plane_id")]
    if cvs:
        with eng.connect() as conn:
            names = ",".join(f":c{i}" for i in range(len(cvs)))
            rows = conn.execute(text(f"SELECT conversation_id, status, final_state, memory, call_outcome FROM conversation_memories WHERE conversation_id IN ({names})"),
                                {f"c{i}": v for i, v in enumerate(cvs)}).fetchall()
            for r in rows:
                fs = json.loads(r[2]) if isinstance(r[2], str) else (r[2] or {})
                m = json.loads(r[3]) if isinstance(r[3], str) else (r[3] or {})
                sf = (m or {}).get("structured_fields") or {}
                mem[r[0]] = {"status": r[1], "workflow_slots": (fs or {}).get("workflow_slots"),
                             "disposition": sf.get("disposition"), "call_outcome": r[4],
                             "summary": (m or {}).get("summary")}
    out = {}
    for sid in ids:
        d = docs.get(sid)
        if not d:
            out[sid] = {"missing": True, "checkpoint": cks.get(sid) if isinstance(cks, dict) else None}
            continue
        out[sid] = {
            "cv": d.get("control_plane_id"), "started_at": str(d.get("started_at")), "duration_sec": d.get("duration_sec"),
            "end_reason": d.get("end_reason"), "bot_version": d.get("bot_version"), "prompt_version": d.get("prompt_version"),
            "turns": d.get("turns") or [],
            "events": [{k: (str(v) if k == "at" else v) for k, v in e.items() if k != "naturalness"}
                       for e in (d.get("events") or []) if e.get("kind") in KEEP],
            "checkpoint": cks.get(sid) if isinstance(cks, dict) else None,
            "memory": mem.get(d.get("control_plane_id")),
        }
    print("===JSON-BEGIN===")
    print(json.dumps(out, ensure_ascii=False, default=str))
    print("===JSON-END===")


if __name__ == "__main__":
    main()
