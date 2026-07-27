"""Voice pipeline assembly.

    transport.input() → VAD → user-turn control → STT → brain → TTS → transport.output()

Interruption/barge-in is handled by the user-turn controller (VAD start
strategy) which interrupts bot output; the brain additionally cancels its
in-flight LLM/retrieval work on interruption frames, and the streaming TTS
router cancels provider-side synthesis and rejects late audio.

STT/TTS/LLM engines are built from the bot's database-driven provider
configuration (ResolvedBotConfig):
- STT: Sarvam realtime WebSocket (pipecat SarvamSTTService) when the bot's
  STT provider is Sarvam; the segmented EchoSTTService otherwise (mock, REST).
- TTS: StreamingTTSRouter for WebSocket providers (Sarvam, ElevenLabs) with
  per-language voice mapping and fallback; EchoTTSService otherwise.
"""

import logging

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.factory import get_llm_provider, get_stt_provider, get_tts_provider
from shared.bot_config import ResolvedBotConfig
from voice_runtime.brain import ConversationBrain
from voice_runtime.services import EchoSTTService, EchoTTSService
from voice_runtime.tts_router import StreamingTTSRouter, is_streaming_tts_provider
from voice_runtime.recording import SessionRecorder

logger = logging.getLogger(__name__)


def _sarvam_stream_encodings() -> set[str]:
    """Audio encodings the installed sarvamai SDK accepts for streaming STT.

    sarvamai 0.1.28 pins AudioData.encoding to Literal["audio/wav"]; newer
    SDKs may widen it. Introspecting keeps us honest either way — sending an
    unsupported value fails per-chunk validation and silently kills STT.
    """
    try:
        import typing

        from sarvamai.requests.audio_data import AudioDataParams

        hints = typing.get_type_hints(AudioDataParams)
        values = set(typing.get_args(hints["encoding"]))
        if values:
            return values
    except Exception:  # noqa: BLE001 — introspection must never break calls
        pass
    return {"audio/wav"}


def build_stt_service(config: ResolvedBotConfig, *, sample_rate: int = 16000,
                      recorder: SessionRecorder | None = None):
    """STT service from bot config: Sarvam realtime WS or segmented fallback."""
    stt_conf = config.stt or {}
    provider = stt_conf.get("provider") or "sarvam"

    if provider == "sarvam":
        # Realtime WebSocket STT via pipecat's Sarvam integration (sarvamai SDK).
        from pipecat.services.sarvam.stt import SarvamSTTService

        api_key = get_settings().resolve_secret(stt_conf.get("api_key_reference") or "")
        if not api_key:
            raise ProviderError(
                "sarvam-stt", "auth",
                "Sarvam STT credentials are not configured (SARVAM_API_KEY)",
            )
        settings_kwargs = stt_conf.get("settings") or {}
        model = stt_conf.get("model") or "saaras:v3"
        language = stt_conf.get("language") or None  # None → auto-detect ("unknown")
        mode = settings_kwargs.get("mode")
        service_settings = SarvamSTTService.Settings(
            model=model,
            language=language,
            vad_signals=settings_kwargs.get("vad_signals", False) or None,
            high_vad_sensitivity=settings_kwargs.get("high_vad_sensitivity") or None,
        )
        codec = settings_kwargs.get("input_encoding", "wav")
        if codec not in ("wav", "pcm_s16le"):
            codec = "wav"
        if f"audio/{codec}" not in _sarvam_stream_encodings():
            # A codec the installed sarvamai SDK rejects would fail EVERY audio
            # chunk (pydantic Literal validation) and the call would produce no
            # transcripts at all — clamp to wav (raw PCM16 bytes either way).
            logger.warning(
                "sarvam-stt: input_encoding '%s' not supported by the installed "
                "sarvamai SDK; using 'wav'", codec,
            )
            codec = "wav"
        return SarvamSTTService(
            api_key=api_key,
            mode=mode if model.startswith("saaras") else None,
            sample_rate=sample_rate,
            input_audio_codec=codec,
            settings=service_settings,
            keepalive_timeout=8.0,
        )

    stt_provider = get_stt_provider(
        ProviderConfig(
            provider=provider,
            model=stt_conf.get("model", ""),
            language=stt_conf.get("language") or config.language,
            api_key_reference=stt_conf.get("api_key_reference", ""),
            extra=stt_conf.get("extra", {}),
        )
    )
    return EchoSTTService(
        stt_provider,
        language=stt_conf.get("language") or config.language,
        recorder=recorder,
    )


def build_tts_service(
    config: ResolvedBotConfig,
    *,
    recorder: SessionRecorder,
    sample_rate: int = 24000,
):
    """TTS service from bot config: streaming router or segmented fallback."""
    tts_conf = config.tts or {}
    provider = tts_conf.get("provider") or "sarvam"

    if is_streaming_tts_provider(provider):
        return StreamingTTSRouter(
            tts_config=tts_conf,
            language=config.language,
            speed=config.speed,
            sample_rate=sample_rate,
            recorder=recorder,
        )

    tts_provider = get_tts_provider(
        ProviderConfig(
            provider=provider,
            model=tts_conf.get("model", ""),
            voice=tts_conf.get("voice", ""),
            language=config.language,
            api_key_reference=tts_conf.get("api_key_reference", ""),
            extra=tts_conf.get("extra", {}),
        )
    )
    return EchoTTSService(
        tts_provider,
        voice=tts_conf.get("voice") or None,
        language=config.language,
        speed=config.speed,
        sample_rate=sample_rate,
        recorder=recorder,
        model=tts_conf.get("model") or "",
    )


def build_llm_provider(config: ResolvedBotConfig):
    llm_conf = config.llm or {}
    return get_llm_provider(
        ProviderConfig(
            provider=llm_conf.get("provider", "openai"),
            model=llm_conf.get("model", ""),
            api_key_reference=llm_conf.get("api_key_reference", ""),
            timeout_seconds=float(
                (llm_conf.get("settings") or {}).get("timeout_seconds", 15.0)
            ),
            extra=llm_conf.get("extra", {}),
        )
    )


def build_voice_pipeline(
    *,
    transport,
    config: ResolvedBotConfig,
    recorder: SessionRecorder,
    knowledge_service=None,
    workflow_engine=None,
    tts_sample_rate: int = 24000,
    stt_sample_rate: int = 16000,
    use_vad: bool = True,
    idle_timeout_secs: float | None = None,
    client_info: dict | None = None,
) -> tuple[PipelineWorker, ConversationBrain]:
    """Assemble the Pipecat pipeline for one call session."""
    stt = build_stt_service(config, sample_rate=stt_sample_rate, recorder=recorder)
    tts = build_tts_service(config, recorder=recorder, sample_rate=tts_sample_rate)
    llm_provider = build_llm_provider(config)

    brain = ConversationBrain(
        config=config,
        llm=llm_provider,
        recorder=recorder,
        knowledge_service=knowledge_service,
        workflow_engine=workflow_engine,
        client_info=client_info,
    )

    processors = [transport.input()]
    if use_vad:
        processors.append(VADProcessor(vad_analyzer=SileroVADAnalyzer()))
    processors.append(
        UserTurnProcessor(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy()],
                # wait_for_transcript must be False: transcripts are consumed by
                # the brain downstream and never reach the turn processor, so
                # waiting for one only ever hits the 5s fallback — which also
                # blocked barge-in (a new turn can't start while the previous
                # one is stuck open).
                stop=[SpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=0.8, wait_for_transcript=False,
                )],
            )
        )
    )
    processors += [stt, brain, tts, transport.output()]

    worker = PipelineWorker(
        Pipeline(processors),
        params=PipelineParams(
            audio_in_sample_rate=stt_sample_rate,
            audio_out_sample_rate=tts_sample_rate,
            enable_metrics=True,
        ),
        conversation_id=recorder.session_id,
        idle_timeout_secs=idle_timeout_secs,
        cancel_on_idle_timeout=False,
        enable_rtvi=False,
    )
    return worker, brain
