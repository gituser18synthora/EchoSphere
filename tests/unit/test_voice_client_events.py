"""Live client events must not be paced behind the bot's own audio.

The brain's side-channel JSON (live transcript, bot text, language switch) is
what a test UI timestamps a turn with. A plain ``OutputTransportMessageFrame``
is a DataFrame, so pipecat's output transport routes it through the realtime
audio queue: a message pushed after a reply's TTS frames is only written once
that whole reply has played out. A ten-second utterance therefore reported the
bot's turn ~10s after it actually started speaking, which reads as response
latency even though the measured spans were ~2s.

These tests pin the two halves of the fix: the brain emits the URGENT frame,
and both serializers recognise it (it is a SystemFrame, NOT a subclass of the
plain message frame, so an isinstance check for the latter silently drops it).
"""

import json

from pipecat.frames.frames import (
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)

from voice_runtime.serializer import RawPCMSerializer
from voice_runtime.telephony import VaaniFrameSerializer


class _CapturingBrain:
    """Minimal stand-in exercising the real _notify_client implementation."""

    def __init__(self):
        self.pushed = []

    async def push_frame(self, frame, direction=None):
        self.pushed.append(frame)


async def test_notify_client_uses_the_urgent_frame():
    from voice_runtime.brain import ConversationBrain

    brain = _CapturingBrain()
    await ConversationBrain._notify_client(brain, {"type": "bot_text", "text": "hi"})

    assert len(brain.pushed) == 1
    frame = brain.pushed[0]
    # Urgent → written immediately by the transport; the plain frame would be
    # queued behind however many seconds of speech precede it.
    assert isinstance(frame, OutputTransportMessageUrgentFrame)
    assert not isinstance(frame, OutputTransportMessageFrame)
    assert frame.message == {"type": "bot_text", "text": "hi"}


async def test_raw_pcm_serializer_accepts_urgent_messages():
    serializer = RawPCMSerializer()
    payload = {"type": "bot_text", "text": "आपका payment overdue है"}

    urgent = await serializer.serialize(OutputTransportMessageUrgentFrame(message=payload))
    plain = await serializer.serialize(OutputTransportMessageFrame(message=payload))

    assert json.loads(urgent) == payload
    # Both variants stay supported: only the queueing behaviour differs.
    assert json.loads(plain) == payload


async def test_media_serializer_accepts_urgent_control_messages():
    serializer = VaaniFrameSerializer(stream_sid="st_1")
    control = {
        "type": "telephony_control",
        "event": "transfer",
        "reason": "policy_confirmed_agent",
    }

    urgent = await serializer.serialize(
        OutputTransportMessageUrgentFrame(message=control)
    )

    assert urgent is not None, "urgent control events must reach the carrier"
    assert json.loads(urgent)["event"] == "transfer"
