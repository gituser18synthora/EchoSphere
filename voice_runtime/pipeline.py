"""Voice pipeline assembly.

    transport.input() → noise gate → VAD → latency probe → STT
        → user-turn control → brain → TTS → transport.output()

Speech/noise separation runs in three layers, cheapest first: the caller audio
gate (adaptive energy relative to this call's measured noise floor — see
voice_runtime.audio_gate), Silero VAD (neural speech probability), and the
final-transcript quality gate (voice_runtime.transcript_gate). The energy gate
is deliberately AHEAD of the VAD: noise it suppresses can never start a user
turn, interrupt the bot, or reach the STT.

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

import asyncio
import logging
import math
from dataclasses import replace

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.turns.user_start.vad_user_turn_start_strategy import VADUserTurnStartStrategy
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import (
    ExternalUserTurnStrategies,
    UserTurnStrategies,
)

from shared.config import get_settings
from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.factory import get_llm_provider, get_stt_provider, get_tts_provider
from shared.turn_detection import (
    NOISE_GATE_BOUNDS,
    NOISE_GATE_DEFAULTS,
    TURN_DETECTION_BOUNDS,
    TURN_DETECTION_DEFAULTS,
    resolve_bounded,
)
from shared.orchestration.naturalness import SpeechNaturalnessPlanner
from shared.providers.tts.delivery import apply_delivery_params
from shared.bot_config import ResolvedBotConfig
from voice_runtime.audio_gate import CallerAudioGate
from voice_runtime.barge_in import WordConfirmedBargeInStrategy
from voice_runtime.silence_policy import SilencePolicy
from voice_runtime.brain import ConversationBrain
from voice_runtime.latency_filler import LatencyFillerProcessor, get_filler_library
from voice_runtime.voiced_cues import get_voiced_cue_library
from voice_runtime.services import EchoSTTService, EchoTTSService
from voice_runtime.tts_router import StreamingTTSRouter, is_streaming_tts_provider
from voice_runtime.recording import (
    AlignedStereoRecorder,
    CallRecordingWriter,
    SessionRecorder,
)
from voice_runtime.turn_metrics import TurnLatencyTracker, VADLatencyProbe

logger = logging.getLogger(__name__)

# ── turn detection ──────────────────────────────────────────────────────────
# End-of-turn = VAD silence (stop_secs, also triggers the STT flush so the
# final transcript overlaps the policy wait) + user_speech_timeout (the window
# in which the caller may resume after a pause). The brain only runs the LLM
# once the turn closes, so the effective endpoint a caller experiences is
# stop_secs + user_speech_timeout of silence (~1.4 s in the browser).
#
# Telephony audio is quieter and band-limited (8 kHz PSTN), so its VAD
# thresholds are more permissive — with the browser defaults, short low-energy
# words ("हाँ", "yes") often never trip VAD start, the STT never gets flushed
# and the first response waits on the provider's own slow endpointing.
#
# Tenant settings are merged into the immutable session snapshot before this
# module is constructed. Legacy per-bot STT values are read only when an old
# cached snapshot does not yet carry the tenant map.
# Misconfiguration must never produce an unusable call (e.g. a 30 s endpoint
# or a VAD that triggers on line noise).
# Bounds are shared with backend validation (``shared.turn_detection``), so a
# value accepted by the Tenant Admin API is guaranteed to be safe here.


def _warn_invalid(section: str):
    def report(key, value, default):
        logger.warning(
            "%s.%s=%r is not a number; using default %s", section, key, value, default
        )

    return report


def resolve_silence_policy(turn: dict[str, float]) -> SilencePolicy:
    """Platform silence policy with the tenant-configured ladder values.

    ``turn`` is the resolved Turn Detection map for this call's transport:
    first-prompt wait, retry interval and prompt count come from it (a
    snapshot written before a field existed has no key and keeps that
    field's schema default). Bounds are enforced by the schema already; the
    clamp here only guards a corrupt cached value. The post-hold grace stays
    a platform setting.
    """
    base = SilencePolicy.from_settings(get_settings())

    def _turn_value(key: str) -> float:
        low, high = TURN_DETECTION_BOUNDS[key]
        default = TURN_DETECTION_DEFAULTS["browser"][key]
        try:
            value = float(turn.get(key, default))
        except (TypeError, ValueError):
            value = default
        if not math.isfinite(value):
            value = default
        return min(high, max(low, value))

    return replace(
        base,
        prompt_seconds=_turn_value("silence_prompt_seconds"),
        retry_seconds=_turn_value("silence_retry_seconds"),
        max_prompts=int(round(_turn_value("silence_max_prompts"))),
    )


def resolve_turn_detection(
    config: ResolvedBotConfig, transport_kind: str = "browser"
) -> dict[str, float]:
    """Effective turn-detection parameters for one call.

    Fresh snapshots carry a fully-resolved tenant transport map. The legacy
    per-bot STT location remains a fallback only for older cached snapshots
    and direct unit/runtime callers created before tenant settings existed.
    """
    session_values = (config.turn_detection or {}).get(transport_kind, {})
    if isinstance(session_values, dict) and isinstance(
        session_values.get("turn_detection"), dict
    ):
        return resolve_bounded(
            session_values["turn_detection"],
            TURN_DETECTION_DEFAULTS.get(transport_kind, TURN_DETECTION_DEFAULTS["browser"]),
            TURN_DETECTION_BOUNDS,
            on_invalid=_warn_invalid("tenant_turn_detection"),
        )
    return resolve_bounded(
        ((config.stt or {}).get("settings") or {}).get("turn_detection"),
        TURN_DETECTION_DEFAULTS.get(transport_kind, TURN_DETECTION_DEFAULTS["browser"]),
        TURN_DETECTION_BOUNDS,
        on_invalid=_warn_invalid("turn_detection"),
    )


def resolve_noise_gate(
    config: ResolvedBotConfig, transport_kind: str = "browser"
) -> dict[str, float]:
    """Effective caller-audio noise-gate parameters for one call.

    Uses the per-session tenant snapshot when present and the legacy bot STT
    location for backward-compatible cached/direct configs. ``enabled`` is a
    0/1 float so the whole section validates and clamps through one code path.
    """
    session_values = (config.turn_detection or {}).get(transport_kind, {})
    if isinstance(session_values, dict) and isinstance(
        session_values.get("noise_gate"), dict
    ):
        return resolve_bounded(
            session_values["noise_gate"],
            NOISE_GATE_DEFAULTS.get(transport_kind, NOISE_GATE_DEFAULTS["browser"]),
            NOISE_GATE_BOUNDS,
            on_invalid=_warn_invalid("tenant_noise_gate"),
        )
    return resolve_bounded(
        ((config.stt or {}).get("settings") or {}).get("noise_gate"),
        NOISE_GATE_DEFAULTS.get(transport_kind, NOISE_GATE_DEFAULTS["browser"]),
        NOISE_GATE_BOUNDS,
        on_invalid=_warn_invalid("noise_gate"),
    )


# Deepgram Flux end-of-turn defaults, chosen for telephony voice agents:
# eot_threshold stays at the provider's recommended 0.7 (raising it delays
# every turn; lowering it interrupts natural pauses), the eager threshold is
# conservative (speculation costs decision-LLM calls when it misfires), and
# the silence cap forces a turn end well before the provider's 5 s default —
# a caller who has said something and then stays quiet for 3 s is done.
_FLUX_DEFAULT_EAGER_EOT = 0.6
_FLUX_DEFAULT_EOT_TIMEOUT_MS = 3000
_FLUX_BOUNDS = {
    "eot_threshold": (0.5, 0.9),
    "eager_eot_threshold": (0.3, 0.9),
    "eot_timeout_ms": (500, 60000),
}


def _flux_setting(settings: dict, key: str, default):
    """One bounded numeric Flux setting; junk falls back to the default."""
    value = settings.get(key, default)
    if value is None:
        return None  # explicit null disables the feature (eager EOT)
    low, high = _FLUX_BOUNDS[key]
    try:
        if isinstance(value, bool):
            raise TypeError
        value = float(value)
    except (TypeError, ValueError):
        logger.warning("deepgram-stt: %s=%r is not a number; using %s", key, value, default)
        if default is None:
            return None
        value = float(default)
    value = min(max(value, low), high)
    return int(value) if key == "eot_timeout_ms" else value


def _flux_language_hints(stt_conf: dict, config: ResolvedBotConfig) -> list:
    """Language hints for flux-general-multi, as pipecat Language values.

    Explicit ``stt_settings.language_hints`` win; otherwise the bot's own
    configured languages (hi-IN, en-IN → hi, en) are the hints — the model
    biases toward exactly the languages the bot is allowed to speak.
    """
    from pipecat.transcriptions.language import Language

    settings = stt_conf.get("settings") or {}
    raw = settings.get("language_hints")
    if not isinstance(raw, (list, tuple)) or not raw:
        raw = [locale for locale in (config.languages or [config.language]) if locale]
    hints = []
    for code in raw:
        base = str(code).split("-")[0].strip().lower()
        if not base:
            continue
        try:
            hints.append(Language(base))
        except ValueError:
            logger.warning("deepgram-stt: unsupported language hint %r ignored", code)
    seen = set()
    return [h for h in hints if not (h in seen or seen.add(h))]


def build_stt_service(
    config: ResolvedBotConfig,
    *,
    sample_rate: int = 16000,
    recorder: SessionRecorder | None = None,
    use_provider_vad: bool | None = None,
    latency=None,
    barge_in_min_words: int = 2,
    prefer_primary_language: bool = False,
):
    """STT service from bot config: Deepgram Flux or Sarvam realtime WS,
    segmented fallback otherwise."""
    stt_conf = config.stt or {}
    provider = stt_conf.get("provider") or "sarvam"

    if provider == "deepgram":
        # Deepgram Flux conversational STT over /v2/listen: one persistent
        # WebSocket per call, model-integrated turn detection (EndOfTurn is
        # authoritative — see voice_runtime.deepgram_stt).
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTSettings

        from voice_runtime.deepgram_stt import EchoDeepgramFluxSTTService

        api_key = get_settings().resolve_secret(stt_conf.get("api_key_reference") or "")
        if not api_key:
            raise ProviderError(
                "deepgram-stt", "auth",
                "Deepgram STT credentials are not configured (DEEPGRAM_API_KEY)",
            )
        settings_kwargs = stt_conf.get("settings") or {}
        model = stt_conf.get("model") or "flux-general-multi"
        service_settings = DeepgramFluxSTTSettings(
            model=model,
            language=None,
            eot_threshold=_flux_setting(settings_kwargs, "eot_threshold", 0.7),
            eager_eot_threshold=_flux_setting(
                settings_kwargs, "eager_eot_threshold", _FLUX_DEFAULT_EAGER_EOT
            ),
            eot_timeout_ms=_flux_setting(
                settings_kwargs, "eot_timeout_ms", _FLUX_DEFAULT_EOT_TIMEOUT_MS
            ),
        )
        if model == "flux-general-multi":
            hints = _flux_language_hints(stt_conf, config)
            if hints:
                service_settings.language_hints = hints
        extra_params: dict[str, str] = {}
        # numerals: spoken numbers arrive as digits ("six zero one" → "6 0 1"),
        # which identifier extraction fuses into one run. Supported by Flux on
        # /v2/listen (English-family languages on flux-general-multi; other
        # languages pass through unchanged). stt_settings.numerals=false
        # opts a bot out.
        if settings_kwargs.get("numerals", True):
            extra_params["numerals"] = "true"
        return EchoDeepgramFluxSTTService(
            api_key=api_key,
            sample_rate=sample_rate,
            settings=service_settings,
            recorder=recorder,
            latency=latency,
            barge_in_min_words=barge_in_min_words,
            extra_query_params=extra_params,
        )

    if provider == "sarvam":
        # Realtime WebSocket STT via pipecat's Sarvam integration (sarvamai SDK),
        # subclassed to report segment finality — see voice_runtime.sarvam_stt.
        from pipecat.services.sarvam.stt import SarvamSTTService
        from pipecat.transcriptions.language import Language

        from voice_runtime.sarvam_stt import EndpointedSarvamSTTService

        api_key = get_settings().resolve_secret(stt_conf.get("api_key_reference") or "")
        if not api_key:
            raise ProviderError(
                "sarvam-stt", "auth",
                "Sarvam STT credentials are not configured (SARVAM_API_KEY)",
            )
        settings_kwargs = stt_conf.get("settings") or {}
        model = stt_conf.get("model") or "saaras:v3"
        # Auto-detection is unreliable on the sub-three-second, narrowband
        # snippets common on phone calls.  Telephony therefore supplies the
        # bot's primary language when STT language is blank, with an explicit
        # opt-out for genuinely multilingual bots.
        language_code = stt_conf.get("language") or None
        if (
            language_code is None
            and prefer_primary_language
            and settings_kwargs.get("auto_detect_language") is not True
        ):
            language_code = config.language or None
        language = None
        if language_code:
            try:
                language = Language(language_code)
            except ValueError:
                logger.warning(
                    "sarvam-stt: unsupported configured language %r; using auto-detect",
                    language_code,
                )
        mode = settings_kwargs.get("mode")
        provider_vad = settings_kwargs.get("vad_signals", False)
        if use_provider_vad is not None:
            provider_vad = use_provider_vad
        forwarded_setting_names = (
            "positive_speech_threshold",
            "negative_speech_threshold",
            "min_speech_frames",
            "first_turn_min_speech_frames",
            "negative_frames_count",
            "negative_frames_window",
            "start_speech_volume_threshold",
            "interrupt_min_speech_frames",
            "pre_speech_pad_frames",
            "num_initial_ignored_frames",
        )
        forwarded_settings = {
            key: settings_kwargs[key]
            for key in forwarded_setting_names
            if key in settings_kwargs and settings_kwargs[key] is not None
        }
        service_settings = SarvamSTTService.Settings(
            model=model,
            language=language,
            vad_signals=provider_vad,
            high_vad_sensitivity=settings_kwargs.get("high_vad_sensitivity"),
            **forwarded_settings,
        )
        codec = settings_kwargs.get("input_encoding") or "pcm_s16le"
        if codec != "pcm_s16le":
            # Pipecat audio frames are raw little-endian PCM, not standalone
            # RIFF/WAV files.  Labelling those bytes "wav" made short phone
            # utterances intermittently disappear at the provider.
            logger.warning(
                "sarvam-stt: input_encoding '%s' does not match Pipecat's raw "
                "audio frames; using 'pcm_s16le'", codec,
            )
            codec = "pcm_s16le"
        return EndpointedSarvamSTTService(
            api_key=api_key,
            mode=mode if model.startswith("saaras") else None,
            sample_rate=sample_rate,
            input_audio_codec=codec,
            settings=service_settings,
            keepalive_timeout=8.0,
            recorder=recorder,
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


class AlignedRecordingProcessor(FrameProcessor):
    """Feeds the wall-clock-aligned stereo recorder from pipeline frames.

    Pure observer: every frame passes through untouched, so the audio sent to
    STT and to the caller is byte-identical with or without recording. Ready
    stereo chunks are handed to a dedicated writer task — disk latency never
    sits on the audio path — and each aligned region is written exactly once
    (``stop_recording`` is idempotent, covering both the EndFrame path and
    the recorder-driven ``CallRecordingWriter.close()``).
    """

    def __init__(self, *, writer: CallRecordingWriter, sample_rate: int) -> None:
        super().__init__()
        self._writer = writer
        self._aligner = AlignedStereoRecorder(sample_rate=sample_rate)
        self._in_resampler = create_stream_resampler()
        self._out_resampler = create_stream_resampler()
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._write_task = None
        self._stopped = False

    def _enqueue(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._write_task is None:
            self._write_task = self.create_task(self._write_loop())
        self._write_queue.put_nowait(chunk)

    async def _write_loop(self) -> None:
        while True:
            chunk = await self._write_queue.get()
            try:
                await self._writer.append(chunk, self._aligner.sample_rate, 2)
            except Exception:  # noqa: BLE001 — recording must never break audio
                logger.warning("aligned recording write failed", exc_info=True)
            finally:
                self._write_queue.task_done()

    async def stop_recording(self) -> None:
        """Flush the tail and drain pending writes. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        tail = self._aligner.stop()
        if tail:
            self._enqueue(tail)
        if self._write_task is not None:
            await self._write_queue.join()
            await self.cancel_task(self._write_task)
            self._write_task = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.audio and not self._stopped:
            resampled = await self._in_resampler.resample(
                frame.audio, frame.sample_rate, self._aligner.sample_rate
            )
            self._enqueue(self._aligner.add_user_audio(resampled))
        elif isinstance(frame, OutputAudioRawFrame) and frame.audio and not self._stopped:
            resampled = await self._out_resampler.resample(
                frame.audio, frame.sample_rate, self._aligner.sample_rate
            )
            self._enqueue(self._aligner.add_bot_audio(resampled))
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self.stop_recording()
        await self.push_frame(frame, direction)


def build_batch_transcriber(config: ResolvedBotConfig):
    """One-shot batch STT for identifier recovery, or None when unsupported.

    Provider-neutral: the same REST ``STTProvider.transcribe`` the segmented
    pipeline uses, built lazily on FIRST use so no client or connection
    exists unless a recovery actually runs. Any setup/availability failure
    degrades to None-behavior (the caller treats a raised error as a failed,
    never-retried recovery) — recovery is an optimization, not a dependency.
    """
    stt_conf = dict(config.stt or {})
    provider = stt_conf.get("provider") or "sarvam"
    holder: list = []

    async def _transcribe(pcm: bytes, sample_rate: int, language: str) -> str:
        if not holder:
            holder.append(get_stt_provider(
                ProviderConfig(
                    provider=provider,
                    model=stt_conf.get("model", ""),
                    language=stt_conf.get("language") or config.language,
                    api_key_reference=stt_conf.get("api_key_reference", ""),
                    extra=stt_conf.get("extra", {}),
                )
            ))
        result = await holder[0].transcribe(
            pcm, sample_rate=sample_rate,
            language=stt_conf.get("language") or language or config.language,
        )
        return result.text or ""

    return _transcribe


def build_tts_service(
    config: ResolvedBotConfig,
    *,
    recorder: SessionRecorder,
    sample_rate: int = 24000,
    latency=None,
    naturalness=None,
):
    """TTS service from bot config: streaming router or segmented fallback."""
    tts_conf = config.tts or {}
    provider = tts_conf.get("provider") or "sarvam"

    # The catalog marks models without realtime WebSocket support (e.g.
    # ElevenLabs eleven_v3) with streaming=False at config resolution; those
    # synthesize segment-by-segment over REST like any non-streaming provider.
    # Older cached snapshots without the flag keep the streaming path.
    model_streams = tts_conf.get("streaming", True)
    if is_streaming_tts_provider(provider) and model_streams:
        return StreamingTTSRouter(
            tts_config=tts_conf,
            language=config.language,
            speed=config.speed,
            pause_ms=config.pause_ms,
            energy=config.energy,
            sample_rate=sample_rate,
            recorder=recorder,
            latency=latency,
            naturalness=naturalness,
            # Rare planner-gated breath between sentences (pause mode),
            # matched to the engine voice's gender — same clips as the
            # pre-reply latency filler.
            filler_library=get_filler_library(),
        )

    # Delivery tuning for the segmented REST path: canonical speed overrides
    # legacy pace/speed params and Energy maps onto documented model controls
    # (same shared helper as the streaming router and previews).
    extra = apply_delivery_params(
        provider,
        tts_conf.get("model") or "",
        {**(tts_conf.get("extra") or {}), **(tts_conf.get("settings") or {})},
        speed=config.speed,
        energy=config.energy,
    )
    tts_provider = get_tts_provider(
        ProviderConfig(
            provider=provider,
            model=tts_conf.get("model", ""),
            voice=tts_conf.get("voice", ""),
            language=config.language,
            api_key_reference=tts_conf.get("api_key_reference", ""),
            extra=extra,
        )
    )
    return EchoTTSService(
        tts_provider,
        voice=tts_conf.get("voice") or None,
        language=config.language,
        speed=config.speed,
        pause_ms=config.pause_ms,
        sample_rate=sample_rate,
        recorder=recorder,
        model=tts_conf.get("model") or "",
        provider_name=provider,
        naturalness=naturalness,
    )


def build_latency_filler(
    naturalness: SpeechNaturalnessPlanner,
    *,
    sample_rate: int,
    recorder=None,
    library=None,
    cue_library=None,
) -> LatencyFillerProcessor | None:
    """The gap-cover processor for one call, or None when the resolved
    human-speech config turns latency fillers off (no processor, no cost).

    Sits between the TTS service and the output transport; the brain arms it
    per dispatched turn with the active voice's gender. Config already comes
    merged platform -> tenant -> bot and bounds-clamped from the planner.
    With ``latency_filler_ladder`` on, the breath is followed on a long wait
    by voiced cues rendered once per voice (voice_runtime.voiced_cues).
    """
    if not naturalness.latency_fillers_enabled:
        return None
    ladder = naturalness.latency_filler_ladder_enabled
    return LatencyFillerProcessor(
        delay_ms=naturalness.latency_filler_delay_ms,
        library=library if library is not None else get_filler_library(),
        sample_rate=sample_rate,
        recorder=recorder,
        cue_library=(
            (cue_library if cue_library is not None else get_voiced_cue_library())
            if ladder else None
        ),
        hmm_after_ms=naturalness.latency_filler_hmm_ms if ladder else None,
        spoken_after_ms=naturalness.latency_filler_spoken_ms if ladder else None,
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
    customer_context=None,
    runtime_context=None,
    transport_kind: str = "browser",
    previous_memory=None,
    guardrails=None,
) -> tuple[PipelineWorker, ConversationBrain]:
    """Assemble the Pipecat pipeline for one call session."""
    # Deepgram Flux runs its own model-integrated turn detection server-side:
    # its adapter emits the user speech boundaries and the finalized turn
    # transcript itself, so the local Silero VAD and its turn strategies are
    # not built at all — running both would double every start/stop signal.
    stt_provider_code = (config.stt or {}).get("provider") or "sarvam"
    provider_owns_turns = stt_provider_code == "deepgram"

    turn = resolve_turn_detection(config, transport_kind)
    gate_conf = resolve_noise_gate(config, transport_kind)
    barge_in_min_words = int(round(turn["barge_in_min_words"]))

    # Created before the STT/TTS services: the TTS router stamps synthesis
    # request/first-byte, and a turn-authoritative STT (Flux) stamps the
    # physical speech boundaries — all on the same per-call tracker.
    tracker = TurnLatencyTracker(session_id=recorder.session_id)
    # One naturalness planner per call, shared by the brain (turn prefaces,
    # backchannels) and the TTS router (per-sentence pause/rate variation) so
    # variant no-repeat state and telemetry stay coherent. Resolved config
    # comes fully merged from bot_config (platform -> tenant -> bot).
    naturalness = SpeechNaturalnessPlanner(
        config.human_speech,
        config_sources=config.human_speech_sources,
    )
    # Local Silero owns speech boundaries in the normal pipeline. Enabling
    # Sarvam server VAD at the same time produces duplicate start/stop frames;
    # a normal pause can then look like barge-in and cancel the LLM/TTS reply.
    stt = build_stt_service(
        config,
        sample_rate=stt_sample_rate,
        recorder=recorder,
        use_provider_vad=not use_vad,
        latency=tracker,
        barge_in_min_words=barge_in_min_words,
        prefer_primary_language=transport_kind == "telephony",
    )
    tts = build_tts_service(
        config, recorder=recorder, sample_rate=tts_sample_rate, latency=tracker,
        naturalness=naturalness,
    )
    llm_provider = build_llm_provider(config)
    # Latency filler: a gender-matched breath from pre-rendered audio when a
    # dispatched reply has not started speaking within the configured delay,
    # cut the instant reply audio arrives (voice_runtime.latency_filler).
    latency_filler = build_latency_filler(
        naturalness, sample_rate=tts_sample_rate, recorder=recorder,
    )
    # The gate is the brain's source of caller audio energy for the transcript
    # quality gate; None when gating is disabled (the gate's signals then simply
    # do not contribute to a verdict).
    audio_gate = (
        CallerAudioGate(
            noise_margin_db=gate_conf["noise_margin_db"],
            min_speech_ms=gate_conf["min_speech_ms"],
            echo_min_speech_ms=gate_conf["echo_min_speech_ms"],
            hangover_ms=gate_conf["hangover_ms"],
            preroll_ms=gate_conf["preroll_ms"],
            echo_margin_db=gate_conf["echo_margin_db"],
            echo_tail_ms=gate_conf["echo_tail_ms"],
            min_threshold_dbfs=gate_conf["min_threshold_dbfs"],
        )
        # The gate substitutes silence for sub-floor audio (it never drops
        # frames), so it composes with a provider-side turn detector too:
        # Flux keeps receiving a continuous stream, minus the line noise
        # that would otherwise open phantom turns.
        if (use_vad or provider_owns_turns) and gate_conf["enabled"] >= 0.5
        else None
    )
    if latency_filler is not None and audio_gate is not None:
        # A voiced ladder cue ("हम्म…") is bot audio the gate's echo guard
        # does not see (no bot-speaking state flips for it): shield its echo
        # the way mid-caller-turn backchannels are shielded.
        latency_filler.cue_window_hook = (
            lambda active: audio_gate.begin_backchannel_window()
            if active else audio_gate.end_backchannel_window()
        )
    brain = ConversationBrain(
        config=config,
        llm=llm_provider,
        recorder=recorder,
        knowledge_service=knowledge_service,
        workflow_engine=workflow_engine,
        client_info=client_info,
        call_context=call_context,
        customer_context=customer_context,
        runtime_context=runtime_context,
        finalize_grace=turn["finalize_grace"],
        finalize_settle=turn["finalize_settle"],
        complete_endpoint=turn["complete_endpoint"],
        short_reply_endpoint=turn["short_reply_endpoint"],
        latency=tracker,
        audio_gate=audio_gate,
        authoritative_eot=provider_owns_turns,
        previous_memory=previous_memory,
        guardrails=guardrails,
        naturalness=naturalness,
        # Identifier batch recovery (voice_runtime.identifier_capture): at
        # most one REST transcription of bounded retained audio, only when a
        # streaming identifier came out invalid. Lazy — no client is built
        # unless a recovery runs.
        batch_transcriber=build_batch_transcriber(config),
        # No-response ladder: first-prompt wait, retry interval and prompt
        # count are the tenant's Turn Detection values (per transport); the
        # post-hold grace remains a platform setting.
        silence_policy=resolve_silence_policy(turn),
        latency_filler=latency_filler,
    )
    processors = [transport.input()]
    if audio_gate is not None:
        # Ahead of the VAD on purpose: background noise the gate suppresses can
        # never start a user turn, interrupt the bot, or reach the STT.
        processors.append(audio_gate)
    if use_vad and not provider_owns_turns:
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
        # Physical speech boundaries are only visible here: the UserTurnProcessor
        # downstream consumes the VAD frames, so the brain cannot time true
        # end-of-speech itself.
        processors.append(VADLatencyProbe(tracker))
    # STT must receive VADUserStoppedSpeakingFrame directly. Sarvam uses that
    # frame to flush its streaming socket; placing UserTurnProcessor first
    # consumed the control frame and left telephony transcripts waiting for
    # the provider's roughly 60-second server-side endpoint.
    processors.append(stt)
    if provider_owns_turns:
        # Turn boundaries, interruption and the word-confirmed barge-in gate
        # are all produced by the STT adapter itself (Flux TurnInfo events);
        # the turn processor only defers to those external signals.
        user_turn_strategies = ExternalUserTurnStrategies()
    else:
        # Barge-in policy: while the bot is quiet VAD starts the user turn
        # (fast path, unchanged); while it is SPEAKING an interruption must be
        # confirmed by a transcript of >= barge_in_min_words words — VAD alone
        # let ambient speech reaching the mic cancel replies mid-word (see
        # voice_runtime.barge_in). 0 restores pure-VAD interruption.
        start_strategy = (
            WordConfirmedBargeInStrategy(
                min_words=barge_in_min_words,
                # Sustained-speech fallback: Sarvam produces no interim
                # transcripts, so without this a caller who keeps talking
                # could never interrupt (the word gate's arbiter only exists
                # after a VAD stop + flush).
                vad_fallback_secs=turn["barge_in_vad_fallback_secs"],
            )
            if barge_in_min_words > 0
            else VADUserTurnStartStrategy()
        )
        user_turn_strategies = UserTurnStrategies(
            start=[start_strategy],
            # wait_for_transcript must be False: transcripts are consumed by
            # the brain downstream and never reach the turn processor, so
            # waiting for one only ever hits the 5s fallback — which also
            # blocked barge-in (a new turn can't start while the previous
            # one is stuck open). The brain gates the LLM on the resulting
            # UserStoppedSpeakingFrame, so this timeout IS the pause window
            # a caller gets before the bot takes the turn.
            stop=[SpeechTimeoutUserTurnStopStrategy(
                # The strategy's timer starts AT the VAD stop, which itself
                # required stop_secs of silence — charging both stacked the
                # windows (0.9 s effective on telephony where 0.7 s was
                # configured). The caller's total pause budget IS
                # user_speech_timeout; deduct the silence already spent.
                user_speech_timeout=max(
                    0.2, turn["user_speech_timeout"] - turn["stop_secs"]
                ),
                wait_for_transcript=False,
            )],
        )
    processors.append(UserTurnProcessor(user_turn_strategies=user_turn_strategies))
    processors += [brain, tts]
    if latency_filler is not None:
        # After the TTS service so it sees reply audio the moment it exists
        # (cut point), before the transport so its own chunks reach the wire.
        processors.append(latency_filler)
    processors.append(transport.output())

    if get_settings().voice_call_recording_enabled:
        # Sits after transport.output() so it observes exactly the frames that
        # were sent. Stereo WAV: caller on the left channel, bot on the right.
        # The CALLER stream is the master clock (see AlignedStereoRecorder):
        # pipecat's AudioBufferProcessor advanced the timeline on the arrival
        # clock of bot audio, and transports that push TTS faster than real
        # time inflated recordings well past the session's wall-clock length.
        recording_writer = CallRecordingWriter(recorder)
        audiobuffer = AlignedRecordingProcessor(
            writer=recording_writer, sample_rate=stt_sample_rate,
        )
        recording_writer.audiobuffer = audiobuffer
        # The recorder drives the final flush at call end (writer.close) —
        # teardown does not reliably deliver Cancel/End frames to processors
        # sitting after the output transport.
        recorder.recording_writer = recording_writer
        processors.append(audiobuffer)

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
