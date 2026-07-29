"""Telephony media-stream serializers for the voice worker's WebSocket.

Maps a provider name to the Pipecat frame serializer that speaks that
provider's media-stream wire format. Runtime-only code: the platform API
never touches media frames.

Every serializer here also acts as the call's media-stage tracer: it logs
(per session) inbound message counts/sizes, per-channel audio levels,
outbound chunk counts and protocol control events, so a live call can be
followed end to end from ``fs-media[…]``/``vaani-media[…]`` lines →
``stage[…]`` (VAD/STT) → ``turn[…]`` (LLM) → ``tts[…]`` without a packet
capture. Audio payloads and secrets are never logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import os
import time
from array import array
from pathlib import Path

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
_FREESWITCH_MIN_CHUNK_BYTES = 3200
_FREESWITCH_MAX_CHUNK_BYTES = 32_000
_FREESWITCH_FRAME_BYTES = 320
# 100 KB of PCM is ~133.4 KB base64 — anything larger is a protocol violation.
_VAANI_MAX_B64_CHARS = 140_000

# ── FreeSWITCH wire-content analysis ────────────────────────────────────────
# The mod_audio_stream binary feed is NOT self-describing: a 640-byte 20 ms
# message is two interleaved 16-bit little-endian streams — stereo L16@8k
# with the caller (read) stream first and the bot (write) stream second on
# the installed QA module — or, on other builds, a swapped order or one mono
# L16@16k stream.
#
# Byte order: capture analysis on 2026-07-29 (three live calls dumped via
# ECHOSPHERE_FS_AUDIO_DEBUG_DIR) shows BOTH streams are little-endian —
# real caller speech reads with adjacent-sample correlation 0.87 as LE vs
# 0.11 when byte-swapped, and the known-good TTS write stream reads 0.92 LE
# vs 0.05 swapped. A network-byte-order (big-endian) interpretation merely
# amplifies the noise floor ×256 and must NOT be applied.
#
# Levels: real caller speech arrived at peak ≈ 6000 (0.18 full-scale) — just
# BELOW the telephony VAD volume gate — and other calls carried near-silence
# (peak 24–424) when the caller did not speak. The serializer therefore
# measures BOTH streams, selects the one that actually carries voice, and
# raises the caller level with a bounded adaptive gain before VAD/STT.
#
# Channel/level evidence is only trusted while the bot has not sent audio
# recently: during bot playback, inbound levels are dominated by line echo
# (or, on a misidentified channel, by the bot's own TTS), and trusting them
# makes the bot barge in on its own greeting — observed live on 2026-07-29
# when the write stream was selected with a fixed ×8 gain.
_FS_SPEECH_PEAK = 500             # ≥ ~1.5% full-scale counts as voice evidence
_FS_VOICED_MSGS_TO_DECIDE = 25    # ≈ 0.5 s of voiced 20 ms messages
_FS_MONO_MIN_CORR = 0.90          # even/odd correlation ⇒ mono at 2× rate
_FS_QUIET_GRACE_SECS = 1.5        # evidence counts this long AFTER bot audio
_FS_ECHO_GATE_SECS = 0.7          # echo-gating window after the last bot audio
_FS_ECHO_GATE_PEAK = 2500         # below this during playback = echo → no gain
_FS_PLAYBACK_SUSPECT_PEAK = 3000  # unlocked channel this loud during playback = own TTS
_FS_PLAYBACK_SUSPECT_MSGS = 15    # ≈ 0.3 s of that before switching away
_FS_GAIN_TARGET_PEAK = 16000      # ≈ -6 dBFS post-gain caller speech target
_FS_DEFAULT_INPUT_GAIN = 12.0     # gain CAP; observed speech at 0.18 FS needs ~2.7×
_FS_DEBUG_CAPTURE_SECS = 20       # per-channel dump when the debug dir is set
_MEDIA_LOG_INTERVAL_SECS = 5.0

logger = logging.getLogger(__name__)

_CHANNEL_NAMES = {0: "first", 1: "second"}
_CHANNEL_ALIASES = {"first": 0, "left": 0, "second": 1, "right": 1}


class FreeSwitchAudioStreamSerializer(FrameSerializer):
    """Media wire format used by ``mod_audio_stream`` on FreeSWITCH.

    Inbound: binary L16 PCM, two interleaved 16-bit little-endian streams
    (see the analysis notes above). Only the caller stream is sent into
    VAD/STT so the bot cannot transcribe its own playback. Bot audio must be
    returned in the module's ``streamAudio`` JSON envelope; raw binary output
    is not played.

    ``caller_channel``:
      - ``"auto"`` (default): start on the *first* stream (FreeSWITCH's
        standard read/caller position on the installed QA module) and
        lock/switch from bot-quiet voice evidence — at most one switch per
        call, loudly logged.
      - ``"first"``/``"second"`` (aliases ``left``/``right``): pin explicitly;
        no automatic switching.

    Inbound level handling (evaluated per 20 ms message):
      - While the bot is quiet: apply an adaptive gain — ``min(input_gain,
        16000/observed_voiced_peak)``, never below 1 — so the observed
        0.18-full-scale caller speech clears the VAD volume gate but
        already-loud speech is never clipped.
      - Within the echo window after bot audio: quiet messages (< echo gate)
        pass UNGAINED so playback echo cannot trip VAD/barge-in; loud messages
        pass gained (real barge-in) once the channel is locked.
      - An *unlocked* channel at playback level during playback is carrying
        our own TTS: those messages are muted and sustained evidence switches
        the caller channel — the bot can never hear its own greeting.

    Setting ``ECHOSPHERE_FS_AUDIO_DEBUG_DIR`` dumps the first ~20 s of BOTH
    channels to ``.s16le`` files there for offline listening/analysis
    (contains caller audio — development diagnosis only).
    """

    def __init__(
        self,
        *,
        session_id: str = "",
        caller_channel: str = "auto",
        input_gain: float = _FS_DEFAULT_INPUT_GAIN,
        send_kill_audio: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._session_id = session_id or "?"
        self._send_kill_audio = send_kill_audio
        mode = (caller_channel or "auto").strip().lower()
        if mode != "auto" and mode not in _CHANNEL_ALIASES:
            logger.warning(
                "fs-media[%s] invalid caller_channel=%r — using 'auto'",
                self._session_id, caller_channel,
            )
            mode = "auto"
        self._channel_mode = mode
        # Standard FreeSWITCH stereo bug layout: read/caller first.
        self._selected = _CHANNEL_ALIASES.get(mode, 0)
        self._locked = mode != "auto"  # pinned config is trusted immediately
        self._switched = False
        self._mono_2x = False
        self._base_gain = max(1.0, float(input_gain))
        self._voiced_peak_track = 0.0  # decaying max of bot-quiet voiced peaks
        # inbound stats (totals + current log interval)
        self._inbound_bytes = 0
        self._inbound_interval_bytes = 0
        self._inbound_msgs = 0
        self._inbound_interval_msgs = 0
        self._inbound_interval_peak = 0
        self._first_channel_interval_peak = 0
        self._second_channel_interval_peak = 0
        self._quiet_voiced = [0, 0]   # voiced messages seen while bot quiet
        self._quiet_peak = [0, 0]     # per-channel peak while bot quiet
        self._playback_suspect = [0, 0]
        self._muted_msgs = 0
        self._corr_xy = 0.0
        self._corr_xx = 0.0
        self._corr_yy = 0.0
        self._corr_msgs = 0
        self._last_inbound_log = 0.0
        self._logged_first_in = False
        self._seen_text_types: set[str] = set()
        # outbound
        self._pending_audio = bytearray()
        self._out_chunks = 0
        self._out_bytes = 0
        self._interval_out_chunks = 0
        self._interval_out_bytes = 0
        self._last_bot_audio = 0.0
        self._first_out_time = 0.0
        self._logged_first_out = False
        # Opt-in per-channel debug capture (ECHOSPHERE_FS_AUDIO_DEBUG_DIR):
        # both interleaved streams are dumped separately so channel
        # orientation, byte order and the mono-at-2× question can be settled
        # offline (see scripts/dialer_sim.py inspect).
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
            self._debug_audio_remaining = 8000 * 2 * _FS_DEBUG_CAPTURE_SECS
            logger.warning(
                "FreeSWITCH audio debug capture enabled: first=%s second=%s",
                first_path,
                second_path,
            )

    async def setup(self, frame: StartFrame):
        pass

    # ── outbound: bot audio → module playback envelope ─────────────────────

    async def serialize(self, frame: Frame) -> str | None:
        if isinstance(frame, OutputAudioRawFrame):
            self._last_bot_audio = time.monotonic()
            self._pending_audio.extend(frame.audio)
            return self._emit_chunk(force=False)
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._last_bot_audio = time.monotonic()
            return self._emit_chunk(force=True)
        if isinstance(frame, InterruptionFrame):
            dropped = len(self._pending_audio)
            self._pending_audio.clear()
            logger.info(
                "fs-media[%s] barge-in: dropped %d pending outbound bytes%s",
                self._session_id, dropped,
                ", sending killAudio" if self._send_kill_audio else "",
            )
            if self._send_kill_audio:
                # Local buffers alone are not enough: up to
                # _FREESWITCH_MAX_CHUNK_BYTES (~2 s) of audio already shipped
                # keeps playing at the module and talks over the caller.
                # killAudio tells mod_audio_stream to drop its playback queue.
                return json.dumps({"type": "killAudio"})
        return None

    def _emit_chunk(self, *, force: bool) -> str | None:
        audio = self._pop_audio_chunk(force=force)
        if audio is None:
            return None
        self._out_chunks += 1
        self._out_bytes += len(audio)
        self._interval_out_chunks += 1
        self._interval_out_bytes += len(audio)
        if not self._logged_first_out:
            self._logged_first_out = True
            self._first_out_time = time.monotonic()
            logger.info(
                "fs-media[%s] first outbound bot-audio chunk → dialer: %d bytes "
                "(%.0f ms) raw L16@8k mono in streamAudio envelope "
                "(sampleRate=8000)",
                self._session_id, len(audio), len(audio) / 16.0,
            )
        return json.dumps({
            "type": "streamAudio",
            "data": {
                "audioDataType": "raw",
                "sampleRate": 8000,
                "audioData": base64.b64encode(audio).decode("ascii"),
            },
        })

    def _pop_audio_chunk(self, *, force: bool) -> bytes | None:
        if not self._pending_audio:
            return None
        available = len(self._pending_audio)
        if not force and available < _FREESWITCH_MIN_CHUNK_BYTES:
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
        return audio

    # ── inbound: caller audio ───────────────────────────────────────────────

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)) and data:
            return self._deserialize_media(bytes(data))
        if isinstance(data, str) and data:
            # Metadata / module status messages are not caller audio.
            self._log_text_message(data)
        return None

    def _deserialize_media(self, wire: bytes) -> Frame | None:
        # One frame is two signed 16-bit samples. Drop an incomplete trailing
        # frame rather than shifting channel alignment for all later samples.
        usable = len(wire) - (len(wire) % 4)
        if usable == 0:
            return None
        samples = array("h")
        samples.frombytes(wire[:usable])
        channels = (samples[0::2], samples[1::2])
        peaks = (
            max((abs(s) for s in channels[0]), default=0),
            max((abs(s) for s in channels[1]), default=0),
        )
        self._debug_capture(channels)
        since_bot_audio = (
            time.monotonic() - self._last_bot_audio if self._last_bot_audio else 1e9
        )
        self._update_evidence(channels, peaks, since_bot_audio > _FS_QUIET_GRACE_SECS)

        if self._mono_2x:
            audio_samples = samples
            selected_peak = max(peaks)
        else:
            audio_samples = channels[self._selected]
            selected_peak = peaks[self._selected]

        audio_samples, gain = self._condition_level(
            audio_samples, selected_peak, since_bot_audio
        )
        self._track_inbound(len(wire), len(audio_samples) * 2, peaks, gain)
        return InputAudioRawFrame(
            audio=audio_samples.tobytes(),
            sample_rate=16000 if self._mono_2x else 8000,
            num_channels=1,
        )

    def _condition_level(self, audio_samples, peak: int, since_bot_audio: float):
        """Echo-safe level conditioning for the selected caller stream.

        Returns (samples, applied_gain). See the class docstring for the
        rules; this is what keeps the bot from barging in on itself.
        """
        if since_bot_audio <= _FS_ECHO_GATE_SECS:
            if not self._locked and peak >= _FS_PLAYBACK_SUSPECT_PEAK:
                # An unlocked channel at playback level during playback is our
                # own TTS coming back — never let VAD/STT hear it.
                self._muted_msgs += 1
                return array("h", bytes(len(audio_samples) * 2)), 0.0
            if peak < _FS_ECHO_GATE_PEAK:
                return audio_samples, 1.0  # echo stays below VAD volume
        gain = self._effective_gain()
        if gain <= 1.0:
            return audio_samples, 1.0
        gained = array("h", audio_samples)  # never mutate the analysis view
        for index, sample in enumerate(gained):
            gained[index] = max(-32768, min(32767, int(sample * gain)))
        return gained, gain

    def _effective_gain(self) -> float:
        if self._voiced_peak_track <= 0:
            return self._base_gain
        return max(1.0, min(self._base_gain,
                            _FS_GAIN_TARGET_PEAK / self._voiced_peak_track))

    def _update_evidence(self, channels, peaks, bot_quiet: bool) -> None:
        if bot_quiet:
            for i in (0, 1):
                if peaks[i] > self._quiet_peak[i]:
                    self._quiet_peak[i] = peaks[i]
                if peaks[i] >= _FS_SPEECH_PEAK:
                    self._quiet_voiced[i] += 1
            if peaks[0] >= _FS_SPEECH_PEAK and peaks[1] >= _FS_SPEECH_PEAK:
                self._accumulate_correlation(channels)
            selected_peak = peaks[self._selected]
            if selected_peak >= _FS_SPEECH_PEAK:
                self._voiced_peak_track = max(
                    self._voiced_peak_track * 0.999, float(selected_peak)
                )
            elif self._voiced_peak_track > 0:
                self._voiced_peak_track *= 0.999
            if not self._locked:
                self._maybe_lock_channel()
        elif not self._locked:
            # During playback an unlocked channel at TTS level is suspect.
            for i in (0, 1):
                if peaks[i] >= _FS_PLAYBACK_SUSPECT_PEAK:
                    self._playback_suspect[i] += 1
            self._maybe_flee_playback_channel()

    def _accumulate_correlation(self, channels) -> None:
        for x, y in zip(channels[0], channels[1]):
            fx, fy = float(x), float(y)
            self._corr_xy += fx * fy
            self._corr_xx += fx * fx
            self._corr_yy += fy * fy
        self._corr_msgs += 1

    def _correlation(self) -> float:
        denom = math.sqrt(self._corr_xx * self._corr_yy)
        return (self._corr_xy / denom) if denom > 0 else 0.0

    def _maybe_lock_channel(self) -> None:
        """One-shot channel decision from bot-quiet evidence only."""
        voiced = self._quiet_voiced
        threshold = _FS_VOICED_MSGS_TO_DECIDE
        if voiced[0] >= threshold and voiced[1] >= threshold:
            corr = self._correlation()
            self._locked = True
            if corr >= _FS_MONO_MIN_CORR and self._corr_msgs >= threshold:
                self._mono_2x = True
                logger.warning(
                    "fs-media[%s] wire is MONO PCM at 2× rate (even/odd sample "
                    "correlation %.3f) — switching to mono L16@16k handling",
                    self._session_id, corr,
                )
            else:
                logger.warning(
                    "fs-media[%s] voice on BOTH streams (corr=%.3f) — keeping "
                    "the %s stream as caller",
                    self._session_id, corr, _CHANNEL_NAMES[self._selected],
                )
            return
        other = 1 - self._selected
        if (
            voiced[other] >= threshold
            and voiced[other] >= 10 * max(1, voiced[self._selected])
            and self._quiet_peak[self._selected] < _FS_SPEECH_PEAK
        ):
            self._switch_selected(
                other,
                f"caller voice found on the {_CHANNEL_NAMES[other]} stream while "
                f"the {_CHANNEL_NAMES[self._selected]} stream is silent "
                f"(quiet peaks first={self._quiet_peak[0]} "
                f"second={self._quiet_peak[1]})",
            )
            self._locked = True
        elif voiced[self._selected] >= threshold:
            self._locked = True  # the selected stream demonstrably carries voice
            logger.info(
                "fs-media[%s] caller channel confirmed: %s stream "
                "(quiet_voiced=[%d,%d])",
                self._session_id, _CHANNEL_NAMES[self._selected],
                voiced[0], voiced[1],
            )

    def _maybe_flee_playback_channel(self) -> None:
        suspect = self._playback_suspect[self._selected]
        other = 1 - self._selected
        if (
            suspect >= _FS_PLAYBACK_SUSPECT_MSGS
            and self._playback_suspect[other] < _FS_PLAYBACK_SUSPECT_MSGS
        ):
            self._switch_selected(
                other,
                f"the {_CHANNEL_NAMES[self._selected]} stream carries the bot's "
                f"own playback ({suspect} messages at ≥{_FS_PLAYBACK_SUSPECT_PEAK} "
                "peak during TTS output)",
            )

    def _switch_selected(self, to: int, reason: str) -> None:
        if self._switched:
            return
        old = _CHANNEL_NAMES[self._selected]
        self._selected = to
        self._switched = True
        self._voiced_peak_track = 0.0
        logger.warning(
            "fs-media[%s] auto-switching caller channel %s → %s: %s. "
            "Set FREESWITCH_CALLER_CHANNEL=%s to pin this.",
            self._session_id, old, _CHANNEL_NAMES[to], reason,
            _CHANNEL_NAMES[to],
        )

    # ── inbound telemetry / debug capture ───────────────────────────────────

    def _debug_capture(self, channels) -> None:
        if not self._debug_audio_remaining or self._debug_first_file is None:
            return
        capture_bytes = min(len(channels[0]) * 2, self._debug_audio_remaining)
        self._debug_first_file.write(channels[0].tobytes()[:capture_bytes])
        self._debug_second_file.write(channels[1].tobytes()[:capture_bytes])
        self._debug_audio_remaining -= capture_bytes
        if self._debug_audio_remaining <= 0:
            self._debug_first_file.close()
            self._debug_second_file.close()
            self._debug_first_file = None
            self._debug_second_file = None
            logger.warning("FreeSWITCH audio debug capture completed")

    def _track_inbound(self, wire_len: int, emitted_len: int, peaks, gain: float) -> None:
        self._inbound_bytes += emitted_len
        self._inbound_interval_bytes += emitted_len
        self._inbound_msgs += 1
        self._inbound_interval_msgs += 1
        selected_peak = max(peaks) if self._mono_2x else peaks[self._selected]
        if selected_peak > self._inbound_interval_peak:
            self._inbound_interval_peak = selected_peak
        if peaks[0] > self._first_channel_interval_peak:
            self._first_channel_interval_peak = peaks[0]
        if peaks[1] > self._second_channel_interval_peak:
            self._second_channel_interval_peak = peaks[1]
        if not self._logged_first_in:
            self._logged_first_in = True
            logger.info(
                "fs-media[%s] first inbound media frame: %d bytes binary "
                "(2×16-bit interleaved), caller_channel=%s selected=%s gain=%.1f",
                self._session_id, wire_len, self._channel_mode,
                _CHANNEL_NAMES[self._selected], gain,
            )
        now = time.monotonic()
        if self._last_inbound_log == 0.0:
            self._last_inbound_log = now
            return
        if now - self._last_inbound_log < _MEDIA_LOG_INTERVAL_SECS:
            return
        logger.info(
            "freeswitch audio inbound[%s]: msgs=%d total_bytes=%d interval_bytes=%d "
            "selected=%s(gain=%.1f) selected_peak=%d (%.3f full-scale) "
            "raw_first_peak=%d raw_second_peak=%d quiet_voiced=[%d,%d] "
            "quiet_peak=[%d,%d] muted_msgs=%d | out: chunks=%d bytes=%d",
            self._session_id,
            self._inbound_interval_msgs,
            self._inbound_bytes,
            self._inbound_interval_bytes,
            "mono16k" if self._mono_2x else _CHANNEL_NAMES[self._selected],
            self._effective_gain(),
            self._inbound_interval_peak,
            self._inbound_interval_peak / 32768.0,
            self._first_channel_interval_peak,
            self._second_channel_interval_peak,
            self._quiet_voiced[0], self._quiet_voiced[1],
            self._quiet_peak[0], self._quiet_peak[1],
            self._muted_msgs,
            self._interval_out_chunks, self._interval_out_bytes,
        )
        if self._first_out_time:
            # Pacing check: audio-seconds shipped vs wall-clock since first
            # send. ratio≈1.0 = real-time; <1 the caller will hear gaps,
            # >1 the module is buffering ahead.
            out_audio_s = self._out_bytes / 16000.0
            wall_s = now - self._first_out_time
            logger.info(
                "fs-media[%s] outbound pacing: audio_s=%.1f wall_s=%.1f "
                "ratio=%.2f chunks=%d",
                self._session_id, out_audio_s, wall_s,
                (out_audio_s / wall_s) if wall_s > 0 else 0.0,
                self._out_chunks,
            )
        self._inbound_interval_bytes = 0
        self._inbound_interval_msgs = 0
        self._inbound_interval_peak = 0
        self._first_channel_interval_peak = 0
        self._second_channel_interval_peak = 0
        self._interval_out_chunks = 0
        self._interval_out_bytes = 0
        self._last_inbound_log = now

    def _log_text_message(self, data: str) -> None:
        kind = "non-json"
        try:
            message = json.loads(data)
            if isinstance(message, dict):
                kind = str(message.get("type") or message.get("event") or "?")
        except (json.JSONDecodeError, ValueError):
            pass
        if kind not in self._seen_text_types:
            self._seen_text_types.add(kind)
            logger.info(
                "fs-media[%s] text message from dialer module (type=%s): %s",
                self._session_id, kind, data[:200],
            )


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

    def __init__(
        self, *, stream_sid: str, track: str = "inbound",
        session_id: str = "", **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._stream_sid = stream_sid
        self._track = track or "inbound"
        self._session_id = session_id or stream_sid
        self._out_chunk = 0
        self._pending_audio = bytearray()
        self._last_in_chunk = 0
        self._stopped = False
        self._warned_sid_mismatch = False
        # media-stage tracing
        self._in_msgs = 0
        self._in_bytes = 0
        self._interval_in_msgs = 0
        self._interval_in_bytes = 0
        self._interval_in_peak = 0
        self._out_bytes = 0
        self._interval_out_chunks = 0
        self._interval_out_bytes = 0
        self._dropped: dict[str, int] = {}
        self._last_media_log = 0.0
        self._logged_first_in = False
        self._logged_first_out = False

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
            dropped = len(self._pending_audio)
            self._pending_audio.clear()
            logger.info(
                "vaani-media[%s] sending clear (barge-in, dropped %d pending bytes)",
                self._session_id, dropped,
            )
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
                    logger.info(
                        "vaani-media[%s] sending transfer (reason=%s)",
                        self._session_id, payload["transfer"]["reason"],
                    )
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
            self._drop("bad_json")
            return None
        if not isinstance(message, dict):
            self._drop("bad_json")
            return None
        sid = message.get("streamSid")
        if sid and sid != self._stream_sid:
            # Another stream's events must never feed this call.
            self._drop("foreign_sid")
            if not self._warned_sid_mismatch:
                self._warned_sid_mismatch = True
                logger.warning(
                    "vaani-media[%s] dropping event for foreign streamSid "
                    "(expected %s)",
                    self._session_id, self._stream_sid,
                )
            return None
        event = message.get("event")
        if event == "media":
            media = message.get("media") or {}
            payload = media.get("payload") or ""
            if len(payload) > _VAANI_MAX_B64_CHARS:
                self._drop("oversize")
                logger.warning(
                    "vaani-media[%s] media payload exceeds 100 KB limit — dropped",
                    self._session_id,
                )
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
                    self._drop("duplicate_chunk")
                    return None
                self._last_in_chunk = seq_no
            try:
                audio = base64.b64decode(payload)
            except (binascii.Error, ValueError):
                self._drop("bad_base64")
                return None
            if not audio:
                self._drop("empty")
                return None
            self._observe_inbound(media, payload, audio)
            return InputAudioRawFrame(audio=audio, sample_rate=8000, num_channels=1)
        if event == "stop":
            logger.info(
                "vaani-media[%s] stop event from dialer (reason=%s) — ending worker",
                self._session_id, (message.get("stop") or {}).get("reason"),
            )
            # EndWorkerFrame (not EndFrame): a bare EndFrame injected from the
            # input transport flows downstream without stopping the
            # PipelineWorker — the call would stay "active" until the socket
            # dropped. EndWorkerFrame triggers the worker's own shutdown, so a
            # Vaani-initiated stop tears the session down even if Vaani keeps
            # the socket open.
            return EndWorkerFrame(reason="caller_stop")
        # `connected`, duplicate `start` and unknown events are ignored safely.
        return None

    def _drop(self, reason: str) -> None:
        self._dropped[reason] = self._dropped.get(reason, 0) + 1

    def _observe_inbound(self, media: dict, payload: str, audio: bytes) -> None:
        self._in_msgs += 1
        self._in_bytes += len(audio)
        self._interval_in_msgs += 1
        self._interval_in_bytes += len(audio)
        samples = array("h")
        samples.frombytes(audio[: len(audio) - (len(audio) % 2)])
        peak = max((abs(s) for s in samples), default=0)
        if peak > self._interval_in_peak:
            self._interval_in_peak = peak
        if not self._logged_first_in:
            self._logged_first_in = True
            logger.info(
                "vaani-media[%s] first inbound media event: chunk=%s "
                "payload_b64=%d chars decoded=%d bytes (expected L16@8k mono)",
                self._session_id, media.get("chunk"), len(payload), len(audio),
            )
        self._maybe_log_interval()

    def _maybe_log_interval(self) -> None:
        now = time.monotonic()
        if self._last_media_log == 0.0:
            self._last_media_log = now
            return
        if now - self._last_media_log < _MEDIA_LOG_INTERVAL_SECS:
            return
        logger.info(
            "vaani-media[%s] in: msgs=%d bytes=%d peak=%d (%.3f full-scale) | "
            "out: chunks=%d bytes=%d | dropped=%s | totals: in=%d out=%d",
            self._session_id,
            self._interval_in_msgs, self._interval_in_bytes,
            self._interval_in_peak, self._interval_in_peak / 32768.0,
            self._interval_out_chunks, self._interval_out_bytes,
            {k: v for k, v in self._dropped.items() if v} or "{}",
            self._in_bytes, self._out_bytes,
        )
        self._interval_in_msgs = 0
        self._interval_in_bytes = 0
        self._interval_in_peak = 0
        self._interval_out_chunks = 0
        self._interval_out_bytes = 0
        self._last_media_log = now

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
        self._out_bytes += len(chunk)
        self._interval_out_chunks += 1
        self._interval_out_bytes += len(chunk)
        if not self._logged_first_out:
            self._logged_first_out = True
            logger.info(
                "vaani-media[%s] first outbound bot-audio media event → dialer: "
                "chunk=1 %d bytes (L16@8k mono, base64)",
                self._session_id, len(chunk),
            )
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
        logger.info(
            "vaani-media[%s] sending stop (reason=%s) — nothing may follow it",
            self._session_id, reason,
        )
        return json.dumps({
            "event": "stop",
            "streamSid": self._stream_sid,
            "stop": {"reason": reason},
        })


def build_media_serializer(
    provider: str, *, start_message: dict | None = None, session_id: str = ""
):
    """Return the Pipecat frame serializer for a provider media stream.

    `start_message` is the provider's stream-start payload (already parsed),
    required by providers whose serializer needs stream identifiers.
    `session_id` tags the serializer's media-stage log lines.
    """
    if provider == "freeswitch":
        # QA uses mod_audio_stream: 2×16-bit interleaved binary inbound
        # (caller stream measured/selected above), JSON/base64 playback out.
        from shared.config import get_settings

        settings = get_settings()
        return FreeSwitchAudioStreamSerializer(
            session_id=session_id,
            caller_channel=settings.freeswitch_caller_channel,
            input_gain=settings.freeswitch_input_gain,
            send_kill_audio=settings.freeswitch_send_kill_audio,
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
        return VaaniFrameSerializer(stream_sid=stream_sid, session_id=session_id)
    raise ApiError(f"Unsupported telephony provider '{provider}'", 400)
