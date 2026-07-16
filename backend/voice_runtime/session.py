"""Voice session state and persistence.

- Redis: live per-call state (tenant-scoped keys), created by the API when a
  session is issued and consumed by the voice worker as the *trusted* mapping
  from session token → tenant/bot context.
- MongoDB: transcript (`conversation_transcripts`) and voice events
  (`voice_events`) — written asynchronously, never in the audio critical path.
- MySQL: a `conversation_sessions` row is created at call end (summary/usage).
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.config import get_settings
from backend.db.mongo import Mongo
from backend.db.redis import get_redis
from backend.knowledge.security import mask_pii
from backend.voice_runtime.bot_config import ResolvedBotConfig

logger = logging.getLogger(__name__)

_SESSION_PREFIX = "voice:session:"


def _session_key(session_id: str) -> str:
    return f"{_SESSION_PREFIX}{session_id}"


@dataclass
class TurnRecord:
    role: str  # user | bot
    text: str
    timestamp: float = field(default_factory=time.time)
    route: str | None = None
    kb_used: bool = False
    kb_sources: list[dict] = field(default_factory=list)
    latency_ms: dict = field(default_factory=dict)


async def create_voice_session(
    *,
    tenant_id: str,
    bot_id: str,
    user_id: str | None,
    channel: str = "browser",
    caller: str | None = None,
) -> dict:
    """Issue a session token (called from the authenticated API process)."""
    settings = get_settings()
    session_id = f"vs_{secrets.token_urlsafe(18)}"
    payload = {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "bot_id": bot_id,
        "user_id": user_id,
        "channel": channel,
        "caller": caller,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "issued",
    }
    await get_redis().set(
        _session_key(session_id), json.dumps(payload), ex=settings.voice_session_timeout
    )
    return payload


async def load_voice_session(session_id: str) -> dict | None:
    """Trusted lookup used by the voice worker; returns None if expired/unknown."""
    raw = await get_redis().get(_session_key(session_id))
    return json.loads(raw) if raw else None


async def update_voice_session(session_id: str, **fields) -> None:
    redis = get_redis()
    raw = await redis.get(_session_key(session_id))
    if not raw:
        return
    payload = json.loads(raw)
    payload.update(fields)
    ttl = await redis.ttl(_session_key(session_id))
    await redis.set(_session_key(session_id), json.dumps(payload), ex=max(ttl, 60))


async def end_voice_session(session_id: str) -> None:
    await get_redis().delete(_session_key(session_id))


class SessionRecorder:
    """Accumulates turns/events/usage for one call and persists them."""

    def __init__(self, session_id: str, config: ResolvedBotConfig, channel: str = "browser",
                 caller: str | None = None) -> None:
        self.session_id = session_id
        self.config = config
        self.channel = channel
        self.caller = caller
        self.started_at = time.time()
        self.turns: list[TurnRecord] = []
        self.events: list[dict] = []
        self.usage: dict[str, float] = {
            "stt_seconds": 0.0, "llm_input_tokens": 0, "llm_output_tokens": 0,
            "tts_characters": 0, "kb_searches": 0,
        }
        self.end_reason: str | None = None

    def add_turn(self, turn: TurnRecord) -> None:
        self.turns.append(turn)

    def add_event(self, kind: str, **data) -> None:
        self.events.append({
            "kind": kind,
            "at": datetime.now(timezone.utc).isoformat(),
            **data,
        })

    async def flush_event(self, kind: str, **data) -> None:
        """Persist a single event immediately (barge-in, handoff, errors)."""
        self.add_event(kind, **data)
        try:
            await Mongo.voice_events().insert_one({
                "session_id": self.session_id,
                "tenant_id": self.config.tenant_id,
                "bot_id": self.config.bot_id,
                "kind": kind,
                "at": datetime.now(timezone.utc),
                "data": data,
            })
        except Exception:  # noqa: BLE001 - persistence must not break the call
            logger.warning("voice event write failed (%s)", kind)

    async def finalize(self, reason: str = "completed") -> None:
        """Persist transcript + session summary. Called once at call end."""
        self.end_reason = reason
        duration = int(time.time() - self.started_at)
        transcript = [
            {
                "role": t.role,
                "text": mask_pii(t.text, kinds={"card_number", "aadhaar", "pan"}),
                "ts": t.timestamp,
                "route": t.route,
                "kbUsed": t.kb_used,
                "kbSources": t.kb_sources,
                "latencyMs": t.latency_ms,
            }
            for t in self.turns
        ]
        try:
            await Mongo.transcripts().update_one(
                {"session_id": self.session_id},
                {
                    "$set": {
                        "session_id": self.session_id,
                        "tenant_id": self.config.tenant_id,
                        "bot_id": self.config.bot_id,
                        "channel": self.channel,
                        "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc),
                        "duration_sec": duration,
                        "end_reason": reason,
                        "turns": transcript,
                        "events": self.events,
                        "usage": self.usage,
                        "bot_version": self.config.version,
                    }
                },
                upsert=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("transcript persistence failed for %s", self.session_id)

        await asyncio.to_thread(self._write_control_plane_row, duration, reason)

    def _write_control_plane_row(self, duration: int, reason: str) -> None:
        from backend.core.ids import new_id
        from backend.db.mysql import get_sessionmaker
        from backend.models import ConversationSession

        session = get_sessionmaker()()
        try:
            existing = session.get(ConversationSession, self.session_id)
            if existing is None:
                session.add(
                    ConversationSession(
                        id=new_id("cv"),
                        tenant_id=self.config.tenant_id,
                        bot_id=self.config.bot_id,
                        channel="voice" if self.channel != "browser" else "web",
                        caller_masked=mask_pii(self.caller or "", kinds={"phone"}) or None,
                        started_at=datetime.fromtimestamp(self.started_at, tz=timezone.utc),
                        duration_sec=duration,
                        contained=not any(e.get("kind") == "handoff" for e in self.events),
                        escalation_reason=(
                            "human_handoff"
                            if any(e.get("kind") == "handoff" for e in self.events)
                            else None
                        ),
                        language=self.config.language,
                        status="completed",
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("conversation_sessions row write failed")
            session.rollback()
        finally:
            session.close()
