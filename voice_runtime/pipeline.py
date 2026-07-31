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
from pipecat.audio.vad.vad_analyzer import VADParams
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

# ── turn detection ──────────────────────────────────────────────────────────
# End-of-turn = VAD silence (stop_secs, also triggers the STT flush so the
# final transcript overlaps the policy wait) + user_speech_timeout (the window
# in which the caller may resume after a pause). The brain only runs the LLM
# once the turn closes, so the effective endpoint a caller experiences is
# stop_secs + user_speech_timeout of silence (~1 s with the defaults).
#
# Telephony audio is quieter and band-limited (8 kHz PSTN), so its VAD
# thresholds are more permissive — with the browser defaults, short low-energy
# words ("हाँ", "yes") often never trip VAD start, the STT never gets flushed
# and the first response waits on the provider's own slow endpointing.
#
# Per-bot overrides live in voice settings: stt_settings.turn_detection.
TURN_DETECTION_DEFAULTS: dict[str, dict[str, float]] = {
    "browser": {
        "confidence": 0.7,
        "start_secs": 0.2,
        "stop_secs": 0.2,
        "min_volume": 0.6,
        "user_speech_timeout": 0.8,
    },
    "telephony": {
        "confidence": 0.6,
        "start_secs": 0.2,
        "stop_secs": 0.2,
        "min_volume": 0.4,
        "user_speech_timeout": 0.8,
    },
}

# Misconfiguration must never produce an unusable call (e.g. a 30 s endpoint
# or a VAD that triggers on line noise).
_TURN_BOUNDS: dict[str, tuple[float, float]] = {
    "confidence": (0.3, 0.95),
    "start_secs": (0.1, 1.0),
    "stop_secs": (0.1, 2.0),
    "min_volume": (0.0, 1.0),
    "user_speech_timeout": (0.2, 3.0),
}


def resolve_turn_detection(
    config: ResolvedBotConfig, transport_kind: str = "browser"
) -> dict[str, float]:
    """Effective turn-detection parameters for one call.

    Transport-aware defaults overridden by the bot's
    ``stt_settings.turn_detection``, every value clamped to a sane range.
    """
    defaults = TURN_DETECTION_DEFAULTS.get(
        transport_kind, TURN_DETECTION_DEFAULTS["browser"]
    )
    overrides = ((config.stt or {}).get("settings") or {}).get("turn_detection") or {}
    resolved: dict[str, float] = {}
    for key, default in defaults.items():
        value = overrides.get(key, default)
        try:
            value = float(value)
        except (TypeError, ValueError):
            logger.warning(
                "turn_detection.%s=%r is not a number; using default %s",
                key, value, default,
            )
            value = default
        low, high = _TURN_BOUNDS[key]
        resolved[key] = min(max(value, low), high)
    return resolved


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
    call_context: dict | None = None,
    transport_kind: str = "browser",
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
        call_context=call_context,
    )

    turn = resolve_turn_detection(config, transport_kind)
    processors = [transport.input()]
    if use_vad:
        processors.append(
            VADProcessor(
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(
                        confidence=turn["confidence"],
                        start_secs=turn["start_secs"],
                        stop_secs=turn["stop_secs"],
                        min_volume=turn["min_volume"],
                    )
                )
            )
        )
    # STT must receive VADUserStoppedSpeakingFrame directly. Sarvam uses that
    # frame to flush its streaming socket; placing UserTurnProcessor first
    # consumed the control frame and left telephony transcripts waiting for
    # the provider's roughly 60-second server-side endpoint.
    processors.append(stt)
    processors.append(
        UserTurnProcessor(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy()],
                # wait_for_transcript must be False: transcripts are consumed by
                # the brain downstream and never reach the turn processor, so
                # waiting for one only ever hits the 5s fallback — which also
                # blocked barge-in (a new turn can't start while the previous
                # one is stuck open). The brain gates the LLM on the resulting
                # UserStoppedSpeakingFrame, so this timeout IS the pause window
                # a caller gets before the bot takes the turn.
                stop=[SpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=turn["user_speech_timeout"],
                    wait_for_transcript=False,
                )],
            )
        )
    )
    processors += [brain, tts, transport.output()]

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
