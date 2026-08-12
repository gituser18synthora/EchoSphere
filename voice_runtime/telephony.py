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
import os
import time
from pathlib import Path

import numpy as np

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    EndWorkerFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    StopFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer

from shared.config import get_settings
from shared.errors import ApiError
_VAANI_MIN_CHUNK_BYTES = 3200
_VAANI_MAX_CHUNK_BYTES = 100_000
_VAANI_FRAME_BYTES = 320
_FREESWITCH_MIN_CHUNK_BYTES = 3200
_FREESWITCH_MAX_CHUNK_BYTES = 32_000
_FREESWITCH_FRAME_BYTES = 320
# 100 KB of PCM is ~133.4 KB base64 — anything larger is a protocol violation.
_VAANI_MAX_B64_CHARS = 140_000

# First-packet ramp (see _RampedAudioMixin). The steady-state 3200-byte packet
# is 200 ms of 8 kHz PCM; the output transport feeds this serializer one
# 40 ms chunk per 40 ms pacing tick, so waiting for 3200 bytes delayed the
# FIRST packet of every reply by ~160 ms. Emitting smaller packets while the
# reply starts costs no playout margin — five 40 ms ticks carry 200 ms of
# audio in 160 ms of wall clock, so the dialer's lead is the same ~40 ms
# either way. It simply arrives sooner.
_RAMP_THRESHOLDS = (640, 1280, 2560)
# Outbound audio quiet for longer than this is a new utterance: the ramp
# resets so the next reply's first packet is fast again.
_RAMP_RESET_IDLE_S = 0.5

logger = logging.getLogger(__name__)


class _RampedAudioMixin:
    """Outbound PCM buffering with a fast first packet per utterance.

    The steady-state packet size is unchanged; only the first few packets of
    an utterance are allowed out early, which is what the caller perceives as
    the bot's response time.
    """

    def _init_ramp(self) -> None:
        self._ramp_step = 0
        self._last_audio_at = 0.0

    def _note_audio(self) -> None:
        """Record inbound-from-TTS audio, restarting the ramp after a gap."""
        now = time.monotonic()
        if self._last_audio_at and now - self._last_audio_at > _RAMP_RESET_IDLE_S:
            self._ramp_step = 0
        self._last_audio_at = now

    def _reset_ramp(self) -> None:
        self._ramp_step = 0

    def _min_chunk_bytes(self, steady_bytes: int) -> int:
        if self._ramp_step >= len(_RAMP_THRESHOLDS):
            return steady_bytes
        return min(_RAMP_THRESHOLDS[self._ramp_step], steady_bytes)

    def _advance_ramp(self) -> None:
        if self._ramp_step < len(_RAMP_THRESHOLDS):
            self._ramp_step += 1


class _FreeSwitchControlMixin:
    """Telephony control (`transfer`) handling shared by both FreeSWITCH
    serializers.

    The brain publishes handoff decisions as ``telephony_control`` transport
    messages (the same contract the Vaani serializer speaks). Dropping them —
    the pre-fix behavior — meant the bot ANNOUNCED the transfer and then kept
    the call: ``voicebot.lua`` waits for the ``mod_audio_fork::transfer``
    event that only our ``{"type": "transfer"}`` message can produce.
    """

    # Whether the module behind this serializer understands the transfer
    # message. mod_audio_fork converts it into the CUSTOM event the dialplan
    # script acts on; mod_audio_stream has no equivalent.
    _TRANSFER_ON_WIRE = True

    def _init_control(self) -> None:
        self._transfer_sent = False
        # Installed by the session host (app.py): observes control events at
        # the moment they reach the wire (transfer bookkeeping + monitoring).
        self.on_telephony_control = None

    async def _serialize_control(self, message: dict) -> str | None:
        if (
            message.get("type") != "telephony_control"
            or message.get("event") != "transfer"
        ):
            return None
        if self._transfer_sent:
            # A retried handoff decision must not re-trigger the dialplan.
            return None
        self._transfer_sent = True
        await self._notify_control(message)
        if not self._TRANSFER_ON_WIRE:
            logger.error(
                "freeswitch transfer requested but mod_audio_stream has no "
                "transfer message — use the audio_fork transport "
                "(voicebot.lua) for human handoff"
            )
            return None
        data = {"reason": message.get("reason") or "transfer"}
        if message.get("transfer_queue"):
            data["transfer_queue"] = str(message["transfer_queue"])
        if message.get("agent_id"):
            data["agent_id"] = str(message["agent_id"])
        wire_message = json.dumps({"type": "transfer", "data": data})
        logger.info("freeswitch outbound transfer: %s", wire_message)
        return wire_message

    async def _notify_control(self, message: dict) -> None:
        callback = getattr(self, "on_telephony_control", None)
        if callback is None:
            return
        try:
            await callback(message)
        except Exception:  # noqa: BLE001 — bookkeeping must never break media
            logger.exception("telephony control hook failed")


class FreeSwitchAudioStreamSerializer(
    _FreeSwitchControlMixin, _RampedAudioMixin, FrameSerializer
):
    """Media wire format used by ``mod_audio_stream`` on FreeSWITCH.

    Audio arrives as stereo binary L16 PCM. The installed QA
    ``mod_audio_stream`` build emits caller/read audio first and bot/write
    audio second. Only the caller channel is sent into VAD/STT so the bot
    cannot transcribe its own playback. Bot audio must
    be returned in the module's ``streamAudio`` JSON envelope; raw binary
    output is not played.
    """

    _TRANSFER_ON_WIRE = False  # mod_audio_stream has no transfer message

    def __init__(self, *, send_kill_audio: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self._init_control()
        self._pending_audio = bytearray()
        # Barge-in: clearing local pending bytes is not enough — up to ~2 s of
        # already-shipped audio sits in the module's playback buffer and would
        # talk over the caller. killAudio tells mod_audio_stream to drop it;
        # disable only for older module builds that reject the message.
        self._send_kill_audio = bool(send_kill_audio)
        self._init_ramp()
        self._inbound_bytes = 0
        self._inbound_interval_bytes = 0
        self._inbound_interval_peak = 0
        self._first_channel_interval_peak = 0
        self._second_channel_interval_peak = 0
        self._last_inbound_log = 0.0
        self._warned_text_input = False
        self._debug_audio_remaining = 0
        self._debug_first_file = None
        self._debug_second_file = None
        debug_dir = os.getenv("ECHOSPHERE_FS_AUDIO_DEBUG_DIR")
        if debug_dir:
            directory = Path(debug_dir)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = f"{int(time.time())}-{id(self)}"
            first_path = directory / f"echosphere-fs-{stamp}-first.s16le"
            second_path = directory / f"echosphere-fs-{stamp}-second.s16le"
            self._debug_first_file = first_path.open("wb")
            self._debug_second_file = second_path.open("wb")
            self._debug_audio_remaining = 8000 * 2 * 20
            logger.warning(
                "FreeSWITCH audio debug capture enabled: first=%s second=%s",
                first_path,
                second_path,
            )

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame) -> str | None:
        if isinstance(frame, OutputAudioRawFrame):
            self._note_audio()
            self._pending_audio.extend(frame.audio)
            return self._pop_audio_chunk(force=False)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._reset_ramp()
            return self._pop_audio_chunk(force=True)
        if isinstance(frame, InterruptionFrame):
            self._pending_audio.clear()
            self._reset_ramp()
            if self._send_kill_audio:
                return json.dumps({"type": "killAudio"})
            return None
        # Both message variants must be accepted (the urgent one is a
        # SystemFrame, not a subclass) or transfer controls vanish.
        if isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
            return await self._serialize_control(frame.message or {})
        return None

    def _pop_audio_chunk(self, *, force: bool) -> str | None:
        if not self._pending_audio:
            return None
        available = len(self._pending_audio)
        if not force and available < self._min_chunk_bytes(
            _FREESWITCH_MIN_CHUNK_BYTES
        ):
            return None
        limit = min(available, _FREESWITCH_MAX_CHUNK_BYTES)
        size = limit - (limit % _FREESWITCH_FRAME_BYTES)
        if size == 0:
            if not force:
                return None
            size = available
        audio = bytes(self._pending_audio[:size])
        del self._pending_audio[:size]
        remainder = len(audio) % _FREESWITCH_FRAME_BYTES
        if remainder:
            audio += b"\x00" * (_FREESWITCH_FRAME_BYTES - remainder)
        self._advance_ramp()
        return json.dumps({
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": 8000,
                "audioData": base64.b64encode(audio).decode("ascii"),
            },
        })

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)) and data:
            wire_audio = bytes(data)
            # One stereo frame is two signed 16-bit samples (first=caller/read,
            # second=bot/write on the installed QA module). Drop an
            # incomplete trailing frame rather than shifting channel alignment
            # for all subsequent samples.
            usable = len(wire_audio) - (len(wire_audio) % 4)
            if usable == 0:
                return None
            # Vectorized: the previous pure-Python per-sample scans ran three
            # generator passes per 20 ms frame on the event loop (~25k
            # iterations/second/call).
            stereo_samples = np.frombuffer(wire_audio[:usable], dtype="<i2")
            first_channel = stereo_samples[0::2]
            second_channel = stereo_samples[1::2]
            if self._debug_audio_remaining:
                capture_bytes = min(
                    len(first_channel) * 2, self._debug_audio_remaining
                )
                self._debug_first_file.write(
                    first_channel.tobytes()[:capture_bytes]
                )
                self._debug_second_file.write(
                    second_channel.tobytes()[:capture_bytes]
                )
                self._debug_audio_remaining -= capture_bytes
                if self._debug_audio_remaining == 0:
                    self._debug_first_file.close()
                    self._debug_second_file.close()
                    self._debug_first_file = None
                    self._debug_second_file = None
                    logger.warning("FreeSWITCH audio debug capture completed")
            # int32 before abs(): |-32768| overflows int16.
            first_peak = (
                int(np.abs(first_channel.astype(np.int32)).max())
                if first_channel.size else 0
            )
            second_peak = (
                int(np.abs(second_channel.astype(np.int32)).max())
                if second_channel.size else 0
            )
            self._first_channel_interval_peak = max(
                self._first_channel_interval_peak, first_peak
            )
            self._second_channel_interval_peak = max(
                self._second_channel_interval_peak, second_peak
            )
            audio = first_channel.tobytes()
            self._inbound_bytes += len(audio)
            self._inbound_interval_bytes += len(audio)
            # The selected caller channel IS the first channel — its peak was
            # already computed; the third per-sample pass recomputed it.
            self._inbound_interval_peak = max(
                self._inbound_interval_peak, first_peak
            )
            now = time.monotonic()
            if self._last_inbound_log == 0.0 or now - self._last_inbound_log >= 5.0:
                logger.info(
                    "freeswitch audio inbound: total_bytes=%d interval_bytes=%d "
                    "selected_peak=%d (%.3f full-scale) "
                    "raw_first_peak=%d raw_second_peak=%d",
                    self._inbound_bytes,
                    self._inbound_interval_bytes,
                    self._inbound_interval_peak,
                    self._inbound_interval_peak / 32768.0,
                    self._first_channel_interval_peak,
                    self._second_channel_interval_peak,
                )
                self._inbound_interval_bytes = 0
                self._inbound_interval_peak = 0
                self._first_channel_interval_peak = 0
                self._second_channel_interval_peak = 0
                self._last_inbound_log = now
            return InputAudioRawFrame(
                audio=audio, sample_rate=8000, num_channels=1
            )
        if isinstance(data, str) and data and not self._warned_text_input:
            self._warned_text_input = True
            logger.info(
                "freeswitch websocket text message received instead of audio: %s",
                data[:160],
            )
        # Metadata and module status messages are not caller audio.
        return None


class FreeSwitchAudioForkSerializer(
    _FreeSwitchControlMixin, _RampedAudioMixin, FrameSerializer
):
    """Bidirectional media wire format used by ``mod_audio_fork``.

    The fork is started in ``mono 16k`` mode, so inbound binary frames are
    already caller-only signed 16-bit L16 PCM and must not be deinterleaved.
    Bot audio is returned through the module's documented ``playAudio`` JSON
    envelope. ``killAudio`` clears both EchoSphere's pending audio and the
    module's current playback when Pipecat raises an interruption. A
    ``telephony_control`` transfer becomes the module's ``transfer`` message,
    which FreeSWITCH surfaces as the ``mod_audio_fork::transfer`` CUSTOM
    event ``voicebot.lua`` acts on.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._init_control()
        self._pending_audio = bytearray()
        self._init_ramp()
        self._inbound_bytes = 0
        self._inbound_interval_bytes = 0
        self._inbound_interval_peak = 0
        self._last_inbound_log = 0.0
        self._warned_text_input = False

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame) -> str | None:
        if isinstance(frame, OutputAudioRawFrame):
            self._note_audio()
            self._pending_audio.extend(frame.audio)
            return self._pop_audio_chunk(force=False)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._reset_ramp()
            return self._pop_audio_chunk(force=True)
        if isinstance(frame, InterruptionFrame):
            self._pending_audio.clear()
            self._reset_ramp()
            return json.dumps({"type": "killAudio"})
        if isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
            return await self._serialize_control(frame.message or {})
        return None

    def _pop_audio_chunk(self, *, force: bool) -> str | None:
        if not self._pending_audio:
            return None
        available = len(self._pending_audio)
        if not force and available < self._min_chunk_bytes(
            _FREESWITCH_MIN_CHUNK_BYTES
        ):
            return None
        limit = min(available, _FREESWITCH_MAX_CHUNK_BYTES)
        size = limit - (limit % _FREESWITCH_FRAME_BYTES)
        if size == 0:
            if not force:
                return None
            size = available
        audio = bytes(self._pending_audio[:size])
        del self._pending_audio[:size]
        remainder = len(audio) % _FREESWITCH_FRAME_BYTES
        if remainder:
            audio += b"\x00" * (_FREESWITCH_FRAME_BYTES - remainder)
        self._advance_ramp()
        return json.dumps({
            "type": "playAudio",
            "data": {
                "audioContentType": "raw",
                "sampleRate": 8000,
                "audioContent": base64.b64encode(audio).decode("ascii"),
            },
        })

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)) and data:
            audio = bytes(data)
            # L16 samples are two bytes. Ignore an incomplete trailing byte
            # rather than passing malformed PCM into VAD/STT.
            audio = audio[:len(audio) - (len(audio) % 2)]
            if not audio:
                return None
            samples = np.frombuffer(audio, dtype="<i2")
            peak = (
                int(np.abs(samples.astype(np.int32)).max())
                if samples.size else 0
            )
            self._inbound_bytes += len(audio)
            self._inbound_interval_bytes += len(audio)
            self._inbound_interval_peak = max(
                self._inbound_interval_peak, peak
            )
            now = time.monotonic()
            if (
                self._last_inbound_log == 0.0
                or now - self._last_inbound_log >= 5.0
            ):
                logger.info(
                    "freeswitch fork audio inbound: total_bytes=%d "
                    "interval_bytes=%d peak=%d (%.3f full-scale)",
                    self._inbound_bytes,
                    self._inbound_interval_bytes,
                    self._inbound_interval_peak,
                    self._inbound_interval_peak / 32768.0,
                )
                self._inbound_interval_bytes = 0
                self._inbound_interval_peak = 0
                self._last_inbound_log = now
            return InputAudioRawFrame(
                audio=audio, sample_rate=16000, num_channels=1
            )
        if isinstance(data, str) and data and not self._warned_text_input:
            self._warned_text_input = True
            logger.info(
                "freeswitch fork websocket metadata received: %s",
                data[:160],
            )
        return None


class VaaniFrameSerializer(_RampedAudioMixin, FrameSerializer):
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
        self._init_ramp()
        self._last_in_chunk = 0
        self._stopped = False
        self._warned_sid_mismatch = False

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if self._stopped:
            return None  # after `stop`, nothing else may go on the wire
        if isinstance(frame, OutputAudioRawFrame):
            self._note_audio()
            self._pending_audio.extend(frame.audio)
            return self._pop_audio_chunk(force=False)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._reset_ramp()
            return self._pop_audio_chunk(force=True)
        if isinstance(frame, InterruptionFrame):
            self._pending_audio.clear()
            self._reset_ramp()
            return json.dumps({
                "event": "clear",
                "streamSid": self._stream_sid,
                "clear": {"reason": "interrupt"},
            })
        # The urgent variant is a SystemFrame and does not subclass the plain
        # message frame — both must be accepted or control events vanish.
        if isinstance(
            frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)
        ):
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
                    wire_message = json.dumps(payload)
                    logger.info(
                        "vaani websocket outbound transfer: %s",
                        wire_message,
                    )
                    return wire_message
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
        if not force and available < self._min_chunk_bytes(_VAANI_MIN_CHUNK_BYTES):
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
        self._advance_ramp()
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


def build_media_serializer(
    provider: str,
    *,
    start_message: dict | None = None,
    transport: str | None = None,
):
    """Return the Pipecat frame serializer for a provider media stream.

    `start_message` is the provider's stream-start payload (already parsed),
    required by providers whose serializer needs stream identifiers.
    """
    if provider == "freeswitch":
        if transport == "audio_fork":
            return FreeSwitchAudioForkSerializer()
        if transport not in (None, "", "audio_stream"):
            raise ApiError(
                f"Unsupported FreeSWITCH media transport '{transport}'", 400
            )
        # QA uses mod_audio_stream: stereo L16 inbound (caller channel is
        # selected above), JSON/base64 playback out.
        return FreeSwitchAudioStreamSerializer(
            send_kill_audio=get_settings().freeswitch_send_kill_audio,
        )
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
