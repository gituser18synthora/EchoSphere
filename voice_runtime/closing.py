"""A cancelable workflow closing waits for actual output, not synthesis."""

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_runtime.frames import WorkflowClosingFrame, WorkflowClosingPlayedFrame


class ClosingPlaybackObserver(FrameProcessor):
    """After transport.output(): acknowledge its ordered farewell fence.

    The TTS service orders the fence behind its audio contexts; the output
    transport orders it behind paced audio and its final partial-chunk flush.
    Interruptions discard queued fences along with the audio. A fence already
    in flight is harmless: the brain also checks the closing token.
    """

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, WorkflowClosingFrame):
            await self.push_frame(
                WorkflowClosingPlayedFrame(token=frame.token), FrameDirection.UPSTREAM,
            )
            return
        await self.push_frame(frame, direction)

