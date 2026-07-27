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
import re
import time

from pipecat.frames.frames import (
    EndWorkerFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputTransportMessageFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from shared.knowledge.schemas import RetrievalRequest
from shared.knowledge.security import sanitize_for_context
from shared.orchestration.router import RouteDecision, RouteKind, TurnRouter
from shared.providers.base import LLMProvider, ProviderError
from shared.providers.languages import to_platform_language
from shared.bot_config import ResolvedBotConfig
from voice_runtime.frames import SwitchVoiceLanguageFrame, TTSFlushHintFrame
from voice_runtime.recording import SessionRecorder, TurnRecord

logger = logging.getLogger(__name__)

_HISTORY_MAX_TURNS = 20
# Mid-response flush: if the LLM stalls this long with text already buffered,
# nudge the TTS to start rendering what we have.
_LLM_PAUSE_FLUSH_SECONDS = 0.6

# ── conversation-language following ─────────────────────────────────────────
# The conversation follows the caller's CURRENT language (per meaningful
# utterance), while the bot's default language is only the starting point.
# Switches are stabilized so a single borrowed word never flips the language:
# the utterance must be long enough AND its dominant script must agree with
# the language the STT detected.
_MIN_SWITCH_WORDS = 2
_DEVANAGARI_CHARS = re.compile(r"[ऀ-ॿ]")
_LATIN_CHARS = re.compile(r"[A-Za-z]")

_LANGUAGE_LABELS = {
    "hi": "Hindi", "en": "English", "bn": "Bengali", "ta": "Tamil",
    "te": "Telugu", "mr": "Marathi", "gu": "Gujarati", "kn": "Kannada",
    "ml": "Malayalam", "pa": "Punjabi", "or": "Odia", "ur": "Urdu",
}


def language_label(locale: str | None) -> str:
    """Readable language name for a platform locale ("hi-IN" → "Hindi")."""
    if not locale:
        return ""
    return _LANGUAGE_LABELS.get(locale.split("-")[0].lower(), locale)


def script_supports_language(text: str, locale: str) -> bool:
    """Whether an utterance's dominant script is consistent with a locale.

    Hindi speech is transcribed in Devanagari (borrowed English words stay
    Latin, so code-mixed text still counts as Hindi when Devanagari holds a
    meaningful share). English must be clearly Latin-dominant. Languages we
    have no script heuristic for pass through on the STT's word alone.
    """
    dev = len(_DEVANAGARI_CHARS.findall(text))
    lat = len(_LATIN_CHARS.findall(text))
    total = dev + lat
    if total == 0:
        return False
    base = locale.split("-")[0].lower()
    if base == "hi":
        return dev / total >= 0.4
    if base == "en":
        return lat / total >= 0.7
    return True


class ConversationBrain(FrameProcessor):
    def __init__(
        self,
        *,
        config: ResolvedBotConfig,
        llm: LLMProvider,
        recorder: SessionRecorder,
        knowledge_service=None,
        workflow_engine=None,
        client_info: dict | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._llm = llm
        self._recorder = recorder
        self._knowledge = knowledge_service
        self._workflows = workflow_engine
        self._client_info = client_info
        self._router = TurnRouter(
            intents=config.intents,
            has_knowledge_bases=bool(config.kb_ids),
        )
        self._history: list[dict] = []
        self._generation: asyncio.Task | None = None
        self._active_workflow: str | None = None
        self._last_bot_reply: str = ""
        self._conversation_language: str = config.language
        llm_settings = (config.llm or {}).get("settings") or {}
        self._llm_temperature: float = float(llm_settings.get("temperature", 0.3))
        self._llm_max_tokens: int = int(llm_settings.get("max_tokens", 256))
        self._llm_max_retries: int = int(llm_settings.get("max_retries", 1))
        self._pipeline_started = False
        self._pending_greeting = False

    # ── pipeline plumbing ─────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            # The transport's client-connected handler can fire speak_greeting
            # before the StartFrame has propagated (cold start) — frames pushed
            # that early are dropped by pipecat, so the greeting is held here.
            self._pipeline_started = True
            await self.push_frame(frame, direction)
            if self._pending_greeting:
                self._pending_greeting = False
                self._generation = self.create_task(self._open_session())
            return

        if isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)):
            await self._cancel_generation("barge_in")
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TranscriptionFrame):
            await self._cancel_generation("new_turn")
            await self._maybe_switch_language(frame)
            self._generation = self.create_task(self._handle_turn(frame.text))
            return

        await self.push_frame(frame, direction)

    def _supported_languages(self) -> list[str]:
        return self._config.languages or [self._config.language]

    def _match_supported(self, detected: str) -> str | None:
        """Map a detected code onto the bot's configured locale set."""
        supported = self._supported_languages()
        if detected in supported:
            return detected
        base = detected.split("-")[0].lower()
        for locale in supported:
            if locale.split("-")[0].lower() == base:
                return locale
        return None

    async def _maybe_switch_language(self, frame: TranscriptionFrame) -> None:
        """Follow the caller's CURRENT language, with stability rules.

        A switch happens only when the STT-detected language (a) maps onto a
        language this bot is configured for, (b) belongs to a meaningful
        utterance (not a single borrowed word / filler), and (c) agrees with
        the utterance's dominant script. Conversation history, intent state
        and the session itself are untouched by a switch.
        """
        raw = getattr(frame, "language", None)
        if not raw:
            return
        detected = to_platform_language(
            self._config.stt.get("provider", ""), getattr(raw, "value", str(raw))
        )
        if not detected:
            return
        target = self._match_supported(detected)
        if target is None:
            # Unsupported language: keep the conversation language, tell the
            # client clearly instead of silently continuing (or crashing).
            self._recorder.add_event(
                "language_unsupported", language=detected,
                current=self._conversation_language,
            )
            await self._notify_client({
                "type": "event", "name": "language_unsupported",
                "language": detected,
            })
            return
        if target == self._conversation_language:
            return
        text = (frame.text or "").strip()
        if len(text.split()) < _MIN_SWITCH_WORDS:
            return  # too short to re-decide the conversation language
        if not script_supports_language(text, target):
            return  # a borrowed word or mixed utterance — don't oscillate
        self._recorder.add_event(
            "language_detected",
            language=target,
            previous=self._conversation_language,
        )
        self._conversation_language = target
        await self.push_frame(SwitchVoiceLanguageFrame(language=target))
        await self._notify_client({"type": "language", "language": target})

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

    def _language_instruction(self) -> str:
        """Per-turn system-prompt suffix binding the reply to the caller's
        CURRENT language. Only the reply language changes — the role, business
        rules, safety rules and conversation state are explicitly preserved."""
        label = language_label(self._conversation_language)
        if not label:
            return ""
        return (
            f"\n\n# Current conversation language\n"
            f"The caller is currently speaking {label}. Reply ONLY in {label}"
            + (
                " (natural spoken Hindi; everyday English loan-words are fine)"
                if label == "Hindi" else ""
            )
            + ". If the caller switches language, follow them from the next "
            "turn. This changes the reply language only — never the rules, "
            "role, or facts above."
        )

    async def _generate_reply(
        self, text: str, decision: RouteDecision, started: float
    ) -> None:
        system = self._config.system_prompt + self._language_instruction()
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
            first_token_ms = await self._stream_llm_tokens(reply_parts, system, started)
        finally:
            await self.push_frame(LLMFullResponseEndFrame())

        reply = "".join(reply_parts).strip()
        self._record_llm_usage(reply)
        if reply:
            await self._notify_client({"type": "bot_text", "text": reply})
            self._last_bot_reply = reply
            self._history.append({"role": "assistant", "content": reply})
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

    def _record_llm_usage(self, reply: str) -> None:
        """Fold one LLM generation into the call's usage counters.

        Provider-reported streaming usage is the source of truth; when a
        provider doesn't report it, the documented fallback estimates output
        tokens at ~4 chars/token and flags the call as estimated.
        """
        usage = self._recorder.usage
        usage["llm_requests"] = usage.get("llm_requests", 0) + 1
        reported = getattr(self._llm, "last_stream_usage", None)
        if reported is not None:
            usage["llm_input_tokens"] += reported.input_tokens
            usage["llm_output_tokens"] += reported.output_tokens
            usage["llm_cached_tokens"] = (
                usage.get("llm_cached_tokens", 0) + reported.cached_tokens
            )
        elif reply:
            usage["llm_output_tokens"] += len(reply) // 4
            usage["llm_usage_estimated"] = 1

    async def _stream_llm_tokens(
        self, reply_parts: list[str], system: str, started: float
    ) -> float | None:
        """Stream LLM tokens downstream with pause-flush hints and retry.

        Retries (bounded by the configured retry policy) only when the stream
        fails before the first token — a mid-reply retry would repeat audio.
        """
        first_token_ms: float | None = None
        attempts = 0
        while True:
            attempts += 1
            try:
                stream = self._llm.stream(
                    self._history,
                    system=system,
                    temperature=self._llm_temperature,
                    max_tokens=self._llm_max_tokens,
                ).__aiter__()
                pending = asyncio.ensure_future(anext(stream))
                hinted = False
                while True:
                    done, _ = await asyncio.wait(
                        {pending}, timeout=_LLM_PAUSE_FLUSH_SECONDS
                    )
                    if not done:
                        # LLM paused mid-reply: nudge buffered text into TTS once
                        # per stall so speech starts without the next boundary.
                        if reply_parts and not hinted:
                            hinted = True
                            await self.push_frame(TTSFlushHintFrame())
                        continue
                    try:
                        token = pending.result()
                    except StopAsyncIteration:
                        return first_token_ms
                    hinted = False
                    if first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    reply_parts.append(token)
                    await self.push_frame(TextFrame(token))
                    pending = asyncio.ensure_future(anext(stream))
            except asyncio.CancelledError:
                if "pending" in locals() and not pending.done():
                    pending.cancel()
                raise
            except ProviderError as exc:
                if reply_parts or attempts > self._llm_max_retries:
                    raise
                logger.warning("llm stream failed before first token (%s); retrying", exc.category)
                await asyncio.sleep(0.2 * attempts)

    async def _say(self, text: str) -> None:
        """Speak a fixed phrase through the TTS path."""
        self._last_bot_reply = text
        self._history.append({"role": "assistant", "content": text})
        self._recorder.add_turn(TurnRecord(role="bot", text=text))
        await self._notify_client({"type": "bot_text", "text": text})
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(TextFrame(text))
        await self.push_frame(LLMFullResponseEndFrame())

    async def _open_session(self) -> None:
        """Announce the session parameters to the client, then greet.

        The session_config message MUST precede any audio: the browser client
        uses it to build its playback pipeline at the rate the worker actually
        streams (a hardcoded client rate plays 16 kHz audio at 24 kHz — fast,
        pitch-shifted and full of scheduling gaps).
        """
        if self._client_info:
            await self._notify_client({"type": "session_config", **self._client_info})
        await self._say(self._config.greeting)

    async def speak_greeting(self) -> None:
        if not self._pipeline_started:
            self._pending_greeting = True
            return
        await self._open_session()
