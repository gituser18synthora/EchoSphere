"""Custom pipeline frames used between the brain and the TTS router.

DataFrames so they queue in-order with the token TextFrames they relate to.
"""

from dataclasses import dataclass, field

from pipecat.frames.frames import DataFrame, SystemFrame


@dataclass
class SwitchVoiceLanguageFrame(DataFrame):
    """Ask the TTS router to switch the active conversation locale.

    Emitted by the brain when the detected transcript language changes. The
    switch applies from the next bot reply; provider connections are reused
    where the mapping allows it.
    """

    language: str = ""


@dataclass
class TTSFlushHintFrame(DataFrame):
    """Ask the TTS router to flush buffered text mid-turn.

    Emitted by the brain when the LLM token stream pauses for longer than the
    configured interval, so already-buffered text starts rendering instead of
    waiting for the next sentence boundary.
    """

    reason: str = "llm_pause"


@dataclass
class STTEagerEndOfTurnFrame(DataFrame):
    """Provider predicts the caller's turn is over (not yet committed).

    Normalized from Deepgram Flux ``EagerEndOfTurn``. Carries the likely-final
    transcript so the brain can start SPECULATIVE orchestration work (decision
    prefetch) during the provider's end-of-turn confirmation window. Nothing
    speculative may produce audio: the turn is committed only by the final
    ``TranscriptionFrame`` (provider ``EndOfTurn``), and a following
    :class:`STTTurnResumedFrame` discards the speculation entirely.
    """

    text: str = ""
    language: str = ""


@dataclass
class STTTurnResumedFrame(DataFrame):
    """The caller kept talking after an eager end-of-turn prediction.

    Normalized from Deepgram Flux ``TurnResumed``. All speculative work started
    for the eager transcript must be cancelled — the utterance is still going
    and will be re-delivered in full with the real end of turn.
    """

    reason: str = "turn_resumed"


# Transport message the latency filler sends right after a clip it streamed as
# plain output audio (breath / voiced cue) has fully left the pipeline. The
# telephony serializers packetize outbound PCM into 200 ms frames and flush a
# partial packet only on BotStoppedSpeakingFrame — which plain audio never
# produces — so a clip's last <200 ms would otherwise sit in the buffer and
# play glued to the front of the NEXT utterance (heard on FreeSWITCH calls as
# the breath "repeating" right before the reply). The message rides the audio
# queue, so it reaches the serializer after the clip's last frame.
AUDIO_FLUSH_MESSAGE_TYPE = "audio_flush"


@dataclass
class ReplyMarkerFrame(DataFrame):
    """Tag the bot reply that FOLLOWS this frame for delivery tracking.

    Emitted by the brain right before a reply's ``LLMFullResponseStartFrame``
    whenever it needs to know if that reply was actually heard in full. The
    TTS router attaches the marker to the reply's generation and answers with
    a :class:`ReplyTTSCompleteFrame` when the generation finishes (all audio
    delivered) — an interrupted generation never answers.
    """

    marker: int = 0


@dataclass
class ReplyTTSCompleteFrame(SystemFrame):
    """Upstream: the marked reply's synthesis finished and all audio was sent.

    A SystemFrame so it reaches the brain immediately (upstream frames are not
    queued behind audio). ``failed`` marks a generation that produced no
    audio; the brain then never counts the reply as heard.
    """

    marker: int = 0
    failed: bool = False


@dataclass
class ReplyTTSInterruptedFrame(SystemFrame):
    """Upstream: the marked reply's synthesis was cut by a barge-in.

    ``sentences`` lists (chars, audio_seconds_delivered) per aggregated
    sentence in speaking order, so the brain can tell how many sentences had
    played by the time the caller spoke (a ticket readout whose facts played
    and only the closing question was cut WAS heard — cv_fd720f2e9024).
    """

    marker: int = 0
    sentences: list = field(default_factory=list)
