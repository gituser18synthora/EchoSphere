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
            "stt_seconds": 0.0, "stt_requests": 0,
            "llm_input_tokens": 0, "llm_output_tokens": 0,
            "llm_cached_tokens": 0, "llm_requests": 0, "llm_usage_estimated": 0,
            "tts_characters": 0, "kb_searches": 0,
        }
        # Per-engine TTS breakdown ("provider|model|voice" → counters) so a
        # mid-call fallback bills each provider for what it actually spoke.
        self.tts_usage: dict[str, dict] = {}
        self.end_reason: str | None = None
        # Conversation language, live: starts at the bot default and follows
        # the caller (the brain updates it on every detected switch).
        self.language: str = config.language

    def add_turn(self, turn: TurnRecord) -> None:
        self.turns.append(turn)

    def add_tts_usage(self, *, provider: str, model: str, voice: str, characters: int) -> None:
        """Called by the TTS router once per completed generation."""
        self.usage["tts_characters"] += characters
        key = f"{provider}|{model}|{voice}"
        entry = self.tts_usage.setdefault(
            key, {"provider": provider, "model": model, "voice": voice,
                  "characters": 0, "requests": 0}
        )
        entry["characters"] += characters
        entry["requests"] += 1

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
                        "tts_usage": list(self.tts_usage.values()),
                        "bot_version": self.config.version,
                        "language": self.language,
                    }
                },
                upsert=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("transcript persistence failed for %s", self.session_id)

        await asyncio.to_thread(self._write_control_plane_row, duration, reason)

    def _write_control_plane_row(self, duration: int, reason: str) -> None:
        from decimal import Decimal

        from shared.billing.metering import record_usage_event, rollup_call
        from shared.db.mysql import get_sessionmaker
        from shared.ids import new_id
        from shared.models import ConversationSession

        session = get_sessionmaker()()
        try:
            existing = session.get(ConversationSession, self.session_id)
            if existing is not None:
                return  # finalize already persisted this call — never re-bill
            escalated = any(e.get("kind") == "handoff" for e in self.events)
            row = ConversationSession(
                id=new_id("cv"),
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                channel="voice" if self.channel != "browser" else "web",
                caller_masked=mask_pii(self.caller or "", kinds={"phone"}) or None,
                started_at=datetime.fromtimestamp(self.started_at, tz=timezone.utc),
                duration_sec=duration,
                contained=not escalated,
                escalation_reason="human_handoff" if escalated else None,
                language=self.language,
                status="completed",
            )
            session.add(row)

            ai_cost = self._record_usage_events(
                session, record_usage_event, duration
            )
            row.cost_usd = ai_cost

            occurred = datetime.fromtimestamp(self.started_at, tz=timezone.utc)
            rollup_call(
                session,
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                day=occurred.date(),
                contained=not escalated,
                escalated=escalated,
                minutes=Decimal(duration) / 60,
            )
            session.commit()
        except Exception:  # noqa: BLE001
            logger.exception("conversation_sessions/usage write failed")
            session.rollback()
        finally:
            session.close()

    def _record_usage_events(self, session, record_usage_event, duration: int):
        """One usage event per capability/engine for this call (idempotent).

        Streaming chunks and turns were already aggregated in ``self.usage``;
        the deterministic request ids make a re-run of finalize a no-op.
        """
        from decimal import Decimal

        occurred = datetime.fromtimestamp(self.started_at, tz=timezone.utc).replace(tzinfo=None)
        total = Decimal(0)

        def _record(**kwargs):
            nonlocal total
            # Mock providers make no external call — nothing billable happened.
            if (kwargs.get("provider_code") or "") == "mock":
                return None
            event = record_usage_event(
                session,
                tenant_id=self.config.tenant_id,
                bot_id=self.config.bot_id,
                session_id=self.session_id,
                occurred_at=occurred,
                commit=False,
                **kwargs,
            )
            return event

        usage = self.usage
        llm_conf = self.config.llm or {}
        if usage.get("llm_requests") or usage.get("llm_output_tokens"):
            event = _record(
                capability="llm",
                provider_code=llm_conf.get("provider") or "openai",
                model_code=llm_conf.get("model") or "",
                request_id=f"{self.session_id}:llm",
                requests=int(usage.get("llm_requests") or 1),
                input_tokens=int(usage.get("llm_input_tokens") or 0),
                output_tokens=int(usage.get("llm_output_tokens") or 0),
                cached_tokens=int(usage.get("llm_cached_tokens") or 0),
                usage_source="estimated" if usage.get("llm_usage_estimated") else "provider",
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        stt_conf = self.config.stt or {}
        stt_seconds = float(usage.get("stt_seconds") or 0)
        stt_source = "provider"
        if stt_seconds <= 0 and any(t.role == "user" for t in self.turns):
            # Realtime WS STT streams audio for the whole call; the connection
            # duration is the documented billing estimate when the provider
            # reports no per-utterance duration.
            stt_seconds = float(duration)
            stt_source = "estimated"
        if stt_seconds > 0:
            event = _record(
                capability="stt",
                provider_code=stt_conf.get("provider") or "sarvam",
                model_code=stt_conf.get("model") or "",
                request_id=f"{self.session_id}:stt",
                requests=int(usage.get("stt_requests") or 1),
                audio_seconds=round(stt_seconds, 3),
                usage_source=stt_source,
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        for entry in self.tts_usage.values():
            key = f"{entry['provider']}|{entry['model']}|{entry['voice']}"
            event = _record(
                capability="tts",
                provider_code=entry["provider"] or "sarvam",
                model_code=entry["model"] or "",
                voice_code=entry["voice"] or None,
                request_id=f"{self.session_id}:tts:{key}"[:120],
                requests=int(entry["requests"] or 1),
                characters=int(entry["characters"] or 0),
            )
            if event is not None:
                total += Decimal(str(event.cost_usd))

        if self.channel not in ("browser", "web") and duration > 0:
            # Telephony minutes are tracked/priced separately from AI usage.
            _record(
                capability="telephony",
                provider_code=self.channel,
                model_code="",
                request_id=f"{self.session_id}:telephony",
                audio_seconds=duration,
                usage_metadata={"direction": "inbound"},
            )

        return total
