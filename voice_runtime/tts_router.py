"""Streaming TTS router: per-language engines, barge-in, fallback.

A single Pipecat ``TTSService`` that owns every WebSocket TTS engine of a
call. Responsibilities:

- sentence-aware buffering of LLM tokens (``VoiceSentenceAggregator``) with
  sanitization before text reaches a provider;
- one persistent provider connection per (provider, model, voice), reused
  across turns — never one connection per sentence;
- per-language engine selection from the bot's language→voice mapping,
  switched by ``SwitchVoiceLanguageFrame`` without unnecessary reconnects;
- generation IDs == Pipecat audio-context IDs: audio for cancelled or unknown
  generations is rejected both provider-side and here;
- barge-in: Pipecat's interruption calls ``on_audio_context_interrupted``,
  which cancels the provider generation (ElevenLabs closes the server
  context, Sarvam drops the connection) and discards queued audio;
- fallback to the configured secondary engine ONLY for transient failures
  (timeout / rate limit / upstream errors / connection reset). Auth and
  invalid-configuration errors surface immediately and never fall back.
  The engine that actually produced audio is recorded per generation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field as dc_field

from pipecat.frames.frames import (
    AggregatedTextFrame,
    EndWorkerFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from shared.audio.pcm import resample_pcm, silence_pcm
from shared.audio.text import has_speakable_text, sanitize_for_tts
from shared.orchestration.placeholders import sanitize_spoken_text
from shared.config import get_settings
from shared.providers.base import ProviderError
from shared.providers.tts.delivery import (
    apply_delivery_params,
    delivery_capabilities,
    provider_speed,
)
from shared.orchestration.voice_identity import resolve_language_engine
from shared.providers.tts.elevenlabs_ws import ElevenLabsWebSocketTTSProvider
from shared.providers.tts.sarvam_ws import SarvamWebSocketTTSProvider
from shared.providers.tts.streaming import (
    TRANSIENT_ERROR_CATEGORIES,
    StreamingTTSProvider,
    TTSStreamEvent,
    TTSStreamSettings,
)
from voice_runtime.aggregator import VoiceSentenceAggregator
from voice_runtime.frames import SwitchVoiceLanguageFrame, TTSFlushHintFrame

logger = logging.getLogger(__name__)

_STREAMING_PROVIDERS: dict[str, type[StreamingTTSProvider]] = {
    "sarvam": SarvamWebSocketTTSProvider,
    "elevenlabs": ElevenLabsWebSocketTTSProvider,
}

# Sample rates each provider can emit natively; anything else is resampled.
_SUPPORTED_RATES = {
    "sarvam": {8000, 16000, 22050, 24000},
    "elevenlabs": {8000, 16000, 22050, 24000},
}

# Healthy warm TTFB is 35-700ms; a dialer gives up on prolonged dead air
# (observed live 2026-07-29: socket closed after ~12.7s of silence). 4s leaves
# room for a cold reconnect while still triggering fallback well before the
# dialer hangs up.
_FIRST_AUDIO_TIMEOUT_S = 4.0

# Failure categories that no retry, fallback or later turn can heal: the
# credentials or the engine configuration are wrong. Leaving the call up
# after one of these means the caller sits in dead air until they hang up
# (observed live 2026-07-29: corrupted Sarvam key → 403 on every sentence →
# 12.7 s of silence → dialer closed the socket). The call is ended cleanly
# instead.
#
# ``invalid_input`` is fatal ONLY while the call has produced no audio at all
# (a genuine configuration error fails from the first sentence). Once the
# engine has demonstrably spoken, the same category means the provider
# rejected ONE payload (observed live 2026-08-12: Sarvam 422 "Text must
# contain at least one character from the allowed languages" on an orphan
# punctuation fragment) — ending the call for that dropped healthy calls
# after one or two turns. The failed sentence is skipped instead.
_FATAL_ERROR_CATEGORIES = frozenset({"auth", "invalid_input"})

# In-reply breath (planner-gated, pause mode): a short INHALE clip that
# rises into the sentence it precedes — the pre-reply exhale-shaped clip,
# trimmed, read as a cut noise between two sentences (live feedback
# 2026-09-03). Only a brief beat separates it from the sentence, the way a
# person starts talking on the top of a breath.
_SENTENCE_BREATH_KIND = "inhale"
_SENTENCE_BREATH_BEAT_MS = 60


def is_streaming_tts_provider(provider: str) -> bool:
    return provider in _STREAMING_PROVIDERS


@dataclass
class _Sentence:
    """One aggregated sentence queued for pause-aware dispatch.

    ``pause_after`` is False for mid-sentence flush-hint fragments — the gap
    between a fragment and its continuation is not a sentence boundary.

    ``pause_after_ms``/``speed_scale`` are optional naturalness-planner
    overrides: a per-sentence silence duration (None → the router's
    configured pause_ms) and a subtle multiplier on the bot's base speed
    (applied only on providers that support per-sub-generation rate)."""

    text: str
    pause_after: bool = True
    pause_after_ms: int | None = None
    speed_scale: float | None = None
    emphasis: str = "none"
    pitch_scale: float | None = None
    energy_scale: float | None = None
    question_style: bool = False
    speech_style: str = "neutral"
    phrase_boundaries: tuple[int, ...] = ()
    critical: bool = False
    critical_reason: str = ""
    # One soft breath in the gap before this sentence (planner-decided).
    breath_before: bool = False


@dataclass
class _Generation:
    """Book-keeping for one bot reply (== audio context)."""

    engine: dict
    provider: StreamingTTSProvider
    texts: list[str] = dc_field(default_factory=list)
    flushed: bool = False
    got_audio: bool = False
    fallback_used: bool = False
    watchdog: asyncio.Task | None = None
    audio_bytes: int = 0   # post-resample bytes delivered downstream
    audio_chunks: int = 0
    # Characters actually SENT to the current engine — the provider's billing
    # basis (both Sarvam and ElevenLabs charge for submitted text, played or
    # not). Reset when a fallback switches engines, so exactly one engine is
    # ever billed for a given sentence; pause-mode sentences still waiting in
    # `pending` are not included until they are dispatched.
    dispatched_chars: int = 0
    # Pause-aware sentence serialization (active when pause_ms > 0): sentences
    # wait here until the provider confirms the previous one finished, so
    # inserted silence always lands BETWEEN complete sentence audio segments.
    pending: deque = dc_field(default_factory=deque)
    active: str | None = None            # provider sub-generation id in flight
    active_pause_after: bool = True
    active_pause_after_ms: int | None = None
    active_got_audio: bool = False
    # A sentence finished audibly and the next one owes a leading pause. The
    # gap is materialized right before the NEXT dispatch, so a turn that ends
    # here never gets trailing silence. ``gap_ms`` carries the planned
    # per-sentence duration (None → router default).
    gap_pending: bool = False
    gap_ms: int | None = None
    turn_complete: bool = False
    # A provider "final" arrived while the LLM was still streaming the turn
    # (Sarvam emits one per flush, so a flush-hint produces one). It is
    # ignored, but remembered: if no more text follows, the end-of-turn
    # close-out must not wait for a second final that will never come.
    midturn_final_seen: bool = False
    seq: int = 0
    # In-reply breaths materialized so far (planner caps them per turn).
    breaths: int = 0


class StreamingTTSRouter(TTSService):
    def __init__(
        self,
        *,
        tts_config: dict,
        language: str,
        speed: float = 1.0,
        pause_ms: int = 0,
        energy: int | None = None,
        sample_rate: int = 24000,
        recorder=None,
        provider_factory=None,
        latency=None,
        naturalness=None,
        filler_library=None,
        first_audio_timeout: float = _FIRST_AUDIO_TIMEOUT_S,
        **kwargs,
    ):
        """``tts_config`` is ``ResolvedBotConfig.tts`` (see shared/bot_config.py)."""
        super().__init__(
            push_text_frames=True,
            push_stop_frames=True,
            push_start_frame=True,
            pause_frame_processing=True,
            sample_rate=sample_rate,
            settings=TTSSettings(
                model=tts_config.get("model") or None,
                voice=tts_config.get("voice") or None,
                language=None,
            ),
            **kwargs,
        )
        # Realtime flush rules on top of sentence aggregation.
        self._text_aggregator = VoiceSentenceAggregator(
            aggregation_type=self._text_aggregation_mode
        )
        self.add_text_transformer(self._sanitize_transform)

        self._tts_config = tts_config
        self._default_engine = {
            "provider": tts_config.get("provider") or "sarvam",
            "model": tts_config.get("model") or "",
            "voice": tts_config.get("voice") or "",
            "voice_gender": tts_config.get("voice_gender") or "neutral",
            "params": {},
            "api_key_reference": tts_config.get("api_key_reference") or "",
        }
        self._language_map: dict[str, dict] = tts_config.get("language_map") or {}
        self._fallback_engine: dict | None = tts_config.get("fallback") or None
        self._base_params: dict = tts_config.get("settings") or {}
        self._current_language = language
        self._speed = speed
        self._pause_ms = max(0, int(pause_ms or 0))
        self._energy = energy
        self._recorder = recorder
        self._provider_factory = provider_factory or self._default_provider_factory
        # Shared per-call TurnLatencyTracker (optional): the router is the only
        # place that sees when synthesis was actually requested and when the
        # provider's first byte came back.
        self._latency = latency
        # Optional shared SpeechNaturalnessPlanner: per-sentence pause and
        # subtle rate variation in pause mode. None → legacy fixed delivery.
        self._naturalness = naturalness
        # Optional voice_runtime.latency_filler.FillerClipLibrary: the source
        # of the rare in-reply breath (pause mode, planner-gated), matched to
        # the engine's catalog voice gender.
        self._filler_library = filler_library
        self._first_audio_timeout = first_audio_timeout

        self._providers: dict[tuple, StreamingTTSProvider] = {}
        self._pumps: dict[tuple, asyncio.Task] = {}
        self._generations: dict[str, _Generation] = {}
        # Pause mode: provider sub-generation id → pipecat audio-context id.
        self._subgenerations: dict[str, str] = {}
        # True while a flush-hint fragment is being pushed (see _Sentence).
        self._mid_turn_flush = False
        self._fatal_call_ended = False
        # Whether ANY engine has delivered audio this call — the discriminator
        # between a configuration-level invalid_input (nothing can ever
        # render: end the call) and a per-payload rejection (skip and go on).
        self._call_audio_delivered = False
        # Consecutive invalid_input failures with no audio in between: one is
        # a payload rejection; a second in a row means the engine broke
        # mid-call (e.g. a language switch onto a bad config) — every further
        # reply would fail too, so the dead-air protection applies again.
        self._invalid_input_streak = 0

    async def start(self, frame: StartFrame):
        """Open the provider connection before the first word needs speaking.

        Measured against Sarvam bulbul:v3: ~715ms to first audio on a cold
        WebSocket versus ~35ms once it is up. Left lazy, that handshake lands
        inside the greeting of every single call — the one turn where the
        caller is already waiting through ring and pickup. Connecting here
        moves it into pipeline startup, which overlaps call setup.

        Best-effort by construction: a failure is logged and left to the
        normal synthesis path, which has fallback and retry. Warm-up must
        never be able to stop a call from starting.
        """
        await super().start(frame)
        try:
            engine = self._engine_for_language(self._current_language)
            await self._get_provider(engine, self._current_language)
        except Exception:  # noqa: BLE001 — never block call start on a warm-up
            logger.warning("tts-router: connection warm-up failed", exc_info=True)

    def can_generate_metrics(self) -> bool:
        return True

    # ── text sanitization ───────────────────────────────────────────────
    async def _sanitize_transform(self, text: str, aggregation_type) -> str:
        return sanitize_for_tts(text)

    # ── engine / provider management ────────────────────────────────────
    def _engine_for_language(self, locale: str) -> dict:
        return resolve_language_engine(
            self._language_map, locale, self._default_engine
        )

    def _engine_key(self, engine: dict) -> tuple:
        return (engine.get("provider"), engine.get("model"), engine.get("voice"))

    def _stream_settings(
        self, engine: dict, locale: str, *, speed: float | None = None
    ) -> TTSStreamSettings:
        provider = engine.get("provider") or "sarvam"
        model = engine.get("model") or ""
        # The bot's saved tts_settings describe the DEFAULT engine's
        # provider/model and were validated against that model's schema only.
        # A per-language override or the fallback engine may run a different
        # provider/model, whose parameters have different names and ranges, so
        # the base settings apply solely to engines matching the default
        # selection — an override carries its own validated params. Without
        # this, bulbul:v2 loudness or eleven_flash speed would ride along to an
        # engine that never accepts them.
        inherits_base = (
            provider == self._default_engine.get("provider")
            and model == (self._default_engine.get("model") or "")
        )
        base = self._base_params if inherits_base else {}
        params = {**base, **(engine.get("params") or {})}
        # Canonical Delivery tuning: speed OVERRIDES any legacy pace/speed left
        # in stored settings; Energy fills only fields the operator left unset
        # and only ones the model documents (shared.providers.tts.delivery).
        params = apply_delivery_params(
            provider, model, params,
            speed=self._speed if speed is None else speed, energy=self._energy,
        )
        supported = _SUPPORTED_RATES.get(provider, {self.sample_rate})
        rate = self.sample_rate if self.sample_rate in supported else max(supported)
        reference = engine.get("api_key_reference") or ""
        api_key = get_settings().resolve_secret(reference)
        if not api_key:
            raise ProviderError(
                provider, "auth",
                f"TTS provider '{provider}' credentials are not configured",
            )
        return TTSStreamSettings(
            provider=provider,
            model=model,
            voice=engine.get("voice") or "",
            language=locale,
            sample_rate=rate,
            codec="linear16" if provider == "sarvam" else "pcm",
            params=params,
            api_key=api_key,
            timeout_seconds=15.0,
        )

    def _default_provider_factory(self, settings: TTSStreamSettings) -> StreamingTTSProvider:
        cls = _STREAMING_PROVIDERS.get(settings.provider)
        if cls is None:
            raise ProviderError(
                settings.provider, "invalid_input",
                f"'{settings.provider}' is not a streaming TTS provider",
            )
        return cls(settings)

    async def _get_provider(self, engine: dict, locale: str) -> StreamingTTSProvider:
        key = self._engine_key(engine)
        provider = self._providers.get(key)
        stream_settings = self._stream_settings(engine, locale)
        if provider is None:
            provider = self._provider_factory(stream_settings)
            self._providers[key] = provider
            self._pumps[key] = self.create_task(self._pump_events(key, provider))
        else:
            await provider.configure(stream_settings)
        return provider

    async def _warm_engine(self, engine: dict, locale: str) -> None:
        """Best-effort background warm-up of a not-yet-used engine: failures
        are logged and left to the synthesis path, which has retry/fallback."""
        try:
            provider = await self._get_provider(engine, locale)
            await provider.connect()
        except Exception:  # noqa: BLE001 — warm-up must never affect the call
            logger.warning(
                "tts-router: language-switch warm-up failed", exc_info=True
            )

    def _flush_event_background(self, kind: str, **data) -> None:
        """Persist a recorder event without blocking the caller: an awaited
        Mongo write here would stall the single event-pump consumer for the
        duration of a network round-trip. flush_event appends to the
        in-memory event list before persisting, and tasks run in creation
        order, so event ordering is preserved."""
        coro = self._recorder.flush_event(kind, **data)
        if getattr(self, "_task_manager", None) is not None:
            self.create_task(coro)
        else:
            # Not wired into a pipeline (bare-router tests / teardown edge).
            asyncio.create_task(coro)

    # ── frame handling ──────────────────────────────────────────────────
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, SwitchVoiceLanguageFrame):
            if frame.language and frame.language != self._current_language:
                engine = self._engine_for_language(frame.language)
                logger.info(
                    "tts-router: switching language %s → %s (provider=%s voice=%s)",
                    self._current_language, frame.language,
                    engine.get("provider"), engine.get("voice"),
                )
                self._current_language = frame.language
                if self._engine_key(engine) not in self._providers:
                    # Warm the newly-mapped engine before its first reply
                    # needs it — same best-effort contract as start().
                    self.create_task(self._warm_engine(engine, frame.language))
                if self._recorder is not None:
                    self._flush_event_background(
                        "tts_language_switched",
                        language=frame.language,
                        provider=engine.get("provider"),
                        voice=engine.get("voice"),
                    )
            return
        if isinstance(frame, TTSFlushHintFrame):
            await self._handle_flush_hint()
            return
        await super().process_frame(frame, direction)

    async def _handle_flush_hint(self):
        """Mid-turn flush: release buffered text and push it to the provider."""
        remaining = await self._text_aggregator.flush()
        if remaining and remaining.text.strip():
            # The fragment ends mid-sentence: its continuation must not get a
            # sentence pause in front of it.
            self._mid_turn_flush = True
            try:
                await self._push_tts_frames(
                    AggregatedTextFrame(
                        remaining.text, remaining.type, raw_text=remaining.text
                    )
                )
            finally:
                self._mid_turn_flush = False
        context_id = self._turn_context_id
        if context_id and self.audio_context_available(context_id):
            await self.flush_audio(context_id)

    def _log_engine_failure(self, engine: dict | None, error: ProviderError, stage: str):
        """One structured line per unrecovered TTS failure — enough to place
        the failure (engine + language + category) without leaking secrets."""
        engine = engine or {}
        logger.error(
            "tts-router: %s failed (provider=%s model=%s voice=%s language=%s "
            "category=%s): %s",
            stage, engine.get("provider"), engine.get("model"), engine.get("voice"),
            self._current_language, error.category, str(error)[:300],
        )

    # ── TTSService contract ─────────────────────────────────────────────
    async def run_tts(self, text: str, context_id: str):
        if self._latency is not None:
            self._latency.mark_tts_request()
        logger.info(
            "tts[%s] generating %d chars (lang=%s, context=%s)",
            self._recorder.session_id if self._recorder else "?",
            len(text), self._current_language, str(context_id)[:8],
        )
        guarded = sanitize_spoken_text(text)
        if guarded != text:
            # Last line of defense: an unresolved template placeholder
            # ({customer_name}, [aapka naam]) survived every upstream layer.
            # It is stripped here — raw placeholder text is NEVER spoken.
            logger.warning(
                "tts[%s] stripped unresolved placeholder text before "
                "synthesis (context=%s)",
                self._recorder.session_id if self._recorder else "?",
                str(context_id)[:8],
            )
            if self._recorder is not None:
                self._recorder.add_event(
                    "tts_placeholder_stripped", chars=len(text) - len(guarded),
                )
            text = guarded
        if not has_speakable_text(text):
            # An orphan punctuation/emoji-only fragment (e.g. a held "." the
            # aggregator released at end of turn). Sarvam rejects these with a
            # 422 error AND closes the socket — never dispatch them.
            logger.info(
                "tts[%s] skipping unspeakable segment (context=%s, %d chars)",
                self._recorder.session_id if self._recorder else "?",
                str(context_id)[:8], len(text),
            )
            if self._recorder is not None:
                self._recorder.add_event(
                    "tts_segment_skipped", reason="no_speakable_text",
                    chars=len(text),
                )
            yield None
            return
        state = self._generations.get(context_id)
        engine: dict | None = state.engine if state else None
        try:
            if state is None:
                engine = self._engine_for_language(self._current_language)
                provider = await self._get_provider(engine, self._current_language)
                state = _Generation(engine=engine, provider=provider)
                self._generations[context_id] = state
            state.texts.append(text)
            delivery = None
            planning_ms = 0.0
            if self._naturalness is not None and not self._mid_turn_flush:
                planned_at = time.perf_counter()
                delivery = self._naturalness.plan_segment(
                    text, base_pause_ms=self._pause_ms,
                    language=self._current_language,
                    first_in_turn=len(state.texts) == 1,
                    breaths_so_far=state.breaths,
                )
                if delivery.breath_before and self._filler_library is not None:
                    state.breaths += 1
                planning_ms = (time.perf_counter() - planned_at) * 1000.0
            if self._pause_ms > 0:
                # Pause mode: sentences are serialized so the provider's
                # per-sentence completion marks where silence is inserted.
                sentence = _Sentence(text=text, pause_after=not self._mid_turn_flush)
                if delivery is not None:
                    sentence.pause_after_ms = delivery.pause_after_ms
                    sentence.speed_scale = delivery.speed_scale
                    sentence.emphasis = delivery.emphasis
                    sentence.pitch_scale = delivery.pitch_scale
                    sentence.energy_scale = delivery.energy_scale
                    sentence.question_style = delivery.question_style
                    sentence.speech_style = delivery.speech_style
                    sentence.phrase_boundaries = delivery.phrase_boundaries
                    sentence.critical = delivery.critical
                    sentence.critical_reason = delivery.critical_reason
                    sentence.breath_before = (
                        delivery.breath_before and self._filler_library is not None
                    )
            if delivery is not None:
                provider_name = state.engine.get("provider") or ""
                model = state.engine.get("model") or ""
                capabilities = delivery_capabilities(
                    provider_name, model, streaming=True
                )
                applied_scale = (
                    delivery.speed_scale
                    if self._pause_ms > 0 and capabilities.per_segment_rate
                    else None
                )
                words = len((text or "").split())
                logger.info("naturalness_segment %s", json.dumps({
                    "session": self._recorder.session_id if self._recorder else "?",
                    "context": str(context_id)[:8],
                    "provider": provider_name,
                    "model": model,
                    "human_speech_enabled": bool(
                        getattr(self._naturalness, "enabled", False)
                    ),
                    "language": self._current_language,
                    "character_count": len(text),
                    "word_count": words,
                    "segment_type": (
                        "critical" if delivery.critical
                        else "question" if delivery.question_style
                        else "short" if words <= 3 else "statement"
                    ),
                    "critical_content": delivery.critical,
                    "critical_reason": delivery.critical_reason,
                    "planned_pause_ms": (
                        delivery.pause_after_ms
                        if delivery.pause_after_ms is not None
                        else self._pause_ms
                    ),
                    "speed_scale": applied_scale,
                    "speaking_rate": (
                        provider_speed(provider_name, model, self._speed * applied_scale)
                        if applied_scale is not None else None
                    ),
                    "speech_style": delivery.speech_style,
                    "emphasis": delivery.emphasis,
                    "pitch_scale": (
                        delivery.pitch_scale if capabilities.pitch else None
                    ),
                    "energy_scale": (
                        delivery.energy_scale if capabilities.energy else None
                    ),
                    "question_style": (
                        delivery.question_style if capabilities.question_style else False
                    ),
                    "phrase_boundary_count": len(delivery.phrase_boundaries),
                    "breath_before": bool(
                        delivery.breath_before and self._filler_library is not None
                    ),
                    "naturalness_processing_ms": round(planning_ms, 3),
                }, ensure_ascii=False))
            if self._pause_ms > 0:
                state.pending.append(sentence)
                if state.active is None:
                    await self._dispatch_next_sentence(context_id, state)
            else:
                await state.provider.synthesize_stream(text, generation_id=context_id)
                state.dispatched_chars += len(text)
                # New text after an ignored flush-hint final: the provider
                # owes this generation a fresh final again.
                state.midturn_final_seen = False
        except ProviderError as exc:
            handled = await self._try_fallback(context_id, exc)
            if not handled:
                self._log_engine_failure(engine, exc, "synthesis dispatch")
                yield ErrorFrame(error=f"tts_failure:{exc.category}")
                await self._finalize_generation(context_id, failed=True)
                await self._maybe_end_call_fatal(exc.category)
                return
        except (ConnectionError, OSError, TimeoutError) as exc:
            error = ProviderError("tts", "upstream", str(exc)[:200])
            handled = await self._try_fallback(context_id, error)
            if not handled:
                self._log_engine_failure(engine, error, "synthesis dispatch")
                yield ErrorFrame(error="tts_failure:upstream")
                await self._finalize_generation(context_id, failed=True)
                return
        yield None

    async def flush_audio(self, context_id: str | None = None):
        flush_id = context_id or self.get_active_audio_context_id()
        state = self._generations.get(flush_id) if flush_id else None
        if state is None:
            return
        state.flushed = True
        if self._pause_ms <= 0:
            # Pause mode flushes per sentence at dispatch — an extra turn-level
            # flush would fabricate a spurious burst/final on Sarvam.
            try:
                await state.provider.flush(flush_id)
            except (ConnectionError, OSError):
                pass
        if not state.got_audio and state.watchdog is None:
            state.watchdog = self.create_task(self._first_audio_watchdog(flush_id))

    async def on_turn_context_completed(self):
        context_id = self._turn_context_id
        state = self._generations.get(context_id) if context_id else None
        if state is not None:
            # Marked BEFORE super() runs the end-of-turn flush, so the
            # provider final that flush produces is attributed to a completed
            # turn — never mistaken for a mid-turn flush-hint final.
            state.turn_complete = True
        await super().on_turn_context_completed()
        if state is None:
            return
        if self._pause_ms > 0:
            if state.active is None and not state.pending:
                # The last sentence's final already arrived — close out now.
                await self._finalize_generation(context_id, failed=False)
            return
        if state.midturn_final_seen:
            # No text followed the ignored flush-hint final, so the provider
            # owes no further final for this generation — close out directly.
            await self._finalize_generation(context_id, failed=False)
            return
        try:
            await state.provider.finish(context_id)
        except (ConnectionError, OSError):
            pass

    def _bill_generation(self, state: _Generation, *, interrupted: bool = False) -> None:
        """Record the generation's dispatched characters as billable usage.

        Both Sarvam and ElevenLabs charge for text submitted for synthesis
        whether or not the caller heard all of it, so an interrupted
        generation bills exactly what was already sent — never the sentences
        still queued locally. Idempotent per generation by construction: the
        state is popped from ``_generations`` before/with every billing site.
        """
        if self._recorder is None or state.dispatched_chars <= 0:
            return
        add_usage = getattr(self._recorder, "add_tts_usage", None)
        if add_usage is None:
            return
        add_usage(
            provider=state.engine.get("provider") or "",
            model=state.engine.get("model") or "",
            voice=state.engine.get("voice") or "",
            characters=state.dispatched_chars,
        )
        state.dispatched_chars = 0
        if interrupted:
            self._recorder.add_event(
                "tts_generation_interrupted",
                provider=state.engine.get("provider"),
                voice=state.engine.get("voice"),
            )

    async def on_audio_context_interrupted(self, context_id: str):
        """Barge-in: cancel provider synthesis and drop the generation.

        Queued sentences and any not-yet-played silence die with the audio
        context — a cancelled generation can never emit late audio or pauses.
        Text already sent to the provider was synthesized (and is billed by
        the provider) regardless of the cancel, so it is still counted.
        """
        state = self._generations.pop(context_id, None)
        self._drop_subgenerations(context_id)
        if state is not None:
            if state.watchdog is not None:
                state.watchdog.cancel()
            state.pending.clear()
            self._bill_generation(state, interrupted=True)
            try:
                await state.provider.cancel(state.active or context_id)
            except (ConnectionError, OSError):
                pass
        await super().on_audio_context_interrupted(context_id)

    async def stop(self, frame):
        await super().stop(frame)
        await self._shutdown_providers()

    async def cancel(self, frame):
        await super().cancel(frame)
        await self._shutdown_providers()

    async def _shutdown_providers(self):
        for state in self._generations.values():
            if state.watchdog is not None:
                state.watchdog.cancel()
            # Call teardown with a generation still in flight (e.g. hang-up
            # mid-goodbye): the dispatched text was synthesized and is billed
            # by the provider — count it before the state is dropped.
            self._bill_generation(state, interrupted=True)
        self._generations.clear()
        self._subgenerations.clear()
        pumps, self._pumps = self._pumps, {}
        for task in pumps.values():
            await self.cancel_task(task)
        providers, self._providers = self._providers, {}
        for provider in providers.values():
            try:
                await provider.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    # ── provider event pump ─────────────────────────────────────────────
    async def _pump_events(self, key: tuple, provider: StreamingTTSProvider):
        while True:
            event = await provider.events.get()
            try:
                await self._dispatch_event(key, event)
            except Exception:  # noqa: BLE001 — one bad event must not kill the pump
                logger.exception("tts-router: event dispatch failed")

    async def _dispatch_event(self, key: tuple, event: TTSStreamEvent):
        generation = event.generation_id
        # Pause mode dispatches per-sentence sub-generations; events map back
        # to the owning pipecat audio context here.
        context_id = self._subgenerations.get(generation, generation) if generation else None
        state = self._generations.get(context_id) if context_id else None

        if event.kind == "audio":
            # Reject late audio: unknown/cancelled generations are dropped.
            if state is None or not self.audio_context_available(context_id):
                return
            if self._pause_ms > 0 and generation != state.active:
                return  # audio from a completed/replaced sentence dispatch
            audio = event.audio
            provider_rate = state.provider.settings.sample_rate
            state.active_got_audio = True
            self._call_audio_delivered = True
            self._invalid_input_streak = 0
            if not state.got_audio:
                state.got_audio = True
                # Stamped before the resample/queue below so tts_ttfb is the
                # provider's own time and `playout` isolates ours.
                if self._latency is not None:
                    self._latency.mark_tts_first_byte()
                await self.stop_ttfb_metrics()
                if state.watchdog is not None:
                    state.watchdog.cancel()
                    state.watchdog = None
                # Format trace: what the provider was asked for vs what the
                # pipeline emits. A wrong-speed complaint starts here.
                logger.info(
                    "tts[%s] first audio (context=%s): provider=%s "
                    "provider_rate=%d router_rate=%d resample=%s "
                    "chunk_bytes=%d (mono s16le)",
                    self._recorder.session_id if self._recorder else "?",
                    str(context_id)[:8], state.engine.get("provider"),
                    provider_rate, self.sample_rate,
                    provider_rate != self.sample_rate, len(audio),
                )
            if provider_rate != self.sample_rate:
                audio = resample_pcm(audio, provider_rate, self.sample_rate)
            state.audio_bytes += len(audio)
            state.audio_chunks += 1
            await self.append_to_audio_context(
                context_id,
                TTSAudioRawFrame(audio, self.sample_rate, 1, context_id=context_id),
            )
        elif event.kind == "final":
            if state is None:
                return
            if self._pause_ms > 0:
                await self._on_sentence_final(context_id, generation, state)
            elif state.turn_complete:
                await self._finalize_generation(context_id, failed=False)
            else:
                # A flush-hint flush produced this final while the LLM is
                # still streaming the turn: finalizing here would pop the
                # generation and reject all subsequent audio. The generation
                # stays open and synthesis continues on it.
                state.midturn_final_seen = True
                logger.debug(
                    "tts-router: ignoring mid-turn provider final (context=%s)",
                    str(context_id)[:8],
                )
        elif event.kind == "error":
            error = event.error or ProviderError("tts", "upstream", "unknown TTS error")
            if generation and state is not None:
                handled = await self._try_fallback(context_id, error)
                if not handled:
                    self._log_engine_failure(state.engine, error, "provider stream")
                    await self.push_error(error_msg=f"tts_failure:{error.category}")
                    await self._finalize_generation(context_id, failed=True)
                    await self._maybe_end_call_fatal(error.category)
            else:
                logger.warning("tts-router: provider error outside generation: %s", error)
        # "disconnected" events are informational — reconnects are lazy.

    # ── pause-aware sentence serialization (pause_ms > 0) ───────────────
    async def _dispatch_next_sentence(self, context_id: str, state: _Generation):
        """Send exactly one queued sentence as its own provider generation.

        flush+finish immediately so the provider emits a per-sentence final —
        the completion boundary at which _on_sentence_final inserts silence
        and releases the next sentence. Raises like synthesize_stream; callers
        handle fallback/error routing.
        """
        sentence: _Sentence = state.pending[0]
        await self._materialize_gap(context_id, state, breath_before=sentence.breath_before)
        state.pending.popleft()
        state.seq += 1
        sub_id = f"{context_id}~{state.seq}"
        self._subgenerations[sub_id] = context_id
        state.active = sub_id
        state.active_pause_after = sentence.pause_after
        state.active_pause_after_ms = sentence.pause_after_ms
        state.active_got_audio = False
        await self._apply_sentence_speed(state, sentence)
        await state.provider.synthesize_stream(sentence.text, generation_id=sub_id)
        state.dispatched_chars += len(sentence.text)
        await state.provider.flush(sub_id)
        await state.provider.finish(sub_id)
        if not state.got_audio and state.watchdog is None:
            # Pause mode flushes per sentence, so the turn-level flush that
            # normally arms the watchdog never runs — without this, a stalled
            # FIRST sentence had no first-audio timeout (no fallback, dead
            # air until the dialer gave up).
            state.watchdog = self.create_task(self._first_audio_watchdog(context_id))

    async def _apply_sentence_speed(self, state: _Generation, sentence: _Sentence):
        """Apply a planned per-sentence rate before dispatching it.

        Only on ElevenLabs WS: each pause-mode sub-generation opens its own
        context whose init carries voice_settings, so a reconfigure here is a
        pure settings swap (no reconnect, no flush). Sarvam is excluded — a
        config resend force-flushes the socket server-side, which would
        corrupt an in-flight turn. Neutral scales (≈1.0) skip the reconfigure.
        """
        provider_name = state.engine.get("provider") or ""
        model = state.engine.get("model") or ""
        capabilities = delivery_capabilities(provider_name, model, streaming=True)
        if (
            self._naturalness is None
            or not capabilities.per_segment_rate
        ):
            return
        scale = sentence.speed_scale if sentence.speed_scale is not None else 1.0
        # Reconfigure back to the base rate too, so a scaled sentence never
        # leaves its rate sticky on the ones that follow it.
        try:
            settings = self._stream_settings(
                state.engine, self._current_language, speed=self._speed * scale
            )
            await state.provider.configure(settings)
        except Exception:  # noqa: BLE001 — prosody is decoration, never fatal
            logger.debug("tts-router: per-sentence speed configure failed", exc_info=True)

    async def _materialize_gap(
        self, context_id: str, state: _Generation, *, breath_before: bool = False
    ):
        """Insert the owed inter-sentence silence right before a dispatch.

        Positioning is exact by construction: the previous sentence's audio
        has fully arrived (its final released the gap) and the next sentence
        has not been sent to the provider yet. When the planner asked for a
        breath before this sentence, it follows the planned pause — pause,
        soft breath, a short beat, then the sentence — as TTS audio of the
        same reply (this IS the bot speaking, unlike the pre-reply filler).
        """
        if not state.gap_pending:
            return
        gap_ms = state.gap_ms if state.gap_ms is not None else self._pause_ms
        state.gap_pending = False
        state.gap_ms = None
        if not self.audio_context_available(context_id):
            return
        pieces = [silence_pcm(self.sample_rate, gap_ms)]
        if breath_before and self._filler_library is not None:
            breath = self._filler_library.clip(
                state.engine.get("voice_gender") or "neutral", self.sample_rate,
                kind=_SENTENCE_BREATH_KIND,
            )
            if breath:
                pieces += [breath, silence_pcm(self.sample_rate, _SENTENCE_BREATH_BEAT_MS)]
                if self._recorder is not None:
                    self._recorder.add_event(
                        "sentence_breath_played",
                        context=str(context_id)[:8],
                        gender=state.engine.get("voice_gender") or "neutral",
                        breath_ms=round(len(breath) / (self.sample_rate * 2) * 1000, 1),
                    )
        for audio in pieces:
            if not audio:
                continue
            state.audio_bytes += len(audio)
            state.audio_chunks += 1
            await self.append_to_audio_context(
                context_id,
                TTSAudioRawFrame(audio, self.sample_rate, 1, context_id=context_id),
            )

    async def _on_sentence_final(self, context_id: str, sub_id: str, state: _Generation):
        """One sentence finished rendering: pause, continue, or close out."""
        self._subgenerations.pop(sub_id, None)
        if state.active != sub_id:
            return  # stale final from a replaced (fallback) dispatch
        state.active = None
        # Only an audibly-completed real sentence owes the next one a pause —
        # never a mid-sentence flush fragment, never a silent segment, and a
        # turn that ends here drops the flag (no trailing silence).
        if state.active_pause_after and state.active_got_audio:
            state.gap_pending = True
            state.gap_ms = state.active_pause_after_ms
        state.active_pause_after_ms = None
        if state.pending:
            try:
                await self._dispatch_next_sentence(context_id, state)
            except ProviderError as exc:
                await self._handle_sentence_dispatch_failure(context_id, state, exc)
            except (ConnectionError, OSError, TimeoutError) as exc:
                await self._handle_sentence_dispatch_failure(
                    context_id, state,
                    ProviderError("tts", "upstream", str(exc)[:200]),
                )
        elif state.turn_complete:
            await self._finalize_generation(context_id, failed=False)

    async def _handle_sentence_dispatch_failure(
        self, context_id: str, state: _Generation, error: ProviderError
    ):
        handled = await self._try_fallback(context_id, error)
        if not handled:
            self._log_engine_failure(state.engine, error, "sentence dispatch")
            await self.push_error(error_msg=f"tts_failure:{error.category}")
            await self._finalize_generation(context_id, failed=True)
            await self._maybe_end_call_fatal(error.category)

    def _drop_subgenerations(self, context_id: str) -> None:
        for sub_id in [s for s, ctx in self._subgenerations.items() if ctx == context_id]:
            self._subgenerations.pop(sub_id, None)

    async def _maybe_end_call_fatal(self, category: str) -> None:
        """End the call after an unrecoverable TTS configuration failure.

        Auth/invalid-config failures never fall back and cannot heal within
        the call — every further reply would fail the same way and the caller
        would only ever hear dead air. Ending the worker closes the media
        stream cleanly (telephony serializers emit their protocol `stop`).

        ``invalid_input`` is treated as configuration-level ONLY while no
        audio has ever been delivered this call. Once the engine has spoken,
        the same category is a per-payload rejection (e.g. Sarvam's 422 on an
        unspeakable fragment): the sentence is lost, the call must live on.
        """
        if category not in _FATAL_ERROR_CATEGORIES or self._fatal_call_ended:
            return
        if category == "invalid_input" and self._call_audio_delivered:
            self._invalid_input_streak += 1
            if self._invalid_input_streak < 2:
                logger.warning(
                    "tts[%s] provider rejected one payload (invalid_input) "
                    "after audio was already delivered — skipping the "
                    "segment, keeping the call alive",
                    self._recorder.session_id if self._recorder else "?",
                )
                if self._recorder is not None:
                    self._flush_event_background(
                        "tts_segment_rejected_by_provider", category=category,
                    )
                return
            # Second invalid_input in a row with no audio in between: the
            # engine itself broke mid-call — fall through to the fatal end.
        self._fatal_call_ended = True
        logger.error(
            "tts[%s] unrecoverable TTS failure (%s) — ending the call instead "
            "of leaving dead air",
            self._recorder.session_id if self._recorder else "?", category,
        )
        if self._recorder is not None:
            await self._recorder.flush_event("tts_fatal", category=category)
        await self.push_frame(EndWorkerFrame(reason=f"tts_failure:{category}"))

    async def _finalize_generation(self, context_id: str, *, failed: bool):
        state = self._generations.pop(context_id, None)
        self._drop_subgenerations(context_id)
        if state is None:
            return
        if state.watchdog is not None:
            state.watchdog.cancel()
        if not failed and state.audio_bytes:
            # bytes / (rate × 2 bytes/sample × 1 channel) = seconds of speech.
            duration_s = state.audio_bytes / (self.sample_rate * 2)
            logger.info(
                "tts[%s] generation %s complete: provider=%s chunks=%d "
                "bytes=%d duration=%.2fs rate=%d fallback=%s",
                self._recorder.session_id if self._recorder else "?",
                str(context_id)[:8], state.engine.get("provider"),
                state.audio_chunks, state.audio_bytes, duration_s,
                self.sample_rate, state.fallback_used,
            )
        if self._recorder is not None:
            # Billable characters: the text actually DISPATCHED to the engine
            # that owned the generation at the end (a fallback resets the
            # counter and replays, so exactly one engine is billed). Failed
            # generations are not billed — no audio was delivered and the
            # transient-failure categories are not charged by the providers.
            if not failed:
                self._bill_generation(state)
            self._flush_event_background(
                "tts_provider_used",
                provider=state.engine.get("provider"),
                voice=state.engine.get("voice"),
                fallback_used=state.fallback_used,
                failed=failed,
            )
        if self.audio_context_available(context_id):
            await self.append_to_audio_context(
                context_id, TTSStoppedFrame(context_id=context_id)
            )
            await self.remove_audio_context(context_id)

    async def _first_audio_watchdog(self, context_id: str):
        await asyncio.sleep(self._first_audio_timeout)
        state = self._generations.get(context_id)
        if state is None or state.got_audio:
            return
        error = ProviderError(
            state.engine.get("provider") or "tts", "timeout",
            "No audio received before the first-audio timeout",
        )
        handled = await self._try_fallback(context_id, error)
        if not handled:
            await self.push_error(error_msg="tts_failure:timeout")
            await self._finalize_generation(context_id, failed=True)

    # ── fallback ────────────────────────────────────────────────────────
    async def _try_fallback(self, context_id: str, error: ProviderError) -> bool:
        """Switch the generation to the fallback engine for transient failures.

        Never falls back for auth/config errors; never falls back twice.
        Returns True when the generation was successfully re-dispatched.
        """
        state = self._generations.get(context_id)
        if (
            state is None
            or state.fallback_used
            or self._fallback_engine is None
            or error.category not in TRANSIENT_ERROR_CATEGORIES
        ):
            return False

        logger.warning(
            "tts-router: %s failed (%s) — falling back to %s",
            state.engine.get("provider"), error.category,
            self._fallback_engine.get("provider"),
        )
        try:
            await state.provider.cancel(state.active or context_id)
        except (ConnectionError, OSError):
            pass
        if state.watchdog is not None:
            state.watchdog.cancel()
            state.watchdog = None
        # Sub-generations of the failed engine are dead — their late events
        # must not be attributed to the replayed generation.
        self._drop_subgenerations(context_id)

        try:
            provider = await self._get_provider(self._fallback_engine, self._current_language)
            state.engine = self._fallback_engine
            state.provider = provider
            state.fallback_used = True
            state.got_audio = False
            # The replay re-dispatches every sentence on the NEW engine; the
            # dispatch counter restarts so exactly one engine is billed.
            state.dispatched_chars = 0
            if self._pause_ms > 0:
                # Replay every sentence through the same pause-aware
                # serializer, so the fallback engine gets identical pauses.
                state.pending = deque(_Sentence(text=t) for t in state.texts)
                state.active = None
                state.gap_pending = False  # replay restarts — no leading gap
                await self._dispatch_next_sentence(context_id, state)
                if state.flushed and state.watchdog is None:
                    state.watchdog = self.create_task(
                        self._first_audio_watchdog(context_id)
                    )
            else:
                for text in state.texts:
                    await provider.synthesize_stream(text, generation_id=context_id)
                    state.dispatched_chars += len(text)
                state.midturn_final_seen = False
                if state.flushed:
                    await provider.flush(context_id)
                    await provider.finish(context_id)
                    state.watchdog = self.create_task(self._first_audio_watchdog(context_id))
        except (ProviderError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("tts-router: fallback dispatch failed: %s", exc)
            return False

        if self._recorder is not None:
            self._flush_event_background(
                "tts_fallback",
                from_provider=error.provider,
                to_provider=self._fallback_engine.get("provider"),
                category=error.category,
            )
        return True
