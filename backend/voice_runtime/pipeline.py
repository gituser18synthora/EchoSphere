"""Voice pipeline assembly.

    transport.input() → VAD → user-turn control → STT → brain → TTS → transport.output()

Interruption/barge-in is handled by the user-turn controller (VAD start
strategy) which interrupts bot output; the brain additionally cancels its
in-flight LLM/retrieval work on interruption frames.
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

from backend.providers.base import ProviderConfig
from backend.providers.factory import get_llm_provider, get_stt_provider, get_tts_provider
from backend.voice_runtime.bot_config import ResolvedBotConfig
from backend.voice_runtime.brain import ConversationBrain
from backend.voice_runtime.services import EchoSTTService, EchoTTSService
from backend.voice_runtime.session import SessionRecorder

logger = logging.getLogger(__name__)


def build_providers(config: ResolvedBotConfig):
    stt = get_stt_provider(
        ProviderConfig(
            provider=config.stt.get("provider", "openai"),
            model=config.stt.get("model", ""),
            language=config.language,
            extra=config.stt.get("extra", {}),
        )
    )
    tts = get_tts_provider(
        ProviderConfig(
            provider=config.tts.get("provider", "openai"),
            model=config.tts.get("model", ""),
            voice=config.tts.get("voice", ""),
            language=config.language,
            extra=config.tts.get("extra", {}),
        )
    )
    llm = get_llm_provider(
        ProviderConfig(
            provider=config.llm.get("provider", "openai"),
            model=config.llm.get("model", ""),
            extra=config.llm.get("extra", {}),
        )
    )
    return stt, tts, llm


def build_voice_pipeline(
    *,
    transport,
    config: ResolvedBotConfig,
    recorder: SessionRecorder,
    knowledge_service=None,
    workflow_engine=None,
    tts_sample_rate: int = 24000,
    use_vad: bool = True,
    idle_timeout_secs: float | None = None,
) -> tuple[PipelineWorker, ConversationBrain]:
    """Assemble the Pipecat pipeline for one call session."""
    stt_provider, tts_provider, llm_provider = build_providers(config)

    stt = EchoSTTService(stt_provider, language=config.language)
    tts = EchoTTSService(
        tts_provider,
        voice=config.tts.get("voice") or None,
        language=config.language,
        speed=config.speed,
        sample_rate=tts_sample_rate,
    )
    brain = ConversationBrain(
        config=config,
        llm=llm_provider,
        recorder=recorder,
        knowledge_service=knowledge_service,
        workflow_engine=workflow_engine,
    )

    processors = [transport.input()]
    if use_vad:
        processors.append(VADProcessor(vad_analyzer=SileroVADAnalyzer()))
    processors.append(
        UserTurnProcessor(
            user_turn_strategies=UserTurnStrategies(
                start=[VADUserTurnStartStrategy()],
                stop=[SpeechTimeoutUserTurnStopStrategy(timeout=0.8)],
            )
        )
    )
    processors += [stt, brain, tts, transport.output()]

    worker = PipelineWorker(
        Pipeline(processors),
        params=PipelineParams(
            audio_in_sample_rate=16000,
            audio_out_sample_rate=tts_sample_rate,
            enable_metrics=True,
        ),
        conversation_id=recorder.session_id,
        idle_timeout_secs=idle_timeout_secs,
        cancel_on_idle_timeout=False,
        enable_rtvi=False,
    )
    return worker, brain
