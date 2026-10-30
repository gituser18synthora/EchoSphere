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

Since 2026-10-23 the adapter also guards the always-open utterance against
the bot's own echo and against silence hallucination, and lets a transcript
through only with VAD evidence — see the constants below and
``voice_runtime.spoken_text``. ``build_stt_service`` pins a single-language
bot's language on this socket and forces ``mode=transcribe``: with
``language_code=auto`` the server renders Hindi speech as English.

Enabled by ``SARVAM_REALTIME_STT=true`` (``shared.config.Settings``); the
pipeline falls back to the legacy adapter for a model the realtime endpoint
does not serve. Default OFF: this is an evaluation path.
"""

from __future__ import annotations

import logging
from typing import Any

import time
from collections import deque

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService

from voice_runtime.sarvam_stt import _PRECONNECT_BUFFER_S

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

# What the always-open utterance must NOT be fed, and which transcripts may
# leave the adapter (measured 2026-10-23 by replaying live call
# cv_a3f4eee0d0d2's audio into the realtime socket):
#
# - Digital silence hallucinates. The caller-audio gate passes exact zeros
#   while it is shut; on an open utterance the server decoded 18 s of zeros
#   into partials ("However, whatever happens, happens...") from 4 s on and
#   a server-initiated FINAL at ~18 s, in a random language. Live, that final
#   (2 words, Tamil) confirmed a barge-in and cut the reply mid-sentence; its
#   partials landed in the caller's next turn as English junk. Zero frames
#   are therefore never sent; while nothing is sent the socket is kept alive
#   with Sarvam's ping instead of padding audio.
# - While the bot speaks (and for ``_ECHO_TAIL_S`` after it stops — the
#   reply keeps echoing on a phone leg) audio goes to the socket only while
#   Silero reports caller speech; the last ``_PRE_VAD_RING_S`` of guarded
#   audio is sent first so the gate's pre-roll (first syllable) survives.
# - A transcript leaves the adapter only with VAD evidence: the caller is
#   speaking now, or a ``speech_end`` we sent is still awaiting its final.
#   Anything else is the server's own invention and never reaches the
#   barge-in word gate or the brain.
# - In the echo window a transcript that is mostly the bot's recently spoken
#   words (voice_runtime.spoken_text) is the bot's own voice and is dropped.
_ECHO_TAIL_S = 0.6
_PRE_VAD_RING_S = 0.4
_PENDING_FINAL_S = 2.5
_SILENT_PING_S = 8.0


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
        self, *args, recorder=None, keyterms: list[str] | None = None,
        spoken_text=None, **kwargs,
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
        # Audio that arrives before the socket is open — and, under manual
        # endpointing, before the always-open utterance has been announced
        # (the server only transcribes audio sent after ``speech_start``) —
        # is held and sent with the first frame once both are true. Bounded.
        self._held_audio = bytearray()
        self._held_dropped_bytes = 0
        # Echo / silence guard state (see the module constants).
        self._spoken_text = spoken_text
        self._bot_speaking = False
        self._bot_stopped_at: float | None = None
        self._vad_open = False
        self._pending_finals: deque[float] = deque()
        self._pre_vad_ring = bytearray()
        self._last_sent_at = time.monotonic()
        self._guard_stats = {
            "silence_skipped_ms": 0.0, "guarded_skipped_ms": 0.0, "pings": 0,
            "partials_dropped": 0, "finals_dropped": 0, "echo_dropped": 0,
        }

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

    def _ready_for_audio(self) -> bool:
        if not self._is_websocket_open():
            return False
        return self._endpointing != "manual" or self._utterance_open

    def _hold_audio(self, audio: bytes) -> None:
        self._held_audio.extend(audio)
        cap = int((self.sample_rate or 16000) * 2 * _PRECONNECT_BUFFER_S)
        excess = len(self._held_audio) - cap
        if excess > 0:
            del self._held_audio[:excess]
            self._held_dropped_bytes += excess

    def _release_held_audio(self) -> bytes:
        held = bytes(self._held_audio)
        self._held_audio.clear()
        dropped, self._held_dropped_bytes = self._held_dropped_bytes, 0
        rate = self.sample_rate or 16000
        buffered_ms = round(len(held) / 2 / rate * 1000.0, 1)
        dropped_ms = round(dropped / 2 / rate * 1000.0, 1)
        logger.info(
            "sarvam-realtime: sending %.0f ms of caller audio held before the "
            "socket/utterance was ready (dropped %.0f ms beyond the %.0f s bound)",
            buffered_ms, dropped_ms, _PRECONNECT_BUFFER_S,
        )
        if self._recorder is not None:
            self._recorder.add_event(
                "stt_audio_buffered_before_connect",
                buffered_ms=buffered_ms, dropped_ms=dropped_ms,
            )
        return held

    def _audio_ms(self, byte_count: int) -> float:
        return byte_count / 2 / (self.sample_rate or 16000) * 1000.0

    def _echo_guarded(self, now: float | None = None) -> bool:
        """Whether the bot is speaking or stopped less than the echo tail ago."""
        if self._bot_speaking:
            return True
        if self._bot_stopped_at is None:
            return False
        return (now if now is not None else time.monotonic()) - self._bot_stopped_at < _ECHO_TAIL_S

    def _ring_audio(self, audio: bytes) -> None:
        self._pre_vad_ring.extend(audio)
        cap = int((self.sample_rate or 16000) * 2 * _PRE_VAD_RING_S)
        if len(self._pre_vad_ring) > cap:
            del self._pre_vad_ring[: len(self._pre_vad_ring) - cap]

    async def _keep_alive_while_silent(self, now: float) -> None:
        """Nothing is being sent: ping so the server does not idle the socket
        out (upstream's keepalive only counts audio frames it never sees)."""
        if now - self._last_sent_at < _SILENT_PING_S:
            return
        self._last_sent_at = now
        if await self._send_json({"event": "ping"}):
            self._guard_stats["pings"] += 1

    async def run_stt(self, audio: bytes):
        if not audio:
            yield None
            return
        if not any(audio):
            # The gate's silence (exact zeros): never fed to the open
            # utterance — the server hallucinates text from it.
            self._guard_stats["silence_skipped_ms"] += self._audio_ms(len(audio))
            if self._ready_for_audio():
                await self._keep_alive_while_silent(time.monotonic())
            yield None
            return
        if not self._ready_for_audio():
            self._hold_audio(audio)
            yield None
            return
        now = time.monotonic()
        if self._echo_guarded(now) and not self._vad_open:
            # The bot's own reply on the line (or caller speech Silero has
            # not confirmed yet): keep the last bit for the VAD onset.
            self._ring_audio(audio)
            self._guard_stats["guarded_skipped_ms"] += self._audio_ms(len(audio))
            await self._keep_alive_while_silent(now)
            yield None
            return
        prefix = b""
        if self._held_audio:
            prefix += self._release_held_audio()
        if self._pre_vad_ring:
            prefix += bytes(self._pre_vad_ring)
            self._pre_vad_ring.clear()
        self._last_sent_at = now
        async for frame in super().run_stt(prefix + audio):
            yield frame

    async def process_frame(self, frame, direction: FrameDirection):
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._bot_stopped_at = None
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_open = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_open = False
            self._pre_vad_ring.clear()
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._endpointing == "manual":
            # Upstream just sent ``speech_end`` for this boundary: its final
            # is now owed (``_PENDING_FINAL_S``). Re-open at once so the next
            # utterance is captured from its first sample.
            self._pending_finals.append(time.monotonic())
            self._utterance_open = False
            await self._open_utterance()

    async def _reconnect_websocket(self, attempt_number: int) -> bool:
        reconnected = await super()._reconnect_websocket(attempt_number)
        # The old session's utterance is gone with it: nothing is owed.
        self._pending_finals.clear()
        self._pre_vad_ring.clear()
        return reconnected

    def _purge_pending_finals(self, now: float) -> None:
        while self._pending_finals and now - self._pending_finals[0] > _PENDING_FINAL_S:
            self._pending_finals.popleft()

    def _transcript_drop_reason(self, frame) -> str | None:
        """Why this transcript must not leave the adapter, or None."""
        now = time.monotonic()
        self._purge_pending_finals(now)
        is_final = isinstance(frame, TranscriptionFrame)
        if not self._vad_open and not self._pending_finals:
            return "no_vad_speech"
        if is_final and self._pending_finals and not self._vad_open:
            self._pending_finals.popleft()      # the speech_end we sent is answered
        if (
            self._spoken_text is not None
            and self._echo_guarded(now)
            and self._spoken_text.is_echo(frame.text or "")
        ):
            return "echo_text"
        return None

    async def push_frame(self, frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)):
            reason = self._transcript_drop_reason(frame)
            if reason is not None:
                is_final = isinstance(frame, TranscriptionFrame)
                self._guard_stats["finals_dropped" if is_final else "partials_dropped"] += 1
                if reason == "echo_text":
                    self._guard_stats["echo_dropped"] += 1
                logger.info(
                    "sarvam-realtime: dropped %s (%s): %r",
                    "final" if is_final else "partial", reason, (frame.text or "")[:80],
                )
                if self._recorder is not None and (is_final or reason == "echo_text"):
                    self._recorder.add_event(
                        "stt_realtime_transcript_dropped",
                        reason=reason, final=is_final, words=len((frame.text or "").split()),
                        text=(frame.text or "")[:120], language=str(frame.language or ""),
                        bot_speaking=self._bot_speaking, vad_open=self._vad_open,
                    )
                return
            if isinstance(frame, InterimTranscriptionFrame):
                setattr(frame, "_echosphere_stability_reference", False)
            else:
                # A final while the caller is still speaking is the server
                # splitting a long utterance, not its end (same marker the
                # legacy adapter sets on its barge-in flush finals).
                setattr(frame, "_echosphere_mid_utterance", bool(self._vad_open))
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
                    **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in self._guard_stats.items()},
                )
        await super()._handle_message(message)
        if event == "session.begin":
            await self._open_utterance()
