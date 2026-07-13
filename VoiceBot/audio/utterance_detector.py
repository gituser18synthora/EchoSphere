"""WebRTC VAD-based utterance boundary detection for 16-bit mono PCM."""

import collections
import math
import struct

import webrtcvad


class UtteranceDetector:
    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 3,
        pre_speech_padding_ms: int = 300,
        min_speech_ms: int = 400,
        end_silence_ms: int = 700,
        max_utterance_ms: int = 15000,
        energy_threshold_dbfs: float = -40.0,
    ):
        self._sample_rate = sample_rate
        self._energy_threshold_dbfs = energy_threshold_dbfs
        self._frame_ms = frame_ms
        self._frame_bytes = sample_rate * frame_ms // 1000 * 2
        self._min_speech_ms = min_speech_ms
        self._end_silence_frames = end_silence_ms // frame_ms
        self._max_utterance_frames = max_utterance_ms // frame_ms

        self._vad = webrtcvad.Vad(aggressiveness)
        ring_len = pre_speech_padding_ms // frame_ms
        self._ring: collections.deque[bytes] = collections.deque(maxlen=ring_len)

        self._utterance_frames: list[bytes] = []
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0

    def feed(self, frame: bytes) -> bytes | None:
        if len(frame) != self._frame_bytes:
            raise ValueError(
                f"Expected frame size {self._frame_bytes} bytes, got {len(frame)}",
            )

        if self._rms_dbfs(frame) < self._energy_threshold_dbfs:
            is_speech = False
        else:
            is_speech = self._vad.is_speech(frame, self._sample_rate)

        if not self._in_speech:
            self._ring.append(frame)
            if is_speech:
                self._in_speech = True
                self._utterance_frames.extend(self._ring)
                self._ring.clear()
                self._silence_frames = 0
                self._speech_frames = 1
            return None

        if is_speech:
            self._utterance_frames.append(frame)
            self._silence_frames = 0
            self._speech_frames += 1
        else:
            self._utterance_frames.append(frame)
            self._silence_frames += 1

        should_finalize = (
            self._silence_frames > self._end_silence_frames
            or len(self._utterance_frames) > self._max_utterance_frames
        )
        if not should_finalize:
            return None

        return self._finalize()

    def _rms_dbfs(self, frame: bytes) -> float:
        count = len(frame) // 2
        if count == 0:
            return -96.0
        samples = struct.unpack(f"{count}h", frame)
        mean_sq = sum(s * s for s in samples) / count
        if mean_sq == 0:
            return -96.0
        return 20 * math.log10(math.sqrt(mean_sq) / 32768.0)

    def reset(self) -> None:
        self._ring.clear()
        self._utterance_frames.clear()
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0

    def _finalize(self) -> bytes | None:
        speech_ms = self._speech_frames * self._frame_ms
        payload = b"".join(self._utterance_frames)

        self._ring.clear()
        self._utterance_frames.clear()
        self._in_speech = False
        self._silence_frames = 0
        self._speech_frames = 0

        if speech_ms < self._min_speech_ms:
            return None
        return payload
