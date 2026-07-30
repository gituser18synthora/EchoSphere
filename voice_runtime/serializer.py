"""Browser voice-test wire protocol.

Binary WebSocket messages carry raw 16-bit mono PCM (caller → 16 kHz in,
bot ← output rate out). Text messages are small JSON events so the test UI
can render live transcripts and call events without decoding audio:

  server → client: {"type": "transcript"|"bot_text"|"event", ...}
  client → server: {"type": "event", "name": "..."} (reserved)
"""

import json

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class RawPCMSerializer(FrameSerializer):
    """PCM-in / PCM-out + JSON side-channel for the browser test client.

    The transport sends binary WS messages for `bytes` results and text
    messages for `str` results, so no type declaration is needed.
    """

    def __init__(self, *, input_sample_rate: int = 16000, **kwargs) -> None:
        super().__init__(**kwargs)
        self._input_sample_rate = input_sample_rate

    async def setup(self, frame: StartFrame):
        pass

    async def serialize(self, frame: Frame) -> str | bytes | None:
        from pipecat.frames.frames import OutputTransportMessageFrame

        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, OutputTransportMessageFrame):
            return json.dumps(frame.message)
        if isinstance(frame, TranscriptionFrame):
            return json.dumps({"type": "transcript", "text": frame.text})
        if isinstance(frame, TextFrame):
            return json.dumps({"type": "bot_text", "text": frame.text})
        if isinstance(frame, BotStartedSpeakingFrame):
            return json.dumps({"type": "event", "name": "bot_speaking_started"})
        if isinstance(frame, BotStoppedSpeakingFrame):
            return json.dumps({"type": "event", "name": "bot_speaking_stopped"})
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "event", "name": "interruption"})
        if isinstance(frame, ErrorFrame):
            # Category-style codes only (e.g. "tts_failure:timeout") — never
            # provider payloads or anything that could carry secrets.
            message = str(frame.error or "")[:120]
            return json.dumps({"type": "error", "message": message})
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(
                audio=bytes(data), sample_rate=self._input_sample_rate, num_channels=1
            )
        return None
