"""ConversationBrain — the frame processor between STT and TTS.

For every final user transcription it:
  1. records the turn,
  2. routes it (workflow / call-control / intent / knowledge / chat),
  3. optionally performs tenant-safe KB retrieval,
  4. streams the LLM answer downstream as TextFrames (TTS aggregates them),
and cancels all in-flight work the instant the caller barges in
(InterruptionFrame / UserStartedSpeakingFrame passing through the pipeline).
"""

import asyncio
import json
import logging
import time

from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputTransportMessageFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from backend.knowledge.schemas import RetrievalRequest
from backend.knowledge.security import sanitize_for_context
from backend.orchestration.router import RouteDecision, RouteKind, TurnRouter
from backend.providers.base import LLMProvider
from backend.voice_runtime.bot_config import ResolvedBotConfig
from backend.voice_runtime.session import SessionRecorder, TurnRecord

logger = logging.getLogger(__name__)

_HISTORY_MAX_TURNS = 20


class ConversationBrain(FrameProcessor):
    def __init__(
        self,
        *,
        config: ResolvedBotConfig,
        llm: LLMProvider,
        recorder: SessionRecorder,
        knowledge_service=None,
        workflow_engine=None,
    ) -> None:
        super().__init__()
        self._config = config
        self._llm = llm
        self._recorder = recorder
        self._knowledge = knowledge_service
        self._workflows = workflow_engine
        self._router = TurnRouter(
            intents=config.intents,
            has_knowledge_bases=bool(config.kb_ids),
        )
        self._history: list[dict] = []
        self._generation: asyncio.Task | None = None
        self._active_workflow: str | None = None
        self._last_bot_reply: str = ""

    # ── pipeline plumbing ─────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)):
            await self._cancel_generation("barge_in")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            await self._cancel_generation("new_turn")
            self._generation = self.create_task(self._handle_turn(frame.text))
            return

        await self.push_frame(frame, direction)

    async def _cancel_generation(self, reason: str) -> None:
        if self._generation is not None and not self._generation.done():
            await self.cancel_task(self._generation)
            await self._recorder.flush_event("generation_cancelled", reason=reason)
        self._generation = None

    async def cleanup(self):
        await self._cancel_generation("cleanup")
        await super().cleanup()

    # ── turn handling ─────────────────────────────────────────────────────

    async def _notify_client(self, payload: dict) -> None:
        """Side-channel JSON to the transport (live transcripts for test UIs)."""
        await self.push_frame(OutputTransportMessageFrame(message=payload))

    async def _handle_turn(self, text: str) -> None:
        started = time.perf_counter()
        await self._notify_client({"type": "transcript", "text": text})
        decision = self._router.decide(text, active_workflow=self._active_workflow)
        turn = TurnRecord(role="user", text=text, route=decision.kind.value)
        self._recorder.add_turn(turn)
        self._recorder.add_event(
            "route_decision",
            route=decision.kind.value,
            reason=decision.reason,
            confidence=decision.confidence,
            considered_kb=decision.considered_kb,
        )
        self._history.append({"role": "user", "content": text})
        del self._history[:-_HISTORY_MAX_TURNS]

        try:
            if decision.kind == RouteKind.CALL_CONTROL:
                await self._handle_call_control(decision)
            elif decision.kind == RouteKind.HANDOFF:
                await self._handle_handoff(decision)
            elif decision.kind == RouteKind.SAFETY:
                await self._say(
                    "For your security, please never share card numbers, OTPs or "
                    "passwords on this call. How else can I help you?"
                )
            elif decision.kind == RouteKind.WORKFLOW and self._workflows is not None:
                await self._handle_workflow(decision, text)
            elif decision.kind == RouteKind.CLARIFY:
                await self._say("Sorry, could you tell me a bit more about what you need?")
            else:
                await self._generate_reply(text, decision, started)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad turn must not kill the call
            logger.exception("turn handling failed")
            await self._say(
                "I'm sorry, something went wrong on my end. Could you say that again?"
            )

    async def _handle_call_control(self, decision: RouteDecision) -> None:
        if decision.action == "hangup":
            await self._say("Thank you for calling. Goodbye!")
            await self._recorder.flush_event("call_control", action="hangup")
            await self.push_frame(EndWorkerFrame(reason="caller_hangup_request"))
        elif decision.action == "repeat":
            await self._say(self._last_bot_reply or "I haven't said anything yet.")
        elif decision.action == "slower":
            await self._recorder.flush_event("call_control", action="slower")
            await self._say("Of course, I'll slow down. What would you like to know?")
        else:
            await self._say("Alright.")

    async def _handle_handoff(self, decision: RouteDecision) -> None:
        await self._recorder.flush_event("handoff", reason=decision.reason)
        await self._say(
            "I understand — let me connect you with a human agent. Please hold on."
        )
        # Telephony transports implement the actual transfer; in browser test
        # sessions this event is surfaced to the client for verification.

    async def _handle_workflow(self, decision: RouteDecision, text: str) -> None:
        workflow_name = decision.action or self._active_workflow or "default"
        reply, done = await self._workflows.handle_turn(
            session_id=self._recorder.session_id,
            tenant_id=self._config.tenant_id,
            bot_id=self._config.bot_id,
            workflow_name=workflow_name,
            user_text=text,
        )
        self._active_workflow = None if done else workflow_name
        await self._say(reply)

    # ── generation ────────────────────────────────────────────────────────

    async def _generate_reply(
        self, text: str, decision: RouteDecision, started: float
    ) -> None:
        system = self._config.system_prompt
        kb_sources: list[dict] = []
        retrieval_ms = 0.0

        if decision.kind == RouteKind.KNOWLEDGE and self._knowledge is not None:
            self._recorder.usage["kb_searches"] += 1
            result = await self._knowledge.search(
                RetrievalRequest(
                    tenant_id=self._config.tenant_id,
                    kb_ids=self._config.kb_ids or None,
                    bot_id=self._config.bot_id,
                    query=text,
                )
            )
            retrieval_ms = result.duration_ms
            self._recorder.add_event(
                "kb_retrieval",
                kb_ids=result.kb_ids,
                answerable=result.answerable,
                confidence=result.confidence,
                sources=len(result.sources),
                duration_ms=result.duration_ms,
            )
            if result.answerable:
                context_lines = [
                    f"[{i + 1}] ({s.document_name or s.document_id}"
                    + (f", page {s.page_number}" if s.page_number else "")
                    + f") {sanitize_for_context(s.text)}"
                    for i, s in enumerate(result.sources)
                ]
                system = (
                    system
                    + "\n\nAnswer using ONLY the reference context below. Quote facts "
                    "exactly; do not add information that is not in the context.\n"
                    "Context:\n" + "\n".join(context_lines)
                )
                kb_sources = [
                    {
                        "kbId": s.kb_id,
                        "documentId": s.document_id,
                        "chunkId": s.chunk_id,
                        "page": s.page_number,
                        "score": s.score,
                    }
                    for s in result.sources
                ]
            else:
                await self._say(
                    "I couldn't find that in the information I have. "
                    "Would you like me to connect you with a human agent?"
                )
                return

        first_token_ms: float | None = None
        reply_parts: list[str] = []
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            async for token in self._llm.stream(
                self._history, system=system, max_tokens=256
            ):
                if first_token_ms is None:
                    first_token_ms = (time.perf_counter() - started) * 1000
                reply_parts.append(token)
                await self.push_frame(TextFrame(token))
        finally:
            await self.push_frame(LLMFullResponseEndFrame())

        reply = "".join(reply_parts).strip()
        if reply:
            await self._notify_client({"type": "bot_text", "text": reply})
            self._last_bot_reply = reply
            self._history.append({"role": "assistant", "content": reply})
            self._recorder.usage["llm_output_tokens"] += len(reply) // 4
            self._recorder.add_turn(
                TurnRecord(
                    role="bot",
                    text=reply,
                    route=decision.kind.value,
                    kb_used=bool(kb_sources),
                    kb_sources=kb_sources,
                    latency_ms={
                        "retrieval": round(retrieval_ms, 1),
                        "llm_first_token": round(first_token_ms or 0.0, 1),
                        "total": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            )

    async def _say(self, text: str) -> None:
        """Speak a fixed phrase through the TTS path."""
        self._last_bot_reply = text
        self._history.append({"role": "assistant", "content": text})
        self._recorder.add_turn(TurnRecord(role="bot", text=text))
        await self._notify_client({"type": "bot_text", "text": text})
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(TextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def speak_greeting(self) -> None:
        await self._say(self._config.greeting)
