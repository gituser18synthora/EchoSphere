import json
import logging
 
import redis as redis_sync
import redis.asyncio as aioredis
 
from config.settings import Settings
 
logger = logging.getLogger(__name__)
 
SESSION_PREFIX = "session"
 
 
class RedisSession:
    def __init__(self):
        settings = Settings()
        url = (settings.redis_url or "").strip() or "redis://localhost:6379"
        self._url = url
        self._async_redis = aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
        )
 
    def _key(
        self,
        tenant_id: str,
        voicebot_id: str,
        call_id: str,
    ) -> str:
        return f"{SESSION_PREFIX}:{tenant_id}:{voicebot_id}:{call_id}"
 
    def _estimate_tokens(self, text: str) -> int:
        return int(len(str(text).split()) * 1.3)
 
    def _build_session_dict(self, call_state) -> tuple[dict, list]:
        """Build serializable session dict from call_state. Returns (session, turns_data)."""
        turns_data = [
            {
                "turn_id": turn.turn_id,
                "role": turn.role,
                "content": turn.content,
                "intent": turn.intent,
                "confidence": turn.confidence,
                "token_count": self._estimate_tokens(turn.content),
                "timestamp": turn.timestamp.isoformat(),
            }
            for turn in call_state.turns
        ]
        total_token_count = sum(td["token_count"] for td in turns_data)
 
        active_goal = None
        if call_state.active_goal:
            g = call_state.active_goal
            active_goal = {
                "goal_name": g.goal_name,
                "slots": g.slots,
                "started_at_turn": g.started_at_turn,
                "paused": g.paused,
                "pause_reason": g.pause_reason,
            }
 
        session = {
            "voicebot_id": call_state.voicebot_id,
            "call_id": call_state.call_id,
            "tenant_id": call_state.tenant_id,
            "caller_phone": call_state.caller_phone,
            "started_at": call_state.call_start_time.isoformat(),
            "detected_language": call_state.detected_language,
            "sentiment_trend": call_state.sentiment_trend,
            "turn_count": call_state.turn_count,
            "turns": turns_data,
            "total_token_count": total_token_count,
            "running_summary": call_state.running_summary,
            "running_summary_turn": call_state.running_summary_turn,
            "privacy_deletion_requested": call_state.privacy_deletion_requested,
            "system_prompt": getattr(call_state, "system_prompt", "") or "",
            "active_goal": active_goal,
        }
        return session, turns_data
 
    async def _write(
        self,
        call_state,
        max_call_duration_minutes: int,
        label: str,
    ) -> None:
        """Single write path used by both create() and save()."""
        ttl = max_call_duration_minutes * 2 * 60
        session, turns_data = self._build_session_dict(call_state)
        key = self._key(
            call_state.tenant_id,
            call_state.voicebot_id,
            call_state.call_id,
        )
        await self._async_redis.setex(
            key,
            ttl,
            json.dumps(session, default=str),
        )
        logger.info(
            "[Redis] Session %s | call_id=%s | turns=%s | turn_count=%s",
            label,
            call_state.call_id,
            len(turns_data),
            call_state.turn_count,
        )
 
    async def create(
        self,
        call_state,
        max_call_duration_minutes: int,
    ) -> None:
        await self._write(call_state, max_call_duration_minutes, label="CREATED")
 
    async def save(
        self,
        call_state,
        max_call_duration_minutes: int,
    ) -> None:
        await self._write(call_state, max_call_duration_minutes, label="SAVED")
 
    async def get_full_session(
        self,
        tenant_id: str,
        voicebot_id: str,
        call_id: str,
    ) -> dict | None:
        key = self._key(tenant_id, voicebot_id, call_id)
        raw = await self._async_redis.get(key)
        if raw is None:
            logger.warning(
                "[Redis] Session NOT FOUND | call_id=%s",
                call_id,
            )
            return None
        session = json.loads(raw)
        logger.debug(
            "[Redis] Session READ | call_id=%s | turns=%s",
            call_id,
            len(session.get("turns", [])),
        )
        return session
 
    async def get(
        self,
        tenant_id: str,
        voicebot_id: str,
        call_id: str,
    ) -> dict | None:
        """Alias for tests and legacy callers."""
        return await self.get_full_session(tenant_id, voicebot_id, call_id)
 
    async def delete(
        self,
        tenant_id: str,
        voicebot_id: str,
        call_id: str,
    ) -> None:
        key = self._key(tenant_id, voicebot_id, call_id)
        deleted = await self._async_redis.delete(key)
        if deleted:
            logger.info("[Redis] Session DELETED | call_id=%s", call_id)
        else:
            logger.warning(
                "[Redis] Session delete — key not found | call_id=%s",
                call_id,
            )
 
    @staticmethod
    def clear_all_sessions_sync(redis_url: str) -> int:
        r = redis_sync.from_url(
            (redis_url or "").strip() or "redis://localhost:6379",
            decode_responses=True,
        )
        keys = r.keys(f"{SESSION_PREFIX}:*")
        if keys:
            r.delete(*keys)
            logger.info("[Redis] Cleared %s stale sessions", len(keys))
            return len(keys)
        return 0