"""Smart Turn end-of-turn detection (pipecat ``LocalSmartTurnAnalyzerV3``), gated.

The fixed pause window (``user_speech_timeout``) decides the end of a caller
turn today and costs ~0.7 s p50 on live telephony. Smart Turn v3.2 is an
8 MB int8 ONNX model, bundled with pipecat >= 0.0.99, that predicts from the
audio itself whether the caller has finished — in 10–100 ms on CPU, run by
pipecat in the analyzer's own thread pool (never on the event loop).

How it is wired (``voice_runtime.pipeline.build_user_turn_stop_strategies``):

- ``TurnAnalyzerUserTurnStopStrategy`` + the analyzer run in the
  ``UserTurnProcessor`` next to the existing ``FinalBoundedTurnStopStrategy``.
  Whichever stop strategy triggers first ends the turn, so the pause window
  stays the FALLBACK: a turn the model never declares complete still closes
  at ``user_speech_timeout`` exactly as before. The analyzer's own silence
  cut-off is set to the same window, so it cannot end a turn earlier than the
  window on silence alone — only a model verdict can.
- The brain's lexical complete-thought shortcut (``voice_runtime.endpointing``)
  is untouched and still fires first for "haan" / "nahi" and closed sentences.

Gate: tenant ``turn_detection.smart_turn`` (0 = off, 1 = auto, 2 = on; the
numeric form every turn-detection field uses) and the process-wide kill
switch ``SMART_TURN_ENABLED`` (shared.config). ``auto`` enables the model
only for a bot whose primary language the bundled model supports.

Supported languages: the bundled ``smart-turn-v3.2-cpu.onnx`` carries no
language metadata (its ONNX custom metadata holds only quantizer fields) and
the pipecat wheel ships no model card, so the list below is the one published
in the model's README (https://github.com/pipecat-ai/smart-turn, "Support for
23 languages", read 2026-10-22). Tamil and Malayalam are NOT in it; such bots
keep the fixed window under ``auto``.
"""

from __future__ import annotations

import logging
import threading
from importlib import resources as _resources

from pipecat.audio.turn.smart_turn.base_smart_turn import BaseSmartTurn, SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)

logger = logging.getLogger(__name__)

SMART_TURN_MODEL_NAME = "smart-turn-v3.2-cpu.onnx"
SMART_TURN_MODEL_PACKAGE = "pipecat.audio.turn.smart_turn.data"
SMART_TURN_LANGUAGES_SOURCE = "https://github.com/pipecat-ai/smart-turn (README, 2026-10-22)"
# ISO 639-1 base codes of the 23 languages the model card lists: Arabic,
# Bengali, Chinese, Danish, Dutch, German, English, Finnish, French, Hindi,
# Indonesian, Italian, Japanese, Korean, Marathi, Norwegian, Polish,
# Portuguese, Russian, Spanish, Turkish, Ukrainian, Vietnamese.
SMART_TURN_LANGUAGES: frozenset[str] = frozenset({
    "ar", "bn", "zh", "da", "nl", "de", "en", "fi", "fr", "hi", "id", "it",
    "ja", "ko", "mr", "no", "pl", "pt", "ru", "es", "tr", "uk", "vi",
})

MODE_OFF = 0
MODE_AUTO = 1
MODE_ON = 2
_MODE_WORDS = {"off": MODE_OFF, "auto": MODE_AUTO, "on": MODE_ON}
_MODE_NAMES = {MODE_OFF: "off", MODE_AUTO: "auto", MODE_ON: "on"}


def smart_turn_mode_from_setting(value: object) -> int:
    """0/1/2 or ``off``/``auto``/``on`` → mode; anything else is ``auto``."""
    if isinstance(value, str):
        return _MODE_WORDS.get(value.strip().lower(), MODE_AUTO)
    try:
        if isinstance(value, bool):
            raise TypeError
        mode = int(round(float(value)))
    except (TypeError, ValueError):
        return MODE_AUTO
    return mode if mode in _MODE_NAMES else MODE_AUTO


def mode_name(mode: int) -> str:
    return _MODE_NAMES.get(mode, "auto")


def language_supported(locale: str | None) -> bool:
    """Whether the bot's primary language (``hi-IN`` → ``hi``) is in the model card."""
    base = (locale or "").split("-")[0].split("_")[0].strip().lower()
    return bool(base) and base in SMART_TURN_LANGUAGES


def smart_turn_decision(mode: int, language: str | None, *, enabled: bool) -> tuple[bool, str]:
    """``(use_smart_turn, reason)`` for one call."""
    if not enabled:
        return False, "kill_switch"
    if mode == MODE_OFF:
        return False, "tenant_off"
    if mode == MODE_ON:
        return True, "tenant_on"
    if language_supported(language):
        return True, "auto_supported_language"
    return False, "auto_unsupported_language"


# ── one ONNX session per process ─────────────────────────────────────────────
# pipecat builds a fresh InferenceSession per analyzer instance (one per call);
# the 8 MB model is read-only and ORT sessions are thread-safe for run(), so
# every call shares one session. Per-call state (audio buffer, timers) stays
# in the analyzer instance.
_SESSION_LOCK = threading.Lock()
_SESSION = None


def model_path() -> str:
    return str(_resources.files(SMART_TURN_MODEL_PACKAGE).joinpath(SMART_TURN_MODEL_NAME))


def shared_session():
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            _SESSION = ort.InferenceSession(model_path(), sess_options=options)
            logger.info("smart-turn: loaded %s (shared session)", SMART_TURN_MODEL_NAME)
        return _SESSION


class SharedSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """pipecat's v3 analyzer on the process-wide session (no per-call model load).

    Inference stays in ``BaseSmartTurn``'s thread pool (``analyze_end_of_turn``
    → ``run_in_executor``), off the event loop.
    """

    def __init__(self, *, sample_rate: int | None = None, params: SmartTurnParams | None = None):
        BaseSmartTurn.__init__(self, sample_rate=sample_rate, params=params)
        self._log_data = False
        self._session = shared_session()


class EndpointReportingSmartTurnStrategy(TurnAnalyzerUserTurnStopStrategy):
    """``TurnAnalyzerUserTurnStopStrategy`` that tells the latency tracker how
    the turn ended (``endpoint_by="smart_turn"``) and what the model predicted."""

    def __init__(self, *, tracker=None, recorder=None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tracker = tracker
        self._recorder = recorder

    async def _handle_prediction_result(self, result) -> None:
        if result is not None and self._tracker is not None:
            self._tracker.note_smart_turn(
                probability=float(getattr(result, "probability", 0.0) or 0.0),
                is_complete=bool(getattr(result, "is_complete", False)),
                inference_ms=float(getattr(result, "e2e_processing_time_ms", 0.0) or 0.0),
            )
        await super()._handle_prediction_result(result)

    async def trigger_user_turn_stopped(self, **kwargs) -> None:
        if self._tracker is not None:
            self._tracker.mark_endpoint_by("smart_turn")
        await super().trigger_user_turn_stopped(**kwargs)


def build_smart_turn_strategy(
    *,
    mode: int,
    language: str | None,
    enabled: bool,
    user_speech_timeout: float,
    wait_for_transcript: bool,
    tracker=None,
    recorder=None,
) -> EndpointReportingSmartTurnStrategy | None:
    """The Smart Turn stop strategy for one call, or None when gated off.

    The decision is recorded on the call's event stream (``smart_turn_gate``)
    either way, so a call's endpointing mode is never a guess afterwards.
    """
    use, reason = smart_turn_decision(mode, language, enabled=enabled)
    if recorder is not None:
        recorder.add_event(
            "smart_turn_gate", enabled=use, reason=reason, mode=mode_name(mode),
            language=language, model=SMART_TURN_MODEL_NAME if use else None,
        )
    if not use:
        return None
    analyzer = SharedSmartTurnAnalyzer(
        params=SmartTurnParams(
            # The analyzer's own silence cut-off equals the tenant pause
            # window: silence alone ends the turn no earlier than today.
            stop_secs=max(0.3, float(user_speech_timeout)),
        ),
    )
    return EndpointReportingSmartTurnStrategy(
        turn_analyzer=analyzer,
        # Sarvam finals are marked ``finalized``: a COMPLETE verdict ends the
        # turn the moment the final lands (or at the STT safety net).
        wait_for_transcript=wait_for_transcript,
        tracker=tracker,
        recorder=recorder,
    )


__all__ = [
    "MODE_AUTO", "MODE_OFF", "MODE_ON", "SMART_TURN_LANGUAGES", "SMART_TURN_MODEL_NAME",
    "EndpointReportingSmartTurnStrategy", "SharedSmartTurnAnalyzer",
    "build_smart_turn_strategy", "language_supported", "mode_name",
    "smart_turn_decision", "smart_turn_mode_from_setting",
]
