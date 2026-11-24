"""Ordered opening/closing fences wait for transport output, not synthesis."""

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_runtime.frames import (
    GreetingPlaybackFrame, GreetingPlayedFrame,
    WorkflowClosingFrame, WorkflowClosingPlayedFrame,
)


class ClosingPlaybackObserver(FrameProcessor):
    """After transport.output(): acknowledge ordered opening/closing fences.

    The TTS service orders the fence behind its audio contexts; the output
    transport orders it behind paced audio and its final partial-chunk flush.
    Interruptions discard queued fences along with the audio. A fence already
    in flight is harmless: the brain checks the token/marker and cancellation.
    This proves transport delivery, not whether the remote caller heard it.
    """

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, GreetingPlaybackFrame):
            await self.push_frame(
                GreetingPlayedFrame(marker=frame.marker), FrameDirection.UPSTREAM,
            )
            return
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, WorkflowClosingFrame):
            await self.push_frame(
                WorkflowClosingPlayedFrame(token=frame.token), FrameDirection.UPSTREAM,
            )
            return
        await self.push_frame(frame, direction)

