"""Pipecat service wrappers around EchoSphere's provider layer.

These adapters let the same tenant/bot-selectable providers (shared.providers)
drive the realtime pipeline, so the voice runtime and the REST platform share
one provider implementation.
"""

import json
import logging
import time

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

from shared.providers.base import ProviderError, STTProvider, TTSProvider
from shared.providers.tts.delivery import delivery_capabilities, provider_speed
from shared.audio.pcm import resample_pcm, silence_pcm, wav_to_pcm
from shared.audio.text import has_speakable_text
from voice_runtime.frames import SwitchVoiceLanguageFrame

logger = logging.getLogger(__name__)


class EchoSTTService(SegmentedSTTService):
    """VAD-segmented STT backed by an EchoSphere STTProvider."""

    def __init__(self, provider: STTProvider, *, language: str | None = None,
                 recorder=None, **kwargs) -> None:
        from pipecat.services.settings import STTSettings

        super().__init__(settings=STTSettings(model=None, language=language), **kwargs)
        self._provider = provider
        self._language = language
        self._recorder = recorder

    async def run_stt(self, audio: bytes):
        """`audio` arrives as a WAV container (wants_wav_segments default)."""
        try:
            pcm, rate = wav_to_pcm(audio)
            audio_seconds = len(pcm) / (rate * 2) if rate else 0.0
            if self._recorder is not None and audio_seconds > 0:
                # Billable audio duration measured from the actual PCM16
                # payload — exactly the audio sent to the provider.
                add_usage = getattr(self._recorder, "add_stt_usage", None)
                if add_usage is not None:
                    add_usage(seconds=audio_seconds, basis="pcm")
                else:  # legacy/stub recorders
                    usage = self._recorder.usage
                    usage["stt_seconds"] = usage.get("stt_seconds", 0) + audio_seconds
                    usage["stt_requests"] = usage.get("stt_requests", 0) + 1
            result = await self._provider.transcribe(
                pcm, sample_rate=rate, language=self._language
            )
            if result.text:
                # Quality metadata rides on the frame so the transcript gate
                # can judge the segment (fields are provider-dependent; the
                # exact segment duration comes from the PCM itself).
                yield TranscriptionFrame(
                    result.text,
                    "caller",
                    time_now_iso8601(),
                    language=result.language,
                    result={
                        "provider": getattr(self._provider, "name", "stt"),
                        "language": result.language,
                        "confidence": result.confidence,
                        "language_probability": result.language_probability,
                        "no_speech_prob": result.no_speech_prob,
                        "audio_seconds": audio_seconds or None,
                    },
                )
        except ProviderError as exc:
            logger.warning("STT provider failure: %s", exc)
            yield ErrorFrame(error=f"stt_failure:{exc.category}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("STT unexpected failure")
            yield ErrorFrame(error="stt_failure:unexpected")


class EchoTTSService(TTSService):
    """Sentence-aggregated TTS backed by an EchoSphere TTSProvider."""

    def __init__(
        self,
        provider: TTSProvider,
        *,
        voice: str | None = None,
        language: str | None = None,
        speed: float = 1.0,
        pause_ms: int = 0,
        sample_rate: int | None = None,
        recorder=None,
        model: str | None = None,
        provider_name: str = "",
        naturalness=None,
        **kwargs,
    ) -> None:
        from pipecat.services.settings import TTSSettings

        super().__init__(
            sample_rate=sample_rate,
            settings=TTSSettings(model=None, voice=voice, language=language),
            **kwargs,
        )
        self._provider = provider
        self._voice = voice
        self._language = language
        self._speed = speed
        self._pause_ms = max(0, int(pause_ms or 0))
        self._recorder = recorder
        self._model = model or ""
        self._provider_name = provider_name or getattr(provider, "name", "tts")
        self._naturalness = naturalness
        # Sentence pause bookkeeping: sentences of one turn share a context id
        # and synthesize strictly in sequence here, so "a previous sentence of
        # this context produced audio" IS the sentence boundary. A new turn
        # (or barge-in) gets a fresh context id, which resets the counter.
        self._pause_context_id: str | None = None
        self._spoken_in_context = 0
        self._planned_gap_ms = self._pause_ms

    def can_generate_metrics(self) -> bool:
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, SwitchVoiceLanguageFrame):
            # Per-turn language following applies to the segmented REST path
            # too: subsequent synthesize calls speak the caller's language.
            if frame.language and frame.language != self._language:
                logger.info(
                    "tts: switching language %s → %s", self._language, frame.language
                )
                self._language = frame.language
            return
        await super().process_frame(frame, direction)

    async def run_tts(self, text: str, context_id: str):
        if not has_speakable_text(text):
            # Punctuation/emoji-only fragments have nothing to voice and some
            # providers reject them outright (Sarvam: 422) — skip, never send.
            logger.info("tts: skipping unspeakable segment (%d chars)", len(text))
            yield None
            return
        if context_id != self._pause_context_id:
            self._pause_context_id = context_id
            self._spoken_in_context = 0
            self._planned_gap_ms = self._pause_ms
        # The gap is PREPENDED to the next sentence's first audio, so a turn
        # never starts or ends with inserted silence, and interruption drops
        # it together with the rest of the audio context.
        gap_owed = self._pause_ms > 0 and self._spoken_in_context > 0
        gap_ms = self._planned_gap_ms
        delivery = None
        planning_ms = 0.0
        if self._naturalness is not None:
            planned_at = time.perf_counter()
            delivery = self._naturalness.plan_segment(
                text, base_pause_ms=self._pause_ms, language=self._language or ""
            )
            planning_ms = (time.perf_counter() - planned_at) * 1000.0
        capabilities = delivery_capabilities(
            self._provider_name, self._model, streaming=False
        )
        speed_scale = (
            delivery.speed_scale
            if delivery is not None and capabilities.per_segment_rate
            else None
        )
        segment_speed = self._speed * (speed_scale if speed_scale is not None else 1.0)
        if delivery is not None:
            words = len((text or "").split())
            logger.info("naturalness_segment %s", json.dumps({
                "session": self._recorder.session_id if self._recorder else "?",
                "context": str(context_id)[:8],
                "provider": self._provider_name,
                "model": self._model,
                "human_speech_enabled": bool(
                    getattr(self._naturalness, "enabled", False)
                ),
                "language": self._language,
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
                    if delivery.pause_after_ms is not None else self._pause_ms
                ),
                "speed_scale": speed_scale,
                "speaking_rate": (
                    provider_speed(
                        self._provider_name, self._model, segment_speed
                    ) if capabilities.speaking_rate else None
                ),
                "speech_style": delivery.speech_style,
                "emphasis": delivery.emphasis,
                "pitch_scale": delivery.pitch_scale if capabilities.pitch else None,
                "energy_scale": delivery.energy_scale if capabilities.energy else None,
                "question_style": (
                    delivery.question_style if capabilities.question_style else False
                ),
                "phrase_boundary_count": len(delivery.phrase_boundaries),
                "naturalness_processing_ms": round(planning_ms, 3),
            }, ensure_ascii=False))
        try:
            await self.start_ttfb_metrics()
            # REST providers emit a fixed rate (e.g. ElevenLabs pcm_16000);
            # frames are stamped with the pipeline rate, so mismatching audio
            # must be resampled or it plays at the wrong speed.
            provider_rate = getattr(self._provider, "output_sample_rate", None)
            first = True
            async for chunk in self._provider.stream_synthesize(
                text, voice=self._voice, language=self._language, speed=segment_speed
            ):
                if not chunk:
                    continue
                if first:
                    await self.stop_ttfb_metrics()
                    first = False
                    self._spoken_in_context += 1
                    if gap_owed:
                        yield TTSAudioRawFrame(
                            silence_pcm(self.sample_rate, gap_ms),
                            self.sample_rate, 1, context_id=context_id,
                        )
                    if delivery is not None:
                        self._planned_gap_ms = (
                            delivery.pause_after_ms
                            if delivery.pause_after_ms is not None else self._pause_ms
                        )
                    add_usage = getattr(self._recorder, "add_tts_usage", None)
                    if add_usage is not None:
                        # Billed once per synthesized segment, on first audio.
                        add_usage(
                            provider=getattr(self._provider, "name", "tts"),
                            model=self._model,
                            voice=self._voice or "",
                            characters=len(text),
                        )
                if provider_rate and provider_rate != self.sample_rate:
                    chunk = resample_pcm(chunk, provider_rate, self.sample_rate)
                yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
        except ProviderError as exc:
            logger.warning("TTS provider failure: %s", exc)
            yield ErrorFrame(error=f"tts_failure:{exc.category}")
        except Exception:  # noqa: BLE001
            logger.exception("TTS unexpected failure")
            yield ErrorFrame(error="tts_failure:unexpected")
