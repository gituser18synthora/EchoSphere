"""Telephony media-stream serializers for the voice worker's WebSocket.

Maps a provider name to the Pipecat frame serializer that speaks that
provider's media-stream wire format. Runtime-only code: the platform API
never touches media frames.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    EndWorkerFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    StartFrame,
    StopFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

from shared.errors import ApiError
from voice_runtime.serializer import RawPCMSerializer

_VAANI_MIN_CHUNK_BYTES = 3200
_VAANI_MAX_CHUNK_BYTES = 100_000
_VAANI_FRAME_BYTES = 320
# 100 KB of PCM is ~133.4 KB base64 — anything larger is a protocol violation.
_VAANI_MAX_B64_CHARS = 140_000

logger = logging.getLogger(__name__)


class VaaniFrameSerializer(FrameSerializer):
    """Vaani Telephony JSON media-stream protocol.

    Vaani sends/receives base64 encoded 8 kHz, 16-bit, mono PCM in JSON
    `media` events. Outbound audio is grouped on 320-byte boundaries to match
    the platform's playback guidance.

    Idempotency/robustness: events carrying a different streamSid are dropped,
    `media` chunks with a non-increasing sequence number are treated as
    duplicates (a Vaani retry must never produce doubled caller audio, doubled
    STT usage or doubled replies), oversized/malformed payloads are ignored,
    and exactly one outbound `stop` is ever emitted.
    """

    def __init__(self, *, stream_sid: str, track: str = "inbound", **kwargs) -> None:
        super().__init__(**kwargs)
        self._stream_sid = stream_sid
        self._track = track or "inbound"
        self._out_chunk = 0
        self._pending_audio = bytearray()
        self._last_in_chunk = 0
        self._stopped = False
        self._warned_sid_mismatch = False

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if self._stopped:
            return None  # after `stop`, nothing else may go on the wire
        if isinstance(frame, OutputAudioRawFrame):
            self._pending_audio.extend(frame.audio)
            return self._pop_audio_chunk(force=False)
        if isinstance(frame, BotStoppedSpeakingFrame):
            return self._pop_audio_chunk(force=True)
        if isinstance(frame, InterruptionFrame):
            self._pending_audio.clear()
            return json.dumps({
                "event": "clear",
                "streamSid": self._stream_sid,
                "clear": {"reason": "interrupt"},
            })
        if isinstance(frame, OutputTransportMessageFrame):
            message = frame.message or {}
            if message.get("type") == "telephony_control":
                event = message.get("event")
                if event == "transfer":
                    payload = {
                        "event": "transfer",
                        "streamSid": self._stream_sid,
                        "transfer": {
                            "reason": message.get("reason") or "transfer",
                        },
                    }
                    if message.get("transfer_queue"):
                        payload["transfer"]["transfer_queue"] = message["transfer_queue"]
                    if message.get("agent_id"):
                        payload["transfer"]["agent_id"] = message["agent_id"]
                    return json.dumps(payload)
                if event == "stop":
                    self._stopped = True
                    return self._stop_message(message.get("reason") or "stop")
        if isinstance(frame, (EndFrame, StopFrame, EndWorkerFrame)):
            # The protocol `stop` must never be swallowed by a residual audio
            # tail (a sub-chunk remnant is inaudible; the stop event is not).
            self._stopped = True
            self._pending_audio.clear()
            return self._stop_message("stop")
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(audio=bytes(data), sample_rate=8000, num_channels=1)
        try:
            message = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(message, dict):
            return None
        sid = message.get("streamSid")
        if sid and sid != self._stream_sid:
            # Another stream's events must never feed this call.
            if not self._warned_sid_mismatch:
                self._warned_sid_mismatch = True
                logger.warning(
                    "vaani: dropping event for foreign streamSid (expected %s)",
                    self._stream_sid,
                )
            return None
        event = message.get("event")
        if event == "media":
            media = message.get("media") or {}
            payload = media.get("payload") or ""
            if len(payload) > _VAANI_MAX_B64_CHARS:
                logger.warning("vaani: media payload exceeds 100 KB limit — dropped")
                return None
            # Duplicate protection: Vaani numbers chunks sequentially; a retry
            # or replay (non-increasing sequence) is dropped so it can never
            # double caller audio, STT usage or bot replies.
            seq = media.get("chunk")
            try:
                seq_no = int(seq)
            except (TypeError, ValueError):
                seq_no = None
            if seq_no is not None:
                if seq_no <= self._last_in_chunk:
                    return None
                self._last_in_chunk = seq_no
            try:
                audio = base64.b64decode(payload)
            except (binascii.Error, ValueError):
                return None
            if not audio:
                return None
            return InputAudioRawFrame(audio=audio, sample_rate=8000, num_channels=1)
        if event == "stop":
            # EndWorkerFrame (not EndFrame): a bare EndFrame injected from the
            # input transport flows downstream without stopping the
            # PipelineWorker — the call would stay "active" until the socket
            # dropped. EndWorkerFrame triggers the worker's own shutdown, so a
            # Vaani-initiated stop tears the session down even if Vaani keeps
            # the socket open.
            return EndWorkerFrame(reason="caller_stop")
        # `connected`, duplicate `start` and unknown events are ignored safely.
        return None

    def _pop_audio_chunk(self, *, force: bool) -> str | None:
        if not self._pending_audio:
            return None
        available = len(self._pending_audio)
        if not force and available < _VAANI_MIN_CHUNK_BYTES:
            return None
        limit = min(available, _VAANI_MAX_CHUNK_BYTES)
        size = limit - (limit % _VAANI_FRAME_BYTES)
        if size == 0:
            if not force:
                return None
            size = available
        chunk = bytes(self._pending_audio[:size])
        del self._pending_audio[:size]
        remainder = len(chunk) % _VAANI_FRAME_BYTES
        if remainder:
            chunk += b"\x00" * (_VAANI_FRAME_BYTES - remainder)
        self._out_chunk += 1
        return json.dumps({
            "event": "media",
            "streamSid": self._stream_sid,
            "media": {
                "track": self._track,
                "chunk": str(self._out_chunk),
                "timestamp": str(int(time.time())),
                "payload": base64.b64encode(chunk).decode("ascii"),
            },
        })

    def _stop_message(self, reason: str) -> str:
        return json.dumps({
            "event": "stop",
            "streamSid": self._stream_sid,
            "stop": {"reason": reason},
        })


def build_media_serializer(provider: str, *, start_message: dict | None = None):
    """Return the Pipecat frame serializer for a provider media stream.

    `start_message` is the provider's stream-start payload (already parsed),
    required by providers whose serializer needs stream identifiers.
    """
    if provider == "freeswitch":
        # mod_audio_fork ships raw L16 both ways — our RawPCM serializer fits.
        return RawPCMSerializer(input_sample_rate=8000)
    if provider == "twilio":
        from pipecat.serializers.twilio import TwilioFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_sid = start.get("streamSid") or (start_message or {}).get("streamSid")
        if not stream_sid:
            raise ApiError("Twilio stream start message missing streamSid", 400)
        return TwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=start.get("callSid"),
        )
    if provider == "telnyx":
        from pipecat.serializers.telnyx import TelnyxFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_id = start.get("stream_id") or (start_message or {}).get("stream_id")
        if not stream_id:
            raise ApiError("Telnyx stream start message missing stream_id", 400)
        return TelnyxFrameSerializer(
            stream_id=stream_id,
            call_control_id=start.get("call_control_id"),
            outbound_encoding=start.get("media_format", {}).get("encoding", "PCMU"),
        )
    if provider == "plivo":
        from pipecat.serializers.plivo import PlivoFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_id = start.get("streamId") or (start_message or {}).get("streamId")
        if not stream_id:
            raise ApiError("Plivo stream start message missing streamId", 400)
        return PlivoFrameSerializer(stream_id=stream_id, call_id=start.get("callId"))
    if provider == "exotel":
        from pipecat.serializers.exotel import ExotelFrameSerializer

        start = (start_message or {}).get("start", {})
        stream_sid = start.get("stream_sid") or (start_message or {}).get("stream_sid")
        if not stream_sid:
            raise ApiError("Exotel stream start message missing stream_sid", 400)
        return ExotelFrameSerializer(stream_sid=stream_sid)
    if provider == "vaani":
        start = (start_message or {}).get("start", {})
        stream_sid = start.get("streamSid") or (start_message or {}).get("streamSid")
        if not stream_sid:
            raise ApiError("Vaani stream start message missing streamSid", 400)
        media_format = start.get("mediaFormat") or {}
        if int(media_format.get("sampleRate") or 8000) != 8000:
            raise ApiError("Vaani mediaFormat.sampleRate must be 8000", 400)
        if int(media_format.get("channels") or 1) != 1:
            raise ApiError("Vaani mediaFormat.channels must be 1", 400)
        return VaaniFrameSerializer(stream_sid=stream_sid)
    raise ApiError(f"Unsupported telephony provider '{provider}'", 400)
