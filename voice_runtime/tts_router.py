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
import logging
from dataclasses import dataclass, field as dc_field

from pipecat.frames.frames import (
    AggregatedTextFrame,
    ErrorFrame,
    Frame,
    TTSAudioRawFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from shared.audio.pcm import resample_pcm
from shared.audio.text import sanitize_for_tts
from shared.config import get_settings
from shared.providers.base import ProviderError
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

_FIRST_AUDIO_TIMEOUT_S = 10.0


def is_streaming_tts_provider(provider: str) -> bool:
    return provider in _STREAMING_PROVIDERS


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


class StreamingTTSRouter(TTSService):
    def __init__(
        self,
        *,
        tts_config: dict,
        language: str,
        speed: float = 1.0,
        sample_rate: int = 24000,
        recorder=None,
        provider_factory=None,
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
            "params": {},
            "api_key_reference": tts_config.get("api_key_reference") or "",
        }
        self._language_map: dict[str, dict] = tts_config.get("language_map") or {}
        self._fallback_engine: dict | None = tts_config.get("fallback") or None
        self._base_params: dict = tts_config.get("settings") or {}
        self._current_language = language
        self._speed = speed
        self._recorder = recorder
        self._provider_factory = provider_factory or self._default_provider_factory
        self._first_audio_timeout = first_audio_timeout

        self._providers: dict[tuple, StreamingTTSProvider] = {}
        self._pumps: dict[tuple, asyncio.Task] = {}
        self._generations: dict[str, _Generation] = {}

    def can_generate_metrics(self) -> bool:
        return True

    # ── text sanitization ───────────────────────────────────────────────
    async def _sanitize_transform(self, text: str, aggregation_type) -> str:
        return sanitize_for_tts(text)

    # ── engine / provider management ────────────────────────────────────
    def _engine_for_language(self, locale: str) -> dict:
        engine = self._language_map.get(locale)
        if engine is None and "-" in locale:
            # e.g. "hi" mapping selected but transcript reported "hi-IN"
            engine = self._language_map.get(locale.split("-")[0])
        if engine is None:
            engine = self._default_engine
        return engine

    def _engine_key(self, engine: dict) -> tuple:
        return (engine.get("provider"), engine.get("model"), engine.get("voice"))

    def _stream_settings(self, engine: dict, locale: str) -> TTSStreamSettings:
        provider = engine.get("provider") or "sarvam"
        params = {**self._base_params, **(engine.get("params") or {})}
        if self._speed and self._speed != 1.0:
            speed_key = "pace" if provider == "sarvam" else "speed"
            params.setdefault(speed_key, self._speed)
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
            model=engine.get("model") or "",
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
                if self._recorder is not None:
                    await self._recorder.flush_event(
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
            await self._push_tts_frames(
                AggregatedTextFrame(
                    remaining.text, remaining.type, raw_text=remaining.text
                )
            )
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
        state = self._generations.get(context_id)
        engine: dict | None = state.engine if state else None
        try:
            if state is None:
                engine = self._engine_for_language(self._current_language)
                provider = await self._get_provider(engine, self._current_language)
                state = _Generation(engine=engine, provider=provider)
                self._generations[context_id] = state
            state.texts.append(text)
            await state.provider.synthesize_stream(text, generation_id=context_id)
        except ProviderError as exc:
            handled = await self._try_fallback(context_id, exc)
            if not handled:
                self._log_engine_failure(engine, exc, "synthesis dispatch")
                yield ErrorFrame(error=f"tts_failure:{exc.category}")
                await self._finalize_generation(context_id, failed=True)
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
        try:
            await state.provider.flush(flush_id)
        except (ConnectionError, OSError):
            pass
        if not state.got_audio and state.watchdog is None:
            state.watchdog = self.create_task(self._first_audio_watchdog(flush_id))

    async def on_turn_context_completed(self):
        context_id = self._turn_context_id
        await super().on_turn_context_completed()
        state = self._generations.get(context_id) if context_id else None
        if state is not None:
            try:
                await state.provider.finish(context_id)
            except (ConnectionError, OSError):
                pass

    async def on_audio_context_interrupted(self, context_id: str):
        """Barge-in: cancel provider synthesis and drop the generation."""
        state = self._generations.pop(context_id, None)
        if state is not None:
            if state.watchdog is not None:
                state.watchdog.cancel()
            try:
                await state.provider.cancel(context_id)
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
        self._generations.clear()
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
        state = self._generations.get(generation) if generation else None

        if event.kind == "audio":
            # Reject late audio: unknown/cancelled generations are dropped.
            if state is None or not self.audio_context_available(generation):
                return
            if not state.got_audio:
                state.got_audio = True
                await self.stop_ttfb_metrics()
                if state.watchdog is not None:
                    state.watchdog.cancel()
                    state.watchdog = None
            audio = event.audio
            provider_rate = state.provider.settings.sample_rate
            if provider_rate != self.sample_rate:
                audio = resample_pcm(audio, provider_rate, self.sample_rate)
            await self.append_to_audio_context(
                generation,
                TTSAudioRawFrame(audio, self.sample_rate, 1, context_id=generation),
            )
        elif event.kind == "final":
            if state is None:
                return
            await self._finalize_generation(generation, failed=False)
        elif event.kind == "error":
            error = event.error or ProviderError("tts", "upstream", "unknown TTS error")
            if generation and state is not None:
                handled = await self._try_fallback(generation, error)
                if not handled:
                    self._log_engine_failure(state.engine, error, "provider stream")
                    await self.push_error(error_msg=f"tts_failure:{error.category}")
                    await self._finalize_generation(generation, failed=True)
            else:
                logger.warning("tts-router: provider error outside generation: %s", error)
        # "disconnected" events are informational — reconnects are lazy.

    async def _finalize_generation(self, context_id: str, *, failed: bool):
        state = self._generations.pop(context_id, None)
        if state is None:
            return
        if state.watchdog is not None:
            state.watchdog.cancel()
        if self._recorder is not None:
            # Billable characters: counted once per generation against the
            # engine that actually delivered audio (fallback replays the same
            # texts on the new engine — never double-counted). Failed
            # generations produced no audio and are not billed.
            add_usage = getattr(self._recorder, "add_tts_usage", None)
            if add_usage is not None and not failed and state.texts:
                add_usage(
                    provider=state.engine.get("provider") or "",
                    model=state.engine.get("model") or "",
                    voice=state.engine.get("voice") or "",
                    characters=sum(len(t) for t in state.texts),
                )
            await self._recorder.flush_event(
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
            await state.provider.cancel(context_id)
        except (ConnectionError, OSError):
            pass
        if state.watchdog is not None:
            state.watchdog.cancel()
            state.watchdog = None

        try:
            provider = await self._get_provider(self._fallback_engine, self._current_language)
            state.engine = self._fallback_engine
            state.provider = provider
            state.fallback_used = True
            state.got_audio = False
            for text in state.texts:
                await provider.synthesize_stream(text, generation_id=context_id)
            if state.flushed:
                await provider.flush(context_id)
                await provider.finish(context_id)
                state.watchdog = self.create_task(self._first_audio_watchdog(context_id))
        except (ProviderError, ConnectionError, OSError, TimeoutError) as exc:
            logger.warning("tts-router: fallback dispatch failed: %s", exc)
            return False

        if self._recorder is not None:
            await self._recorder.flush_event(
                "tts_fallback",
                from_provider=error.provider,
                to_provider=self._fallback_engine.get("provider"),
                category=error.category,
            )
        return True
