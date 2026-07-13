import asyncio
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
import httpx
import redis as redis_sync
 
from voicebot.adapters.factory import ModelFactory
from voicebot.config.settings import Settings
from voicebot.config_layer.models import VoicebotConfig
from voicebot.context_manager.context_manager import ContextManager
from voicebot.goal_engine import GoalEngine, GoalResult
from voicebot.orchestrator.call_state import CallState
from voicebot.usage.publisher import publish_call_usage
from voicebot.orchestrator.exceptions import OrchestratorNotInitializedError
from voicebot.orchestrator.guardrails import GuardrailsEngine
from voicebot.orchestrator.intent_engine import IntentEngine
from voicebot.orchestrator.system_prompt import (
    assemble_system_prompt,
    augment_with_sentiment,
)
from voicebot.prompts.extraction_prompts import (
    build_extraction_prompt,
    get_entity_extraction_system_prompt,
)
from voicebot.prompts.rag_prompts import (
    build_rag_context_prompt,
    build_rag_miss_prompt,
)
from voicebot.prompts.system_prompts import (
    RUNNING_SUMMARY_SYSTEM_PROMPT,
    append_running_summary_section,
    build_running_summary_user_prompt,
)
from voicebot.orchestrator.rag_router import (
    RAGRouter,
    is_usable_rag_result,
)
from voicebot.audio.pcm_utils import join_pcm_chunks
from voicebot.audio.sentence_splitter import split_into_sentences
from voicebot.audio.tts_text import sanitize_for_tts, truncate_at_sentence_boundary
 
if TYPE_CHECKING:
    from voicebot.audio.tts_stream_player import TTSStreamPlayer
 
logger = logging.getLogger(__name__)
 
# ---------------------------------------------------------------------------
# Canned responses
# ---------------------------------------------------------------------------
_UNCLEAR_RESPONSE = "I'm sorry, I didn't catch that. Could you please repeat?"
_EMPTY_LLM_RESPONSE = "I understand. Could you tell me more about what you are looking for?"
_TECHNICAL_ERROR_RESPONSE = "I'm sorry, I'm having some technical difficulties. Could you please repeat that?"
_PRIVACY_RESPONSE = (
    "I have noted your request. All information stored about you will be "
    "deleted after this call ends. Is there anything else I can help you with?"
)
_GOODBYE_RESPONSE = "Thank you for calling. Have a wonderful day. Goodbye!"
_MAX_DURATION_RESPONSE = "I'm sorry, we've reached our time limit. Let me connect you with someone who can help."
_TRANSFER_RESPONSE = "Of course. Let me connect you to someone who can assist you better."
_GUARDRAIL_FALLBACK = "I understand. Let me help you with that. Could you please provide more details?"
 
# ---------------------------------------------------------------------------
# Tools that are hidden from the LLM because their backing services don't
# exist yet. Remove a name from this set once the service is live.
# ---------------------------------------------------------------------------
_RAG_TOOL_NAME = "search_knowledge_base"

# Tools hidden because backing services are not live yet.
_EXCLUDED_TOOLS = {
    "get_customer_info",
    "book_appointment",
    "get_order_status",
    "get_billing_info",
    "escalate_to_agent",
    "test_add",
}
 
 
def _build_redis_sync_client(redis_url: str) -> redis_sync.Redis:
    url = (redis_url or "").strip() or "redis://localhost:6379"
    return redis_sync.from_url(url, decode_responses=True)
 
 
def _turns_to_transcript(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        content = (t.get("content") or "").strip()
        if content:
            prefix = "Caller" if t.get("role") == "user" else "Bot"
            lines.append(f"{prefix}: {content}")
    return "\n".join(lines)
 
 
# ---------------------------------------------------------------------------
# MCP Client
# ---------------------------------------------------------------------------
class MCPClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/") + "/mcp"
        self.api_key = api_key
        self.timeout = timeout
 
    async def list_tools(self) -> list[dict]:
        try:
            async with streamablehttp_client(
                self.base_url,
                headers={"x-api-key": self.api_key},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    tools = []
                    for t in result.tools:
                        # Skip tools whose backing services are not available
                        if t.name in _EXCLUDED_TOOLS:
                            continue
                        try:
                            tool = {
                                "type": "function",
                                "function": {
                                    "name": t.name,
                                    "description": t.description or "",
                                    "parameters": t.inputSchema if t.inputSchema else {
                                        "type": "object",
                                        "properties": {},
                                    },
                                }
                            }
                            tools.append(tool)
                        except Exception as te:
                            logger.error("[MCP] Failed to parse tool %s: %s", t, te)
 
                    logger.info("[MCP] Tools loaded: %s", [t["function"]["name"] for t in tools])
                    return tools
        except Exception as e:
            logger.error("[MCP] list_tools failed: %s", e)
            return []
 
    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        try:
            async with streamablehttp_client(
                self.base_url,
                headers={"x-api-key": self.api_key},
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments)
                    return " ".join(
                        c.text for c in result.content if hasattr(c, "text")
                    )
        except Exception as e:
            logger.error("[MCP] call_tool %s failed: %s", tool_name, e)
            return f"Tool {tool_name} failed: {e}"
 
    async def close(self):
        pass  # streamablehttp_client is a context manager — no persistent connection
 
 
class VoiceBotOrchestrator:
    """
    Core brain for a single voice call.
    One instance per call. Destroyed when call ends.
 
    KEY RULES:
    - Pipeline ALWAYS reaches LLM. No exceptions.
    - LLM receives ALL Redis turns every turn.
    - Every turn is saved to Redis before returning.
    - Running summary is fire-and-forget (never blocks response path).
    - Intent classification + Redis read run concurrently; RAG prefetch when needed.
    - TTS synthesis + Redis save run concurrently per turn.
    """
 
    def __init__(self, config: VoicebotConfig):
        self.config = config
        self.call_state: CallState | None = None
        self._last_spoken: str = ""
        self._last_spoken_audio: bytes = b""
 
        self._settings = Settings()
 
        engine = config.engine
        self.stt_adapter = ModelFactory.create_stt(engine.stt_provider_id)
        self.tts_adapter = ModelFactory.create_tts(engine.tts_provider_id)
        self.llm_adapter = ModelFactory.create_llm(
            engine.llm_provider_id,
            engine.llm_model_id,
        )
        self._fallback_llm = None
 
        self.intent_engine = IntentEngine(self.llm_adapter, config)
        self.context_manager = ContextManager(config)
        self.guardrails_engine = GuardrailsEngine(config)
        self.goal_engine = GoalEngine(self.llm_adapter, config)

        # RAG: MCP search_knowledge_base only (prefetch + optional LLM tool loop).
        # RAGRouter is built from config so domain words match this voicebot's
        # industry — IT support, insurance, ecommerce, etc. No catch-all queries.
        self._rag_router = RAGRouter.from_config(config)

        self._mcp_client: MCPClient | None = None
        self._mcp_tools: list[dict] = []
        mcp_url = getattr(self._settings, "mcp_server_url", "")
        mcp_key = getattr(self._settings, "mcp_api_key", "")
        if mcp_url and mcp_key:
            self._mcp_client = MCPClient(base_url=mcp_url, api_key=mcp_key)
            logger.info("mcp_client_initialized | url=%s", mcp_url)
 
    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
 
    def _get_fallback_llm(self):
        if self._fallback_llm is None:
            self._fallback_llm = ModelFactory.create_llm(
                self.config.engine.fallback_provider_id,
                self.config.engine.fallback_model_id,
            )
        return self._fallback_llm

    def _excluded_tool_names(self) -> set[str]:
        """Per-voicebot tool allowlist (tenant config + global exclusions)."""
        excluded = set(_EXCLUDED_TOOLS)
        if not self.config.engine.enable_rag:
            excluded.add(_RAG_TOOL_NAME)
        return excluded

    def _filter_mcp_tools(self, tools: list[dict]) -> list[dict]:
        excluded = self._excluded_tool_names()
        return [
            t
            for t in tools
            if t.get("function", {}).get("name") not in excluded
        ]

    def _llm_tools(self) -> list[dict] | None:
        if not self._mcp_client or not self._mcp_tools:
            return None
        filtered = self._filter_mcp_tools(self._mcp_tools)
        return filtered or None
 
    async def initialize(self, call_id: str, caller_phone: str) -> bytes:
        self.call_state = CallState(
            call_id=call_id,
            voicebot_id=self.config.voicebot_id,
            caller_phone=caller_phone,
            tenant_id=self.config.tenant_id,
            call_start_time=datetime.utcnow(),
            detected_language=self.config.conversation_intelligence.primary_language,
        )
 
        caller_graph = await self.context_manager.on_call_start(self.call_state)
 
        # Store caller graph on call_state so goal engine can access it
        self.call_state.caller_graph = caller_graph
 
        # Fetch MCP tools BEFORE assembling the system prompt so they are
        # included in the prompt that gets baked into call_state.
        if self._mcp_client:
            raw_tools = await self._mcp_client.list_tools()
            self._mcp_tools = self._filter_mcp_tools(raw_tools)
            logger.info(
                "[MCP] %s tools for LLM (enable_rag=%s): %s",
                len(self._mcp_tools),
                self.config.engine.enable_rag,
                [t["function"]["name"] for t in self._mcp_tools],
            )
            if not self.config.engine.enable_rag:
                logger.info(
                    "[MCP] RAG disabled for voicebot %s — %s not exposed",
                    self.config.voicebot_id,
                    _RAG_TOOL_NAME,
                )
 
        self.call_state.system_prompt = assemble_system_prompt(
            self.config, caller_graph, self.call_state,
        )

        # If search_knowledge_base MCP tool is available, tell the LLM
        # exactly which tenant_id to pass — prevents guessing or hallucination.
        rag_tool_available = any(
            t.get("function", {}).get("name") == "search_knowledge_base"
            for t in self._mcp_tools
        )
        if rag_tool_available:
            business = self.config.business_name
            self.call_state.system_prompt += (
                f"\n\nKNOWLEDGE BASE: For factual questions about {business} "
                f"products, services, procedures, or troubleshooting, you MUST "
                f"use the KNOWLEDGE BASE CONTEXT section when it is provided, "
                f"or call search_knowledge_base before answering. "
                f"Never invent domain-specific facts, prices, steps, or policies. "
                f"Always pass tenant_id=\"{self.config.tenant_id}\" exactly. "
                f"Skip the knowledge base only for greetings, small talk, and "
                f"collecting contact details."
            )

        # Build greeting — LLM-generated for returning callers so it uses
        # their actual name and prior context naturally, static fallback for
        # first-time callers where no graph data exists yet.
        caller_name = ((caller_graph or {}).get("caller_name") or "").strip()
        is_returning = bool(caller_name and caller_graph and caller_graph.get("nodes"))
 
        if is_returning:
            # Ask the LLM to generate a warm, personalised greeting using
            # the system prompt that already has the full caller graph context.
            # This avoids a hardcoded script while still using the known data.
            try:
                greeting_prompt = [
                    {
                        "role": "user",
                        "content": (
                            "Generate a natural, warm greeting for this returning caller. "
                            "Use their name. Keep it to one sentence. "
                            "Do not ask how you can help yet — just greet them. "
                            "No markdown. Speak as you would on a real phone call."
                        ),
                    }
                ]
                greeting_response = await self.llm_adapter.generate(
                    messages=greeting_prompt,
                    system_prompt=self.call_state.system_prompt,
                    max_tokens=60,
                    temperature=0.7,
                )
                self.call_state.usage.record_llm(greeting_response)
                greeting = (greeting_response.text or "").strip()
                if not greeting:
                    raise ValueError("empty greeting")
                logger.info(
                    "[Greeting] LLM personalised greeting for returning caller: %s",
                    greeting,
                )
            except Exception as e:
                # Safe fallback — still uses the name, just not LLM-generated
                logger.warning("[Greeting] LLM greeting failed (%s) — using fallback", e)
                greeting = f"Welcome back, {caller_name}! How can I help you today?"
        else:
            greeting = (
                self.config.personality.greeting_message
                or f"Hello! Welcome to {self.config.business_name}. How can I help you today?"
            )
 
        logger.info(
            "call_initialized | call_id=%s | %s | caller=%s | returning=%s",
            call_id, self.config.name, caller_phone,
            "yes" if is_returning else "no",
        )
 
        greeting_audio = await self._speak(greeting)
 
        self.call_state.add_turn(
            role="assistant", content=greeting, intent="greeting", confidence=1.0,
        )
        self.call_state.turn_count = 0
 
        await self.context_manager.persist_turn(
            call_state=self.call_state,
            user_text="",
            bot_text=greeting,
            intent="greeting",
            confidence=1.0,
        )
        return greeting_audio
 
    async def handle_utterance(
        self,
        audio_bytes: bytes,
        tts_stream_player: "TTSStreamPlayer | None" = None,
    ) -> bytes:
        if self.call_state is None:
            raise OrchestratorNotInitializedError()
 
        if tts_stream_player is not None:
            return await self._handle_utterance_streaming(
                audio_bytes, tts_stream_player,
            )
 
        self._last_spoken = ""
        self._last_spoken_audio = b""
 
        # --- STT ---
        text = await self._transcribe(audio_bytes)
        if not text:
            audio = await self._speak(_UNCLEAR_RESPONSE)
            await self._save_turn("[unclear]", _UNCLEAR_RESPONSE, "unclear", 0.0)
            return audio
 
        logger.info(
            "[Turn %s] Caller said: '%s'",
            self.call_state.turn_count + 1, text,
        )
 
        # --- Escalation check (cheap — no LLM) ---
        if await self._check_escalation(text):
            await self._save_turn(text, self._last_spoken, "escalation", 1.0)
            return self._last_spoken_audio
 
        # --- Active goal ---
        if self.call_state.active_goal and not self.call_state.active_goal.paused:
            goal_result = await self.goal_engine.handle_turn(
                text=text,
                intent=self.call_state.active_goal.goal_name,
                call_state=self.call_state,
            )
            if goal_result.stop_pipeline:
                audio = await self._speak(goal_result.response_text)
                await self._save_turn(
                    text, goal_result.response_text,
                    goal_result.goal_name, 1.0,
                )
                return audio
 
        # --- Intent classification + message building: run concurrently ---
        (intent, confidence, sentiment), messages = await asyncio.gather(
            self._classify_intent(text),
            self._build_messages(text),
        )
        self.call_state.sentiment_trend = sentiment
 
        logger.info(
            "[Turn %s] Intent=%s (%.0f%%) | Sentiment=%s",
            self.call_state.turn_count + 1,
            intent, confidence * 100, sentiment,
        )
 
        # --- Special intents (no LLM needed) ---
        if intent == "privacy_request":
            self.call_state.privacy_deletion_requested = True
            audio = await self._speak(_PRIVACY_RESPONSE)
            await self._save_turn(text, _PRIVACY_RESPONSE, intent, confidence)
            return audio
 
        if intent == "goodbye":
            audio = await self._speak(_GOODBYE_RESPONSE)
            await self._save_turn(text, _GOODBYE_RESPONSE, intent, confidence)
            return audio
 
        # --- Goal routing ---
        if (
            self.goal_engine.is_goal_intent(intent)
            and confidence >= self.config.engine.confidence_threshold
        ):
            goal_result = await self.goal_engine.handle_turn(
                text=text,
                intent=intent,
                call_state=self.call_state,
            )
            if goal_result.stop_pipeline:
                audio = await self._speak(goal_result.response_text)
                await self._save_turn(
                    text, goal_result.response_text, intent, confidence,
                )
                return audio
 
        messages = await self._maybe_enrich_with_rag(text, intent, messages)

        logger.info(
            "[Turn %s] Sending %s messages to LLM (%s history turns)",
            self.call_state.turn_count + 1,
            len(messages), max(0, len(messages) - 2),
        )
 
        # --- LLM generation (with MCP tool call loop) ---
        response = await self._generate(messages)
        if not response or not response.strip():
            logger.error(
                "[Turn %s] LLM returned empty — using safe fallback",
                self.call_state.turn_count + 1,
            )
            response = _EMPTY_LLM_RESPONSE
 
        # --- Guardrails ---
        response = self._apply_guardrails(response, text)
 
        logger.info(
            "[Turn %s] Bot: '%s'",
            self.call_state.turn_count + 1, response[:120],
        )
 
        # --- TTS + Redis save: run concurrently ---
        audio, _ = await asyncio.gather(
            self._speak(response),
            self._save_turn(text, response, intent, confidence),
        )
        return audio
 
    async def _handle_utterance_streaming(
        self,
        audio_bytes: bytes,
        tts_stream_player: "TTSStreamPlayer",
    ) -> bytes:
        self._last_spoken = ""
        self._last_spoken_audio = b""
 
        text = await self._transcribe(audio_bytes)
        if not text:
            audio = await self._speak_streaming(
                _UNCLEAR_RESPONSE, tts_stream_player,
            )
            await self._save_turn("[unclear]", _UNCLEAR_RESPONSE, "unclear", 0.0)
            return audio
 
        logger.info(
            "[Turn %s] Caller said: '%s'",
            self.call_state.turn_count + 1, text,
        )
 
        if await self._check_escalation(text):
            await self._save_turn(text, self._last_spoken, "escalation", 1.0)
            return self._last_spoken_audio
 
        if self.call_state.active_goal and not self.call_state.active_goal.paused:
            goal_result = await self.goal_engine.handle_turn(
                text=text,
                intent=self.call_state.active_goal.goal_name,
                call_state=self.call_state,
            )
            if goal_result.stop_pipeline:
                audio = await self._speak_streaming(
                    goal_result.response_text, tts_stream_player,
                )
                await self._save_turn(
                    text, goal_result.response_text,
                    goal_result.goal_name, 1.0,
                )
                return audio
 
        (intent, confidence, sentiment), messages = await asyncio.gather(
            self._classify_intent(text),
            self._build_messages(text),
        )
        self.call_state.sentiment_trend = sentiment
 
        logger.info(
            "[Turn %s] Intent=%s (%.0f%%) | Sentiment=%s",
            self.call_state.turn_count + 1,
            intent, confidence * 100, sentiment,
        )
 
        if intent == "privacy_request":
            self.call_state.privacy_deletion_requested = True
            audio = await self._speak_streaming(
                _PRIVACY_RESPONSE, tts_stream_player,
            )
            await self._save_turn(text, _PRIVACY_RESPONSE, intent, confidence)
            return audio
 
        if intent == "goodbye":
            audio = await self._speak_streaming(
                _GOODBYE_RESPONSE, tts_stream_player,
            )
            await self._save_turn(text, _GOODBYE_RESPONSE, intent, confidence)
            return audio
 
        if (
            self.goal_engine.is_goal_intent(intent)
            and confidence >= self.config.engine.confidence_threshold
        ):
            goal_result = await self.goal_engine.handle_turn(
                text=text,
                intent=intent,
                call_state=self.call_state,
            )
            if goal_result.stop_pipeline:
                audio = await self._speak_streaming(
                    goal_result.response_text, tts_stream_player,
                )
                await self._save_turn(
                    text, goal_result.response_text, intent, confidence,
                )
                return audio
 
        messages = await self._maybe_enrich_with_rag(text, intent, messages)

        logger.info(
            "[Turn %s] Sending %s messages to LLM (%s history turns)",
            self.call_state.turn_count + 1,
            len(messages), max(0, len(messages) - 2),
        )
 
        response = await self._generate(messages)
        if not response or not response.strip():
            logger.error(
                "[Turn %s] LLM returned empty — using safe fallback",
                self.call_state.turn_count + 1,
            )
            response = _EMPTY_LLM_RESPONSE
 
        response = self._apply_guardrails(response, text)
 
        logger.info(
            "[Turn %s] Bot: '%s'",
            self.call_state.turn_count + 1, response[:120],
        )
 
        audio, _ = await asyncio.gather(
            self._speak_streaming(response, tts_stream_player),
            self._save_turn(text, response, intent, confidence),
        )
        return audio
 
    async def end_call(self, reason: str = "normal") -> dict | None:
        if self.call_state is None:
            logger.warning("end_call called with no call_state")
            return None
 
        logger.info(
            "[end_call] Starting | reason=%s | call_id=%s | turns=%s",
            reason, self.call_state.call_id, self.call_state.turn_count,
        )
 
        extraction = None
        try:
            try:
                transcript = await self._get_transcript()
            except asyncio.CancelledError:
                logger.warning(
                    "[end_call] Transcript fetch cancelled — using in-memory dialogue"
                )
                transcript = self.call_state.transcript_as_dialogue()
 
            logger.info(
                "[end_call] Transcript length=%s | preview=%s",
                len(transcript), transcript[:200],
            )
 
            if self.call_state.privacy_deletion_requested:
                logger.info("[end_call] Privacy flag — skipping extraction")
                extraction = None
            elif not transcript.strip():
                logger.warning("[end_call] Empty transcript — saving minimal record")
                extraction = self._minimal_extraction("empty transcript")
            else:
                extraction = await self._extract_entities(transcript)
 
        except Exception as e:
            logger.error("[end_call] Processing error: %s", e, exc_info=True)
            extraction = self._minimal_extraction("error during processing")
 
        finally:
            try:
                await self.context_manager.on_call_end(
                    call_state=self.call_state,
                    extraction=extraction,
                )
                logger.info("[end_call] on_call_end completed ✅")
            except Exception as e:
                logger.error("[end_call] on_call_end failed: %s", e, exc_info=True)
                self._force_delete_redis_session()
 
            if self._mcp_client:
                try:
                    await self._mcp_client.close()
                    self._mcp_client = None
                    logger.info("[end_call] MCP client closed ✅")
                except Exception as e:
                    logger.warning("[end_call] MCP client close failed: %s", e)
 
            try:
                await publish_call_usage(
                    self.call_state,
                    self.config.engine,
                    self.call_state.usage,
                )
            except Exception as e:
                logger.warning("[end_call] Usage publish failed (non-fatal): %s", e)
 
        logger.info("[end_call] Done | call_id=%s", self.call_state.call_id)
        return extraction
 
    # ------------------------------------------------------------------
    # Internal — per-turn pipeline steps
    # ------------------------------------------------------------------
 
    async def _transcribe(self, audio_bytes: bytes) -> str | None:
        try:
            ci = self.config.conversation_intelligence
            result = await self.stt_adapter.transcribe(
                audio_bytes=audio_bytes,
                language=self.call_state.detected_language,
                auto_detect=ci.auto_language_detection,
            )
            if (
                result.detected_language
                and result.detected_language != self.call_state.detected_language
            ):
                logger.info(
                    "Language switched: %s → %s",
                    self.call_state.detected_language,
                    result.detected_language,
                )
                self.call_state.detected_language = result.detected_language
 
            text = (result.text or "").strip()
            if not text or result.confidence < 0.4:
                return None
            self.call_state.usage.record_stt(audio_bytes)
            return text
        except Exception as e:
            logger.error("STT failed: %s", e)
            return None
 
    async def _classify_intent(self, text: str) -> tuple[str, float, str]:
        try:
            result = await self.intent_engine.classify(text, self.call_state)
            return result.intent, result.confidence, result.sentiment
        except Exception as e:
            logger.error("Intent classification failed: %s", e)
            return "general_query", 0.5, "neutral"
 
    async def _check_escalation(self, text: str) -> bool:
        max_dur = self.config.escalation.max_call_duration
        elapsed = self.call_state.call_duration_minutes()
        pct = elapsed / max_dur if max_dur > 0 else 0
 
        if 0.8 <= pct < 1.0:
            warning = "\n\nNOTE: This call is nearing its time limit. Please wrap up soon."
            if warning not in self.call_state.system_prompt:
                self.call_state.system_prompt += warning
 
        if pct >= 1.0:
            self.call_state.escalation_triggered = True
            self.call_state.escalation_reason = "max_duration"
            self._last_spoken = _MAX_DURATION_RESPONSE
            self._last_spoken_audio = await self._speak(_MAX_DURATION_RESPONSE)
            logger.info("[Escalation] Max duration reached")
            return True
 
        keywords = self.config.escalation.transfer_keywords()
        text_lower = text.lower()
        if any(kw and kw in text_lower for kw in keywords):
            self.call_state.escalation_triggered = True
            self.call_state.escalation_reason = "transfer_requested"
            self._last_spoken = _TRANSFER_RESPONSE
            self._last_spoken_audio = await self._speak(_TRANSFER_RESPONSE)
            logger.info("[Escalation] Transfer keyword detected")
            return True
 
        return False
 
    async def _maybe_enrich_with_rag(
        self,
        text: str,
        intent: str,
        messages: list[dict],
    ) -> list[dict]:
        """Prefetch KB context server-side when this turn needs domain facts."""
        if not self._rag_router.should_prefetch(text=text, intent=intent):
            return messages

        logger.info("[RAG] Prefetching knowledge base | intent=%s", intent)
        context = await self._fetch_rag_context(text)
        return self._inject_rag_into_messages(messages, context)

    async def _fetch_rag_context(self, query: str) -> str:
        """Retrieve KB context via MCP search_knowledge_base only."""
        if not self._mcp_client or _RAG_TOOL_NAME in self._excluded_tool_names():
            logger.warning(
                "[RAG] MCP unavailable or %s disabled — skipping prefetch",
                _RAG_TOOL_NAME,
            )
            return ""

        raw = await self._mcp_client.call_tool(
            _RAG_TOOL_NAME,
            {"query": query, "tenant_id": self.config.tenant_id},
        )
        if is_usable_rag_result(raw):
            return raw.strip()
        return ""

    def _inject_rag_into_messages(
        self,
        messages: list[dict],
        context: str,
    ) -> list[dict]:
        if not messages or messages[0].get("role") != "system":
            return messages

        business = self.config.business_name
        if is_usable_rag_result(context):
            rag_block = build_rag_context_prompt(context, business_name=business)
            logger.info("[RAG] Injected %s chars of KB context", len(context))
        else:
            rag_block = build_rag_miss_prompt(business_name=business)
            logger.info("[RAG] No KB hits — injected anti-hallucination guard")

        updated = list(messages)
        updated[0] = {
            **updated[0],
            "content": (updated[0].get("content") or "") + rag_block,
        }
        return updated

    async def _build_messages(self, current_text: str) -> list[dict]:
        """Build LLM messages list (Redis history + current utterance)."""
        redis_task = asyncio.create_task(
            self.context_manager._session.get_full_session(
                tenant_id=self.call_state.tenant_id,
                voicebot_id=self.call_state.voicebot_id,
                call_id=self.call_state.call_id,
            )
        )

        try:
            session = await redis_task
            if session:
                redis_turns = session.get("turns", [])
                running_summary = session.get("running_summary")
                logger.info("[Context] Read %s turns from Redis", len(redis_turns))
            else:
                logger.warning(
                    "[Context] Redis session not found — falling back to in-memory turns"
                )
                redis_turns = [
                    {"role": t.role, "content": t.content}
                    for t in self.call_state.turns
                ]
                running_summary = self.call_state.running_summary
        except Exception as e:
            logger.error("[Context] Redis read error: %s — using in-memory fallback", e)
            redis_turns = [
                {"role": t.role, "content": t.content}
                for t in self.call_state.turns
            ]
            running_summary = self.call_state.running_summary

        system_content = self.call_state.system_prompt
        if running_summary:
            system_content += append_running_summary_section(running_summary)

        # RAG context is no longer injected here — the LLM calls the
        # search_knowledge_base MCP tool on demand when it needs facts.

        messages = [{"role": "system", "content": system_content}]
        for turn in redis_turns:
            content = (turn.get("content") or "").strip()
            role = turn.get("role", "user")
            if content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": current_text})
        return messages
 
    async def _generate(self, messages: list[dict]) -> str:
        """
        Call the LLM and handle MCP tool calls in a loop until the LLM
        produces a final text response with no further tool calls.
        Falls back to the fallback LLM if the primary fails.
        """
        system_content = ""
        if messages and messages[0].get("role") == "system":
            system_content = messages[0].get("content") or ""
        logger.info("Final system prompt length: %s", len(system_content))
 
        try:
            start = time.perf_counter()
 
            tools = self._llm_tools()

            response = await self.llm_adapter.generate(
                messages=messages,
                system_prompt=self.call_state.system_prompt,
                max_tokens=300,
                temperature=0.7,
                **({"tools": tools} if tools else {}),
            )
            self.call_state.usage.record_llm(response)
            latency = (time.perf_counter() - start) * 1000
 
            # --- MCP tool call loop ---
            loop_count = 0
            max_tool_loops = 5
            while (
                self._mcp_client
                and getattr(response, "tool_calls", None)
                and loop_count < max_tool_loops
            ):
                loop_count += 1
                tool_results = []
 
                for tc in response.tool_calls:
                    tool_name = tc.function.name
                    tool_args = (
                        json.loads(tc.function.arguments)
                        if isinstance(tc.function.arguments, str)
                        else tc.function.arguments
                    )
                    tool_id = tc.id

                    if tool_name in self._excluded_tool_names():
                        logger.warning(
                            "[MCP] blocked tool_call=%s (not allowed for this voicebot)",
                            tool_name,
                        )
                        result = (
                            "This tool is not available for this voicebot configuration."
                        )
                    else:
                        logger.info(
                            "[MCP] tool_call=%s | args=%s | loop=%s",
                            tool_name, tool_args, loop_count,
                        )
                        result = await self._mcp_client.call_tool(
                            tool_name, tool_args
                        )
                    logger.info(
                        "[MCP] tool_result=%s | preview=%s",
                        tool_name, result[:120],
                    )
                    tool_results.append({
                        "tool_call_id": tool_id,
                        "role": "tool",
                        "content": result,
                    })
 
                messages = messages + [
                    {"role": "assistant", "tool_calls": response.tool_calls},
                    *tool_results,
                ]
                response = await self.llm_adapter.generate(
                    messages=messages,
                    system_prompt=self.call_state.system_prompt,
                    max_tokens=300,
                    temperature=0.7,
                    **({"tools": tools} if tools else {}),
                )
                self.call_state.usage.record_llm(response)
 
            if loop_count >= max_tool_loops:
                logger.warning("[MCP] max tool loop iterations reached (%s)", max_tool_loops)
 
            text = (response.text or "").strip()
            logger.info(
                "[LLM] Primary responded | latency=%.0fms | length=%s | preview='%s'",
                latency, len(text), text[:80],
            )
            if text:
                return text
            raise ValueError("LLM returned empty string")
 
        except Exception as e:
            logger.warning("[LLM] Primary failed: %s | trying fallback", e)
 
        try:
            fallback = self._get_fallback_llm()
            response = await fallback.generate(
                messages=messages,
                system_prompt=self.call_state.system_prompt,
                max_tokens=300,
                temperature=0.7,
            )
            self.call_state.usage.record_llm(response)
            text = (response.text or "").strip()
            if text:
                logger.info("[LLM] Fallback succeeded: '%s'", text[:80])
                return text
            raise ValueError("Fallback LLM also returned empty")
 
        except Exception as e:
            logger.error("[LLM] Both LLMs failed: %s", e)
            return _TECHNICAL_ERROR_RESPONSE
 
    def _apply_guardrails(self, response: str, caller_utterance: str) -> str:
        result = self.guardrails_engine.check(
            response, self.call_state, caller_utterance=caller_utterance,
        )
        if result.passed:
            return response
 
        logger.warning(
            "[Guardrails] Violation: type=%s detail=%s action=%s",
            result.violation_type, result.violation_detail, result.suggested_action,
        )
        if result.suggested_action == "truncate":
            return truncate_at_sentence_boundary(response, max_words=100)
        if not result.critical:
            return response
        if result.violation_type != "confidential":
            logger.warning(
                "Guardrails blocked response | reason=%s", result.violation_type,
            )
        return _GUARDRAIL_FALLBACK
 
    async def _speak(self, text: str) -> bytes:
        text = sanitize_for_tts(text, ensure_terminal_punct=True)
        if not text:
            return b""
        try:
            chunks = []
            async for chunk in self.tts_adapter.synthesize_stream(
                text=text,
                voice_id=self.config.engine.voice_id,
                speed=self.config.engine.voice_speed,
                pitch=self.config.engine.voice_pitch,
            ):
                if chunk:
                    chunks.append(chunk)
            audio = join_pcm_chunks(chunks, sample_rate=8000)
            self.call_state.usage.record_tts(audio, characters=len(text))
            self._last_spoken = text
            self._last_spoken_audio = audio
            return audio
        except Exception as e:
            logger.error("TTS failed: %s", e)
            self._last_spoken = text
            return b""
 
    async def _speak_streaming(
        self,
        text: str,
        tts_stream_player: "TTSStreamPlayer",
    ) -> bytes:
        text = sanitize_for_tts(text)
        if not text:
            return b""
        try:
            sentences = split_into_sentences(text)
            audio = await tts_stream_player.stream_sentences(
                sentences,
                voice_id=self.config.engine.voice_id,
                speed=self.config.engine.voice_speed,
                pitch=self.config.engine.voice_pitch,
            )
            self.call_state.usage.record_tts(
                audio,
                characters=tts_stream_player.characters_synthesized,
            )
            self._last_spoken = text
            self._last_spoken_audio = audio
            return audio
        except Exception as e:
            logger.error("TTS streaming failed: %s", e)
            self._last_spoken = text
            return b""
 
    async def _save_turn(
        self,
        user_text: str,
        bot_text: str,
        intent: str,
        confidence: float,
    ) -> None:
        if user_text and user_text != "[unclear]":
            self.call_state.add_turn(
                role="user", content=user_text,
                intent=intent, confidence=confidence,
            )
        self.call_state.add_turn(role="assistant", content=bot_text or "")
        self.call_state.turn_count += 1
 
        try:
            await self.context_manager.persist_turn(
                call_state=self.call_state,
                user_text=user_text,
                bot_text=bot_text or "",
                intent=intent,
                confidence=confidence,
            )
            logger.info(
                "[Save] Turn %s saved to Redis | total turns in memory: %s",
                self.call_state.turn_count, len(self.call_state.turns),
            )
        except Exception as e:
            logger.error("[Save] Redis save failed: %s", e)
 
        if self.call_state.turn_count % 3 == 0:
            self.call_state.system_prompt = augment_with_sentiment(
                self.call_state.system_prompt,
                self.call_state.sentiment_trend,
            )
 
        if self.call_state.turn_count % 5 == 0 and self.call_state.turn_count > 0:
            asyncio.create_task(
                self._generate_running_summary(),
                name=f"summary-{self.call_state.call_id}-t{self.call_state.turn_count}",
            )
 
    # ------------------------------------------------------------------
    # Internal — background / end-of-call helpers
    # ------------------------------------------------------------------
 
    async def _generate_running_summary(self) -> None:
        transcript = self.call_state.transcript_as_dialogue()
        if not transcript.strip():
            return
        prompt = build_running_summary_user_prompt(
            transcript, self.call_state.running_summary,
        )
        try:
            response = await self.llm_adapter.generate(
                messages=[{"role": "user", "content": prompt}],
                system_prompt=RUNNING_SUMMARY_SYSTEM_PROMPT,
                max_tokens=200,
                temperature=0.1,
            )
            self.call_state.usage.record_llm(response)
            if response.text and response.text.strip():
                self.call_state.running_summary = response.text.strip()
                self.call_state.running_summary_turn = self.call_state.turn_count
                logger.info(
                    "[Summary] Generated at turn %s: %s",
                    self.call_state.turn_count,
                    (self.call_state.running_summary or "")[:100],
                )
        except Exception as e:
            logger.error("[Summary] Failed: %s", e)
 
    async def _get_transcript(self) -> str:
        try:
            session = await asyncio.wait_for(
                self.context_manager._session.get_full_session(
                    tenant_id=self.call_state.tenant_id,
                    voicebot_id=self.call_state.voicebot_id,
                    call_id=self.call_state.call_id,
                ),
                timeout=5.0,
            )
            if session:
                turns = session.get("turns", [])
                if turns:
                    transcript = _turns_to_transcript(turns)
                    logger.info("[Transcript] From Redis: %s turns", len(turns))
                    return transcript
 
        except (asyncio.CancelledError, asyncio.TimeoutError) as e:
            logger.warning(
                "[Transcript] Redis %s — trying sync fallback", type(e).__name__,
            )
            transcript = self._get_transcript_sync()
            if transcript:
                return transcript
 
        except Exception as e:
            logger.warning(
                "[Transcript] Redis read failed: %s — using memory fallback", e,
            )
 
        transcript = self.call_state.transcript_as_dialogue()
        logger.info("[Transcript] From memory: %s turns", len(self.call_state.turns))
        return transcript
 
    def _get_transcript_sync(self) -> str:
        try:
            r = _build_redis_sync_client(self._settings.redis_url)
            key = (
                f"session:{self.call_state.tenant_id}"
                f":{self.call_state.voicebot_id}"
                f":{self.call_state.call_id}"
            )
            raw = r.get(key)
            if raw:
                session = json.loads(raw)
                turns = session.get("turns", [])
                transcript = _turns_to_transcript(turns)
                if transcript:
                    logger.info(
                        "[Transcript] From sync Redis: %s turns", len(turns),
                    )
                    return transcript
        except Exception as e:
            logger.warning("[Transcript] Sync Redis failed: %s", e)
        return ""
 
    def _force_delete_redis_session(self) -> None:
        try:
            r = _build_redis_sync_client(self._settings.redis_url)
            key = (
                f"session:{self.call_state.tenant_id}"
                f":{self.call_state.voicebot_id}"
                f":{self.call_state.call_id}"
            )
            r.delete(key)
            logger.info("[end_call] Redis force deleted ✅")
        except Exception as e:
            logger.error("[end_call] Redis force delete also failed: %s", e)
 
    async def _extract_entities(self, transcript: str) -> dict:
        duration_seconds = self.call_state.call_duration_seconds()
        prompt = build_extraction_prompt(
            voicebot_name=self.config.name,
            business_name=self.config.business_name,
            caller_phone=self.call_state.caller_phone,
            duration_seconds=duration_seconds,
            date=datetime.utcnow().strftime("%Y-%m-%d"),
            transcript=transcript,
            call_data_extraction=self.config.call_data_extraction,
        )
        try:
            response = await asyncio.wait_for(
                self.llm_adapter.generate(
                    messages=[{"role": "user", "content": prompt}],
                    system_prompt=get_entity_extraction_system_prompt(),
                    max_tokens=1500,
                    temperature=0.1,
                ),
                timeout=30.0,
            )
            self.call_state.usage.record_llm(response)
            extraction = self._parse_json(response.text)
            logger.info(
                "[Extract] Done | caller_name=%s | nodes=%s | edges=%s | summary=%s",
                extraction.get("caller_name"),
                len(extraction.get("nodes", [])),
                len(extraction.get("edges", [])),
                (extraction.get("summary", "") or "")[:80],
            )
            return extraction
        except Exception as e:
            logger.error("[Extract] Failed: %s", e)
            return self._minimal_extraction("extraction failed")
 
    def _parse_json(self, raw: str) -> dict:
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            return json.loads(clean.strip())
        except Exception as e:
            logger.error("JSON parse failed: %s", e)
            return self._minimal_extraction("json parse failed")
 
    def _minimal_extraction(self, reason: str) -> dict:
        return {
            "caller_name": None,
            "caller_email": None,
            "call_duration_seconds": self.call_state.call_duration_seconds(),
            "nodes": [],
            "edges": [],
            "summary": (
                f"Call with {self.call_state.caller_phone} | "
                f"{self.call_state.turn_count} turns | {reason}"
            ),
        }