"""Per-turn latency instrumentation for the realtime voice loop.

One :class:`TurnLatencyTracker` per call collects the spans that decide whether
a conversation feels natural. Marks are stamped from two places, because no
single processor sees the whole loop:

- :class:`VADLatencyProbe` sits between the VAD and the STT and stamps the
  physical speech boundaries (``VADUserStartedSpeakingFrame`` /
  ``VADUserStoppedSpeakingFrame``). Those frames are consumed by the
  ``UserTurnProcessor`` downstream, so the brain never sees them and cannot
  measure true end-of-speech on its own;
- the brain stamps everything from the STT finals onward, plus the bot-speaking
  boundaries it receives upstream from the output transport.

Spans (all milliseconds, all optional — a span whose marks did not both happen
is simply absent rather than guessed):

``bot_stop_to_speech``  bot finished speaking → caller speech detected
``speech``              caller speech start → speech end
``stt_final``           speech end → last final segment of the utterance
``endpoint``            speech end → turn dispatched (turn-detection dead time)
``llm_first_token``     turn dispatched → first LLM token
``tts_first_audio``     first LLM token → first bot audio on the wire
``response``            speech end → first bot audio (what the caller feels)

``endpoint`` is the span this platform controls end-to-end and the one that
regressions hide in: it is pure dead time added by turn detection, independent
of provider speed.

The spans above bracket the loop but lump our own work in with the providers':
``llm_first_token`` covers intent classification, the policy, tool calls AND
the model, so a slow turn cannot be attributed from it. These break that open,
and are reported alongside (never instead of) the spans above:

``classify``            turn dispatched → intent classification finished
``tool``                classification finished → verification tool finished
``llm_ttft``            LLM request sent → first token (pure provider time)
``tts_queue``           first LLM token → text handed to the TTS provider
``tts_ttfb``            TTS request sent → first audio byte (pure provider time)
``playout``             first TTS byte → first audio on the wire (our buffering)

Reading them: ``classify`` + ``tool`` + ``llm_ttft`` should account for nearly
all of ``llm_first_token``; whatever is left is orchestration overhead. Equally,
``tts_queue`` + ``tts_ttfb`` + ``playout`` should account for
``tts_first_audio``, and a large ``playout`` means audio is being buffered
rather than streamed.
"""

import logging
import time
from dataclasses import dataclass, field

from pipecat.frames.frames import (
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


def _ms(later: float | None, earlier: float | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier) * 1000, 1)


@dataclass
class TurnLatencyTracker:
    """Monotonic marks for the turn currently in flight.

    Marks are idempotent where a provider may repeat an event: the FIRST final
    and the FIRST bot-audio frame of a turn win, so a duplicate STT delivery or
    a second audio chunk cannot move an already-measured boundary.
    """

    session_id: str = ""
    bot_stopped_at: float | None = None
    # Resolved at mark_speech_started rather than derived at report time.
    bot_stop_gap_ms: float | None = None
    speech_started_at: float | None = None
    speech_stopped_at: float | None = None
    first_final_at: float | None = None
    last_final_at: float | None = None
    dispatched_at: float | None = None
    classified_at: float | None = None
    tool_done_at: float | None = None
    llm_request_at: float | None = None
    llm_first_token_at: float | None = None
    tts_request_at: float | None = None
    tts_first_byte_at: float | None = None
    bot_started_at: float | None = None
    # Set once the turn's spans have been reported, so a second bot-audio frame
    # (or a re-render after barge-in) does not emit the same turn twice.
    reported: bool = False
    counts: dict[str, int] = field(default_factory=dict)

    # ── marks ────────────────────────────────────────────────────────────
    def mark_bot_stopped_speaking(self) -> None:
        self.bot_stopped_at = time.monotonic()

    def mark_speech_started(self) -> None:
        # A new utterance begins. The response gap is fully determined at this
        # instant, so it is resolved here rather than left to be derived at
        # report time from a reference that may since have been superseded.
        bot_stopped = self.bot_stopped_at
        self.reset()
        self.speech_started_at = time.monotonic()
        self.bot_stop_gap_ms = _ms(self.speech_started_at, bot_stopped)

    def mark_speech_stopped(self) -> None:
        self.speech_stopped_at = time.monotonic()

    def mark_final(self) -> None:
        now = time.monotonic()
        if self.first_final_at is None:
            self.first_final_at = now
        self.last_final_at = now

    def mark_dispatched(self) -> None:
        self.dispatched_at = time.monotonic()

    def mark_classified(self) -> None:
        self.classified_at = time.monotonic()

    def mark_tool_done(self) -> None:
        self.tool_done_at = time.monotonic()

    def mark_llm_request(self) -> None:
        # A pre-first-token retry re-sends the request; the LAST attempt is the
        # one the first token actually belongs to, so this is not idempotent.
        self.llm_request_at = time.monotonic()

    def mark_llm_first_token(self) -> None:
        if self.llm_first_token_at is None:
            self.llm_first_token_at = time.monotonic()

    def mark_tts_request(self) -> None:
        """First synthesis dispatch of this turn (later sentences are inside it)."""
        if self.tts_request_at is None:
            self.tts_request_at = time.monotonic()

    def mark_tts_first_byte(self) -> None:
        """First audio byte back FROM the provider, before our resample/queue."""
        if self.tts_first_byte_at is None:
            self.tts_first_byte_at = time.monotonic()

    def mark_bot_started_speaking(self) -> None:
        if self.bot_started_at is None:
            self.bot_started_at = time.monotonic()
        # While the bot is speaking there is no completed bot-stop to measure a
        # response gap from: a caller who cuts in now is barging in, not
        # answering. Clearing this keeps a stale previous-turn timestamp from
        # being reported as this turn's think time.
        self.bot_stopped_at = None

    def count(self, name: str) -> None:
        """Tally a per-turn occurrence (rejected segments, merges, duplicates)."""
        self.counts[name] = self.counts.get(name, 0) + 1

    def reset(self) -> None:
        self.bot_stop_gap_ms = None
        self.speech_started_at = None
        self.speech_stopped_at = None
        self.first_final_at = None
        self.last_final_at = None
        self.dispatched_at = None
        self.classified_at = None
        self.tool_done_at = None
        self.llm_request_at = None
        self.llm_first_token_at = None
        self.tts_request_at = None
        self.tts_first_byte_at = None
        self.bot_started_at = None
        self.reported = False
        self.counts = {}

    # ── spans ────────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, float]:
        """Measured spans in milliseconds; absent marks are omitted."""
        spans = {
            "bot_stop_to_speech": self.bot_stop_gap_ms,
            "speech": _ms(self.speech_stopped_at, self.speech_started_at),
            "stt_final": _ms(self.last_final_at, self.speech_stopped_at),
            "endpoint": _ms(self.dispatched_at, self.speech_stopped_at),
            # Attribution inside llm_first_token.
            "classify": _ms(self.classified_at, self.dispatched_at),
            "tool": _ms(self.tool_done_at, self.classified_at),
            "llm_ttft": _ms(self.llm_first_token_at, self.llm_request_at),
            "llm_first_token": _ms(self.llm_first_token_at, self.dispatched_at),
            # Attribution inside tts_first_audio.
            "tts_queue": _ms(self.tts_request_at, self.llm_first_token_at),
            "tts_ttfb": _ms(self.tts_first_byte_at, self.tts_request_at),
            "playout": _ms(self.bot_started_at, self.tts_first_byte_at),
            "tts_first_audio": _ms(self.bot_started_at, self.llm_first_token_at),
            "response": _ms(self.bot_started_at, self.speech_stopped_at),
        }
        return {name: value for name, value in spans.items() if value is not None}

    def report(self) -> dict[str, float]:
        """Log the completed turn's spans once and return them."""
        spans = self.snapshot()
        self.reported = True
        if spans:
            logger.info(
                "turn[%s] latency %s",
                self.session_id,
                " ".join(f"{name}={value:.0f}ms" for name, value in spans.items()),
            )
        return spans


class VADLatencyProbe(FrameProcessor):
    """Pass-through probe stamping physical speech boundaries on the tracker.

    Placed directly after the VAD so it observes the VAD speech frames before
    the ``UserTurnProcessor`` consumes them. It never alters or withholds a
    frame — a probe must not be able to change call behaviour.
    """

    def __init__(self, tracker: TurnLatencyTracker, **kwargs) -> None:
        super().__init__(**kwargs)
        self._tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._tracker.mark_speech_started()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._tracker.mark_speech_stopped()
        await self.push_frame(frame, direction)
