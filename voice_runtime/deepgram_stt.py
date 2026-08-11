"""Deepgram Flux realtime STT adapter (``wss://api.deepgram.com/v2/listen``).

Wraps pipecat's :class:`DeepgramFluxSTTService` (one persistent WebSocket per
call, model-integrated conversational turn detection) and adapts its protocol
to EchoSphere's normalized STT event contract:

===================  ====================================================
Flux wire event      EchoSphere pipeline event
===================  ====================================================
``StartOfTurn``      ``UserStartedSpeakingFrame`` (+ interruption, word-
                     confirmed while the bot is audibly speaking — same
                     policy as ``WordConfirmedBargeInStrategy``)
``Update``           interim transcript growth; confirms a held barge-in
``EagerEndOfTurn``   ``STTEagerEndOfTurnFrame`` (speculative work only)
``TurnResumed``      ``STTTurnResumedFrame`` (cancel speculative work)
``EndOfTurn``        ``TranscriptionFrame(finalized=True, language=…)``
                     then ``UserStoppedSpeakingFrame``
``Error``/close      recorder event + pipecat error path (reconnect-safe)
===================  ====================================================

Design constraints this file owns:

- **Barge-in parity.** Upstream Flux interrupts the bot on ANY ``StartOfTurn``;
  on a phone line that reintroduces the noise-triggered barge-in the word gate
  eliminated. While the bot is audibly speaking, the turn start (and the
  interruption it implies) is HELD until Flux has transcribed at least
  ``barge_in_min_words`` words; a backchannel ("हाँ", "hmm") therefore never
  chops a reply mid-word, while a real "एक मिनट रुकिए" lands as soon as its
  words do — typically from an ``Update`` well before end of turn.
- **Latency truth.** Flux reports per-word times on the stream's own audio
  clock, so end-of-speech is stamped from the last word's ``end`` offset
  rather than message arrival — the ``endpoint`` span then honestly measures
  Flux's turn-decision time plus our dispatch, not just receipt time.
- **Billing.** Deepgram bills streamed audio; the PCM byte count actually sent
  is flushed into the recorder per committed turn (basis ``pcm``), never
  derived from transcript metadata.
- **Framing.** Telephony audio arrives in 20 ms frames; Flux recommends
  ~80 ms chunks, so input is coalesced before sending. The upstream watchdog
  still injects silence if the pipeline stops delivering audio mid-turn.
"""

import logging
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

from voice_runtime.frames import STTEagerEndOfTurnFrame, STTTurnResumedFrame

logger = logging.getLogger(__name__)

# Flux input framing recommendation (see module docstring).
_DEFAULT_CHUNK_MS = 80
# A caller cannot plausibly have stopped speaking further back than this from
# the message that reports it; larger offsets mean a stalled audio clock.
_MAX_SPEECH_END_LOOKBACK_S = 3.0


class EchoDeepgramFluxSTTService(DeepgramFluxSTTService):
    """Deepgram Flux STT with EchoSphere turn/barge-in/billing semantics."""

    def __init__(
        self,
        *,
        barge_in_min_words: int = 2,
        recorder=None,
        latency=None,
        chunk_ms: int = _DEFAULT_CHUNK_MS,
        **kwargs,
    ):
        # ``should_interrupt=False``: interruption policy is owned here (word
        # confirmed), never by the upstream service's start-of-turn handler.
        kwargs.pop("should_interrupt", None)
        super().__init__(should_interrupt=False, **kwargs)
        self._barge_in_min_words = max(0, int(barge_in_min_words))
        self._recorder = recorder
        self._latency = latency
        self._chunk_ms = max(20, int(chunk_ms))
        # Word-confirmed barge-in state (mirrors WordConfirmedBargeInStrategy).
        self._bot_audible = False
        self._turn_start_held = False
        self._turn_start_announced = False
        # Billing: PCM bytes actually sent since the last usage flush.
        self._bytes_sent_unbilled = 0
        self._bytes_sent_total = 0
        self._send_buffer = bytearray()
        self._turn_counter = 0

    # ── pipeline plumbing ─────────────────────────────────────────────────

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # Bot-speaking boundaries travel upstream from the output transport;
        # they are the arbiter for whether a Flux StartOfTurn is a barge-in.
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_audible = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_audible = False
            if self._turn_start_held and not self._turn_start_announced:
                # The caller began speaking over the bot's final syllables and
                # the word gate held the turn; with the bot now quiet there is
                # nothing to protect — open the turn (no interruption
                # broadcast: nothing is playing to interrupt).
                self._turn_start_announced = True
                await self.broadcast_frame(UserStartedSpeakingFrame)
        await super().process_frame(frame, direction)

    @property
    def _effective_sample_rate(self) -> int:
        # self.sample_rate resolves at StartFrame; before that the requested
        # init rate is the truth (byte-clock math must never divide by zero).
        return self.sample_rate or self._init_sample_rate or 8000

    @property
    def _chunk_bytes(self) -> int:
        return int(self._effective_sample_rate * 2 * self._chunk_ms / 1000)

    async def run_stt(self, audio: bytes):
        """Coalesce pipeline audio into ~chunk_ms sends (Flux recommendation).

        Telephony frames are 20 ms; sending each one quadruples message rate
        for no accuracy gain. Bytes are counted when actually sent, so the
        billing flush reflects exactly what Deepgram processed.
        """
        if not audio:
            yield None
            return
        self._send_buffer.extend(audio)
        # While the bot is audibly speaking, every millisecond of coalescing
        # delays the words that could confirm a barge-in — ship each frame as
        # it arrives; the ~80 ms batching is a bandwidth nicety, not a
        # requirement.
        if len(self._send_buffer) < self._chunk_bytes and not self._bot_audible:
            yield None
            return
        chunk = bytes(self._send_buffer)
        self._send_buffer.clear()
        sent_ok = True
        async for frame in super().run_stt(chunk):
            if frame is not None:
                sent_ok = False
            yield frame
        if sent_ok:
            self._bytes_sent_unbilled += len(chunk)
            self._bytes_sent_total += len(chunk)

    async def _send_silence(self, duration_secs: float = 0.5):
        await super()._send_silence(duration_secs)
        # Watchdog silence is streamed (and billed) audio too.
        silence_bytes = int(self._effective_sample_rate * duration_secs) * 2
        self._bytes_sent_unbilled += silence_bytes
        self._bytes_sent_total += silence_bytes

    def _flush_stt_usage(self) -> None:
        """Fold the audio streamed since the last flush into call usage."""
        if self._recorder is None or self._bytes_sent_unbilled <= 0:
            return
        seconds = self._bytes_sent_unbilled / (self._effective_sample_rate * 2)
        self._bytes_sent_unbilled = 0
        add_usage = getattr(self._recorder, "add_stt_usage", None)
        if add_usage is not None:
            add_usage(seconds=seconds, basis="pcm")

    # ── Flux turn lifecycle ───────────────────────────────────────────────

    async def _handle_start_of_turn(self, transcript: str):
        self._user_is_speaking = True
        if self._latency is not None:
            self._latency.mark_speech_started()
        await self.start_metrics()
        await self._call_event_handler("on_start_of_turn", transcript)
        if self._bot_audible and self._barge_in_min_words > 0:
            # Bot is audibly speaking: hold the turn start until the
            # transcript proves this is the caller interrupting, not noise or
            # a backchannel (word-confirmed barge-in policy).
            self._turn_start_held = True
            self._turn_start_announced = False
            await self._maybe_confirm_barge_in(transcript)
            return
        self._turn_start_held = False
        self._turn_start_announced = True
        await self.broadcast_frame(UserStartedSpeakingFrame)
        await self.broadcast_interruption()

    async def _maybe_confirm_barge_in(self, transcript: str) -> None:
        """Release a held turn start once enough words are transcribed."""
        if not self._turn_start_held or self._turn_start_announced:
            return
        words = len((transcript or "").split())
        if words < self._barge_in_min_words:
            return
        logger.info(
            "deepgram-flux barge-in confirmed by transcript (%d words >= %d)",
            words, self._barge_in_min_words,
        )
        self._turn_start_announced = True
        await self.broadcast_frame(UserStartedSpeakingFrame)
        await self.broadcast_interruption()

    async def _handle_update(self, transcript: str):
        await self._maybe_confirm_barge_in(transcript)
        await super()._handle_update(transcript)

    async def _handle_eager_end_of_turn(self, transcript: str, data: dict):
        await self._maybe_confirm_barge_in(transcript)
        if self._latency is not None:
            self._latency.mark_eager_eot()
        # Base behaviour first (InterimTranscriptionFrame for UI parity)…
        await super()._handle_eager_end_of_turn(transcript, data)
        # …then the normalized speculative-work signal for the brain.
        language = self._primary_detected_language(data)
        await self.push_frame(STTEagerEndOfTurnFrame(
            text=(transcript or "").strip(),
            language=getattr(language, "value", "") or "",
        ))

    async def _handle_turn_resumed(self, event: str):
        await super()._handle_turn_resumed(event)
        await self.push_frame(STTTurnResumedFrame())

    async def _handle_end_of_turn(self, transcript: str, data: dict):
        self._turn_counter += 1
        if self._turn_start_held and not self._turn_start_announced:
            # The whole utterance happened while the bot was speaking and
            # never earned an interruption (a backchannel). The transcription
            # still flows — the brain holds it until the reply finishes — but
            # no turn start / interruption is fabricated after the fact.
            words = len((transcript or "").split())
            if words >= self._barge_in_min_words > 0:
                await self._maybe_confirm_barge_in(transcript)
        self._turn_start_held = False
        self._mark_speech_end_from_words(data)
        await super()._handle_end_of_turn(transcript, data)
        self._flush_stt_usage()

    def _mark_speech_end_from_words(self, data: dict) -> None:
        """Stamp physical end-of-speech from Flux's own word timings.

        The EndOfTurn message arrives AFTER the end-of-turn decision window;
        the caller actually stopped speaking at the last word's ``end`` offset
        on the stream's audio clock. Backdating the mark by the difference
        keeps the ``endpoint`` span honest about provider decision time.
        """
        if self._latency is None:
            return
        try:
            words = data.get("words") or []
            last_end = float(words[-1]["end"]) if words else None
        except (KeyError, TypeError, ValueError, IndexError):
            last_end = None
        lookback = 0.0
        if last_end is not None and self._bytes_sent_total > 0:
            audio_clock = self._bytes_sent_total / (self._effective_sample_rate * 2)
            lookback = min(
                max(0.0, audio_clock - last_end), _MAX_SPEECH_END_LOOKBACK_S
            )
        self._latency.mark_speech_stopped_ago(lookback)

    # ── failure visibility / teardown ─────────────────────────────────────

    async def _handle_fatal_error(self, data: dict):
        if self._recorder is not None:
            self._recorder.add_event(
                "stt_provider_error",
                provider="deepgram",
                # Provider error strings are operational metadata, never
                # caller content; still truncated defensively.
                error=str(data.get("error", "unknown"))[:200],
                code=str(data.get("code", ""))[:60] or None,
            )
        await super()._handle_fatal_error(data)

    async def _report_error(self, error):
        if self._recorder is not None:
            self._recorder.add_event(
                "stt_provider_error",
                provider="deepgram",
                error=str(getattr(error, "error", error))[:200],
            )
        await super()._report_error(error)

    async def _disconnect(self):
        self._flush_stt_usage()
        self._send_buffer.clear()
        await super()._disconnect()
