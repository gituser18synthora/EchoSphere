"""Conversation transcript lookup and canonical turn shape.

Two writers persist transcript documents in Mongo `conversation_transcripts`:

- the control-plane API (POST /conversations, demo seed): documents keyed by
  the conversation row id (``session_id == conversation_sessions.id``), turns
  already in the UI shape ``{turn, speaker, text, intent?, ...}``;
- the voice runtime (``SessionRecorder``): documents keyed by the runtime
  session id (``vs_*``), turns in the runtime shape
  ``{role, text, ts, route, kbUsed, kbSources, latencyMs}``. The runtime also
  stamps ``control_plane_id`` — the MySQL row id it creates at call end.

``find_transcript_doc`` resolves a conversation row to its document across
all generations (including legacy runtime docs written before
``control_plane_id`` existed, matched by tenant/bot/start-time). ``ui_turns``
converts either turn shape into the one the frontend and exporters consume.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from shared.config import get_settings
from shared.db.mongo import Mongo
from shared.models import ConversationSession

# Clock skew allowance between the runtime's started_at and the MySQL row's
# when matching legacy documents that carry no control_plane_id.
_LEGACY_MATCH_WINDOW_S = 10


async def find_transcript_doc(c: ConversationSession) -> dict | None:
    col = Mongo.transcripts()
    doc = await col.find_one({
        "tenant_id": c.tenant_id,
        "$or": [{"session_id": c.id}, {"control_plane_id": c.id}],
    })
    if doc is not None or c.started_at is None:
        return doc

    # Legacy runtime documents: keyed by a vs_* session id and written before
    # control_plane_id existed. Match on tenant/bot and closest start time.
    window = timedelta(seconds=_LEGACY_MATCH_WINDOW_S)
    best: dict | None = None
    best_gap = float("inf")
    cursor = col.find({
        "tenant_id": c.tenant_id,
        "bot_id": c.bot_id,
        "control_plane_id": {"$exists": False},
        "started_at": {"$gte": c.started_at - window, "$lte": c.started_at + window},
    })
    async for candidate in cursor:
        started = candidate.get("started_at")
        gap = abs((started - c.started_at).total_seconds()) if started else float("inf")
        # Same-second bursts: prefer the document whose duration also agrees.
        gap += abs(int(candidate.get("duration_sec") or 0) - int(c.duration_sec or 0)) * 0.001
        if gap < best_gap:
            best, best_gap = candidate, gap
    return best


def _runtime_turn(index: int, turn: dict) -> dict:
    out: dict = {
        "turn": index + 1,
        "speaker": "bot" if turn.get("role") == "bot" else "user",
        "text": str(turn.get("text") or ""),
    }
    ts = turn.get("ts")
    if isinstance(ts, (int, float)) and ts > 0:
        out["at"] = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    route = turn.get("route")
    if route:
        out["route"] = str(route)
    sources = turn.get("kbSources") or []
    if sources:
        out["chunksUsed"] = [
            f"{s.get('documentId') or s.get('kbId') or 'doc'}"
            + (f" · p{s.get('page')}" if s.get("page") is not None else "")
            for s in sources
            if isinstance(s, dict)
        ]
    latency = turn.get("latencyMs")
    if isinstance(latency, dict) and latency:
        total = latency.get("total")
        if not isinstance(total, (int, float)):
            nums = [v for v in latency.values() if isinstance(v, (int, float))]
            total = sum(nums) if nums else None
        if isinstance(total, (int, float)):
            out["latencyMs"] = int(round(total))
    elif isinstance(latency, (int, float)):
        out["latencyMs"] = int(round(latency))
    return out


def ui_turns(raw: list | None) -> list[dict]:
    """Map stored turns (either shape) to the UI shape, chronologically."""
    turns: list[dict] = []
    for index, turn in enumerate(raw or []):
        if not isinstance(turn, dict):
            continue
        if "speaker" in turn:  # already the UI/API shape
            out = dict(turn)
            out.setdefault("turn", index + 1)
            turns.append(out)
        else:
            turns.append(_runtime_turn(index, turn))
    turns.sort(key=lambda t: t.get("turn") or 0)
    return turns


# ── call recordings ──────────────────────────────────────────────────────────

def resolve_recording_path(relative_path: str | None) -> Path | None:
    """Resolve a stored recording reference under the recordings root.

    Returns None when the reference is absent, escapes the root (defense
    against a tampered document) or the file no longer exists.
    """
    if not relative_path:
        return None
    root = Path(get_settings().voice_recordings_dir).resolve()
    try:
        full = (root / relative_path).resolve()
    except (OSError, ValueError):
        return None
    if not full.is_relative_to(root) or not full.is_file():
        return None
    return full


def recording_descriptor(c: ConversationSession, doc: dict | None) -> dict | None:
    """Public shape for a conversation's recording, or None when unavailable."""
    info = (doc or {}).get("recording") or {}
    full = resolve_recording_path(info.get("path"))
    if full is None:
        return None
    return {
        "url": f"/api/v1/conversations/{c.id}/recording",
        "mimeType": info.get("mimeType") or "audio/wav",
        "durationSec": float(info.get("durationSec") or 0),
        "sizeBytes": full.stat().st_size,
    }
