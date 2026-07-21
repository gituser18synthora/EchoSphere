"""Per-call transcript/event/usage recording.

- MongoDB: transcript (`conversation_transcripts`) and voice events
  (`voice_events`) — written asynchronously, never in the audio critical path.
- MySQL: a `conversation_sessions` row is created at call end (summary/usage).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from shared.bot_config import ResolvedBotConfig
from shared.db.mongo import Mongo
from shared.knowledge.security import mask_pii

logger = logging.getLogger(__name__)


@dataclass
class TurnRecord:
    role: str  # user | bot
    text: str
    timestamp: float = field(default_factory=time.time)
    route: str | None = None
    kb_used: bool = False
    kb_sources: list[dict] = field(default_factory=list)
    latency_ms: dict = field(default_factory=dict)


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
        from shared.db.mysql import get_sessionmaker
        from shared.ids import new_id
        from shared.models import ConversationSession

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
