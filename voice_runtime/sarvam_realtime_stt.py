"""Sarvam realtime STT (``/speech-to-text-realtime/ws``) behind a flag.

Pipecat ships ``SarvamRealtimeSTTService`` for Sarvam's newer realtime socket
(1.13 added the ``keyterms`` handshake, backoff reconnect and ``language``
in place of ``language_code``). Compared with the legacy ``/speech-to-text/ws`` streaming socket our
``EndpointedSarvamSTTService`` adapter uses, the realtime endpoint:

- emits true ``transcript.partial`` events during speech (the legacy socket
  only ever produces a segment final, which is why the legacy adapter has to
  flush the socket periodically to confirm a barge-in);
- takes its turn boundaries either from the client (``endpointing="manual"``:
  the pipeline's VAD frames become ``speech_start`` / ``speech_end``) or from
  Sarvam's own VAD (``endpointing="vad"``);
- marks every final ``finalized``, so the turn-stop strategy's bounded wait
  ends the moment the final arrives, exactly as with the legacy adapter;
- serves exactly two models, ``saaras:v3-realtime`` and ``saaras:v4``;
- accepts ``keyterms`` as a JSON array query parameter for ``saaras:v4`` and,
  unlike the legacy socket, ENFORCES the contract at the handshake (probed
  2026-10-06: close code 4000 for keyterms on ``saaras:v3-realtime``, for 51
  terms and for a 65-character term). The shared rule set in
  ``shared.providers.stt.sarvam_keyterms`` already keeps us inside it.

Two things measured on 2026-10-06 shape this adapter beyond upstream:

- **Manual endpointing only hears audio from ``speech_start`` on** (a few
  hundred ms of server pre-roll at most). Pipecat sends ``speech_start`` on
  ``VADUserStartedSpeakingFrame``, i.e. after Silero's ``start_secs`` and the
  caller-audio gate's sustained-speech requirement — late enough during a
  barge-in to lose the first word(s) ("Zepto se mera order…" came back as
  "Order abhi tak nahi aaya hai"). So this adapter keeps an utterance OPEN at
  all times: ``speech_start`` right after ``session.begin`` and again right
  after every ``speech_end``. Probed: a duplicate ``speech_start`` is
  harmless, 8 s of open silence before speech is fine, and back-to-back
  utterances each get their own final (190–280 ms after ``speech_end``).
- **Partials are not a stability reference.** Sarvam decodes partials before
  language/script normalisation (Roman-script or English running text for a
  Devanagari final), so the brain's interim-agreement score read them as an
  unstable transcript and, combined with bot-audio overlap, rejected every
  barge-in segment. Partials are tagged so they feed the client UI and the
  barge-in word gate only.

Enabled by ``SARVAM_REALTIME_STT=true`` (``shared.config.Settings``); the
pipeline falls back to the legacy adapter for a model the realtime endpoint
does not serve. Default OFF: this is an evaluation path.
"""

from __future__ import annotations

import logging
from typing import Any

from pipecat.frames.frames import InterimTranscriptionFrame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService

from shared.providers.stt.sarvam_keyterms import (
    normalize_keyterms,
    supports_keyterms,
)

logger = logging.getLogger(__name__)

#: Bot-configured Sarvam STT model → the name the realtime endpoint serves.
#: ``saaras:v3`` has a dedicated realtime build; ``saaras:v4`` is the same
#: name on both sockets (the endpoint rejects ``saaras:v4-realtime``).
REALTIME_MODEL_FOR: dict[str, str] = {
    "saaras:v3": "saaras:v3-realtime",
    "saaras:v3-realtime": "saaras:v3-realtime",
    "saaras:v4": "saaras:v4",
}

ENDPOINTING_MODES = ("manual", "vad")
STREAM_TYPES = ("balanced", "fast")


def realtime_model_for(model: str | None) -> str | None:
    """The realtime-endpoint model for a configured one, or None if unserved."""
    return REALTIME_MODEL_FOR.get((model or "").strip().lower())


def normalize_endpointing(value: object) -> str:
    """``manual`` unless the setting explicitly says ``vad``."""
    text = str(value or "").strip().lower()
    if text in ENDPOINTING_MODES:
        return text
    if text:
        logger.warning(
            "sarvam-realtime: unknown SARVAM_REALTIME_ENDPOINTING %r; using 'manual'", value,
        )
    return "manual"


def normalize_stream_type(value: object) -> str:
    """``balanced`` unless the setting explicitly says ``fast``."""
    text = str(value or "").strip().lower()
    if text in STREAM_TYPES:
        return text
    if text:
        logger.warning(
            "sarvam-realtime: unknown SARVAM_REALTIME_STREAM_TYPE %r; using 'balanced'", value,
        )
    return "balanced"


class RealtimeSarvamSTTService(SarvamRealtimeSTTService):
    """Pipecat's realtime service plus the model gate for key terms and the
    call-event evidence the call review needs.

    Transcription, endpointing, reconnection (pipecat >= 1.13: backoff
    reconnect with the open utterance re-announced) and the ``keyterms``
    handshake parameter are all upstream's. This subclass only refuses key
    terms for a model that does not support them (the endpoint closes the
    socket with code 4000 otherwise), keeps the utterance open under manual
    endpointing, and records session facts.
    """

    def __init__(
        self, *args, recorder=None, keyterms: list[str] | None = None, **kwargs
    ) -> None:
        terms = normalize_keyterms(keyterms)
        model = kwargs.get("settings").model if kwargs.get("settings") is not None else None
        if terms and not supports_keyterms(model):
            logger.warning(
                "sarvam-realtime: %d keyterm(s) configured but model %r does not "
                "support key-term biasing; not sent", len(terms), model,
            )
            terms = []
        # Upstream sends ``keyterms`` on the handshake whenever it is not None,
        # so an empty list must become None (never an empty JSON array).
        super().__init__(*args, keyterms=terms or None, **kwargs)
        self._recorder = recorder
        self._utterance_open = False
        self._reopen_count = 0

    @property
    def keyterms(self) -> list[str]:
        """Key terms that WILL be sent on the handshake (saaras:v4 only)."""
        return list(self._keyterms or [])

    @property
    def endpointing(self) -> str:
        """``manual`` (pipeline VAD drives the boundaries) or ``vad`` (Sarvam's)."""
        return self._endpointing

    @property
    def stream_type(self) -> str:
        return str(self._settings.stream_type)

    async def _open_utterance(self) -> None:
        """Manual endpointing: keep an utterance open so no audio is lost to
        the client VAD's start latency (see the module docstring)."""
        if self._endpointing != "manual":
            return
        if await self._send_json({"event": "speech_start"}):
            self._utterance_open = True
            self._reopen_count += 1

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._endpointing == "manual":
            # Upstream just sent ``speech_end`` for this boundary; re-open at
            # once so the next utterance is captured from its first sample.
            self._utterance_open = False
            await self._open_utterance()

    async def push_frame(self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if isinstance(frame, InterimTranscriptionFrame):
            setattr(frame, "_echosphere_stability_reference", False)
        await super().push_frame(frame, direction)

    async def _handle_message(self, message: dict[str, Any]):
        event = message.get("event")
        if self._recorder is not None:
            if event == "session.begin":
                self._recorder.add_event(
                    "stt_realtime_session",
                    request_id=message.get("request_id"),
                    model=str(self._settings.model),
                    endpointing=self._endpointing,
                    stream_type=self.stream_type,
                    keyterms=len(self._keyterms or []),
                    always_open=self._endpointing == "manual",
                )
            elif event == "error":
                self._recorder.add_event(
                    "stt_realtime_error",
                    code=message.get("code"),
                    fatal=message.get("is_fatal"),
                    message=str(message.get("message") or "")[:200],
                )
            elif event == "session.end":
                self._recorder.add_event(
                    "stt_realtime_session_end",
                    audio_duration_s=message.get("audio_duration_s"),
                )
        await super()._handle_message(message)
        if event == "session.begin":
            await self._open_utterance()
