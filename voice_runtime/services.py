"""Pipecat service wrappers around EchoSphere's provider layer.

These adapters let the same tenant/bot-selectable providers (shared.providers)
drive the realtime pipeline, so the voice runtime and the REST platform share
one provider implementation.
"""

import logging

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

from shared.providers.base import ProviderError, STTProvider, TTSProvider
from shared.audio.pcm import wav_to_pcm

logger = logging.getLogger(__name__)


class EchoSTTService(SegmentedSTTService):
    """VAD-segmented STT backed by an EchoSphere STTProvider."""

    def __init__(self, provider: STTProvider, *, language: str | None = None, **kwargs) -> None:
        from pipecat.services.settings import STTSettings

        super().__init__(settings=STTSettings(model=None, language=language), **kwargs)
        self._provider = provider
        self._language = language

    async def run_stt(self, audio: bytes):
        """`audio` arrives as a WAV container (wants_wav_segments default)."""
        try:
            pcm, rate = wav_to_pcm(audio)
            result = await self._provider.transcribe(
                pcm, sample_rate=rate, language=self._language
            )
            if result.text:
                yield TranscriptionFrame(result.text, "caller", time_now_iso8601())
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
        sample_rate: int | None = None,
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

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str):
        try:
            await self.start_ttfb_metrics()
            first = True
            async for chunk in self._provider.stream_synthesize(
                text, voice=self._voice, language=self._language, speed=self._speed
            ):
                if not chunk:
                    continue
                if first:
                    await self.stop_ttfb_metrics()
                    first = False
                yield TTSAudioRawFrame(chunk, self.sample_rate, 1, context_id=context_id)
        except ProviderError as exc:
            logger.warning("TTS provider failure: %s", exc)
            yield ErrorFrame(error=f"tts_failure:{exc.category}")
        except Exception:  # noqa: BLE001
            logger.exception("TTS unexpected failure")
            yield ErrorFrame(error="tts_failure:unexpected")
