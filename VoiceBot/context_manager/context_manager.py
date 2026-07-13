# voicebot.context_manager/voicebot.context_manager.py

"""
ContextManager: Redis is source of truth for conversation history.
call_state.turns mirrors Redis; LLM messages are built from Redis each turn.
"""

import asyncio
import logging

from voicebot.config_layer.models import VoicebotConfig
from voicebot.context_manager.graph import CallerGraph
from voicebot.context_manager.session import RedisSession
from voicebot.context_manager.token_budget import build_llm_messages_from_redis
from orchestrator.call_state import CallState

logger = logging.getLogger(__name__)


class ContextManager:
    def __init__(self, config: VoicebotConfig):
        self._config = config
        self._session = RedisSession()
        self._graph = CallerGraph()

    async def on_call_start(self, call_state: CallState) -> dict | None:
        caller_graph = None
        if self._config.engine.context_recall_between_calls:
            try:
                caller_graph = await self._graph.load(
                    voicebot_id=call_state.voicebot_id,
                    caller_phone=call_state.caller_phone,
                    memory_expiry_days=(
                        self._config.engine.memory_expiry_days
                    ),
                )
            except Exception as e:
                logger.error(
                    "[ContextManager] Graph load failed: %s",
                    e,
                )

        await self._session.create(
            call_state=call_state,
            max_call_duration_minutes=(
                self._config.escalation.max_call_duration
            ),
        )

        logger.info(
            "[ContextManager] Call started | caller=%s | returning_caller=%s",
            call_state.caller_phone,
            "yes" if caller_graph else "no (first time)",
        )
        return caller_graph

    async def sync_session(self, call_state: CallState) -> None:
        await self._session.save(
            call_state=call_state,
            max_call_duration_minutes=(
                self._config.escalation.max_call_duration
            ),
        )

    async def build_llm_messages(
        self,
        call_state: CallState,
        current_text: str,
        knowledge_content: str | None,
    ) -> list[dict]:
        session = await self._session.get_full_session(
            tenant_id=call_state.tenant_id,
            voicebot_id=call_state.voicebot_id,
            call_id=call_state.call_id,
        )

        if session is None:
            logger.error(
                "[ContextManager] Redis session not found — "
                "falling back to call_state.turns",
            )
            redis_turns = [
                {"role": t.role, "content": t.content}
                for t in call_state.turns
            ]
        else:
            redis_turns = session.get("turns", [])
            logger.info(
                "[ContextManager] Read %s turns from Redis for LLM context",
                len(redis_turns),
            )

        return build_llm_messages_from_redis(
            system_prompt=call_state.system_prompt,
            redis_turns=redis_turns,
            current_text=current_text,
            knowledge_content=knowledge_content,
            running_summary=call_state.running_summary,
            context_window_tokens=(
                self._config.conversation_intelligence
                .context_window_tokens
            ),
        )

    async def persist_turn(
        self,
        call_state: CallState,
        user_text: str,
        bot_text: str,
        intent: str,
        confidence: float,
    ) -> None:
        try:
            await self._session.save(
                call_state=call_state,
                max_call_duration_minutes=(
                    self._config.escalation.max_call_duration
                ),
            )
        except Exception as e:
            logger.error(
                "[ContextManager] Redis save failed: %s",
                e,
            )

    async def get_full_transcript(self, call_state: CallState) -> str:
        session = await self._session.get_full_session(
            tenant_id=call_state.tenant_id,
            voicebot_id=call_state.voicebot_id,
            call_id=call_state.call_id,
        )

        if session is None:
            logger.warning(
                "[ContextManager] No Redis session for transcript — "
                "using call_state.turns",
            )
            return call_state.transcript_as_dialogue()

        turns = session.get("turns", [])
        if not turns:
            return call_state.transcript_as_dialogue()

        lines = []
        for t in turns:
            role = t.get("role", "user")
            content = t.get("content", "")
            prefix = "Caller" if role == "user" else "Bot"
            lines.append(f"{prefix}: {content}")

        transcript = "\n".join(lines)
        logger.info(
            "[ContextManager] Transcript from Redis | %s turns | %s chars",
            len(turns),
            len(transcript),
        )
        return transcript

    async def on_call_end(
        self,
        call_state: CallState,
        extraction: dict | None,
    ) -> None:
        # ── Step 1: MongoDB write ─────────────────────────────────────
        try:
            if call_state.privacy_deletion_requested:
                logger.info(
                    "[ContextManager] Privacy deletion | caller=%s",
                    call_state.caller_phone,
                )
                await self._graph.delete_caller(
                    voicebot_id=call_state.voicebot_id,
                    caller_phone=call_state.caller_phone,
                )
            else:
                # Always write at least a minimal record
                has_content = extraction and (
                    extraction.get("nodes")
                    or extraction.get("summary")
                    or extraction.get("caller_name")
                )
                write_extraction = extraction if has_content else {
                    "caller_name": None,
                    "caller_email": None,
                    "nodes": [],
                    "edges": [],
                    "summary": (
                        f"Call with {call_state.caller_phone} | "
                        f"{call_state.turn_count} turns"
                    ),
                }
                logger.info(
                    "[ContextManager] Writing to MongoDB | "
                    "nodes=%s | name=%s",
                    len(write_extraction.get("nodes", [])),
                    write_extraction.get("caller_name"),
                )
                await self._graph.write(
                    call_state=call_state,
                    extraction=write_extraction,
                    memory_expiry_days=(
                        self._config.engine.memory_expiry_days
                    ),
                )
                logger.info("[ContextManager] MongoDB write ✅")
        except Exception as e:
            logger.error(
                "[ContextManager] MongoDB write error: %s",
                e,
                exc_info=True,
            )

        # ── Step 2: Delete Redis session (always, even if Mongo failed) ──
        try:
            await asyncio.wait_for(
                self._session.delete(
                    tenant_id=call_state.tenant_id,
                    voicebot_id=call_state.voicebot_id,
                    call_id=call_state.call_id,
                ),
                timeout=3.0,
            )
        except Exception as e:
            logger.warning(
                "[ContextManager] Async Redis delete failed: %s — "
                "trying sync fallback",
                e,
            )
            try:
                import redis as _redis_sync
                from config.settings import Settings
                _settings = Settings()
                _r = _redis_sync.from_url(
                    (_settings.redis_url or "").strip()
                    or "redis://localhost:6379",
                    decode_responses=True,
                )
                _key = (
                    f"session:{call_state.tenant_id}"
                    f":{call_state.voicebot_id}"
                    f":{call_state.call_id}"
                )
                _r.delete(_key)
                logger.info(
                    "[ContextManager] Redis deleted via sync fallback ✅",
                )
            except Exception as se:
                logger.error(
                    "[ContextManager] Sync Redis delete also failed: %s",
                    se,
                )

    async def delete_caller_context(self, caller_phone: str) -> bool:
        return await self._graph.delete_caller(
            voicebot_id=self._config.voicebot_id,
            caller_phone=caller_phone,
        )

    async def delete_caller_node(
        self,
        caller_phone: str,
        node_id: str,
    ) -> None:
        await self._graph.delete_node(
            voicebot_id=self._config.voicebot_id,
            caller_phone=caller_phone,
            node_id=node_id,
        )

    async def delete_all_voicebot_context(self) -> int:
        return await self._graph.delete_all_for_voicebot(
            voicebot_id=self._config.voicebot_id,
        )
