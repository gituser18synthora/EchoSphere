"""Conservative transport-delivery evidence; never guess words from elapsed time."""
from __future__ import annotations

from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from voice_runtime.frames import TranscriptPlayoutFrame


class PlayoutLedger:
    def __init__(self):
        self.contexts: dict[str, dict] = {}
        self.used: set[str] = set()

    def note(self, frame: TranscriptPlayoutFrame):
        entry = self.contexts.setdefault(frame.context_id, {"text": "", "complete": False, "interrupted": False})
        if entry["interrupted"]:
            return
        if frame.text:
            # Each boundary carries the complete prefix, so an earlier boundary
            # held behind a transport's partial chunk cannot lose that sentence.
            entry["text"] = " ".join(frame.text.split())
        entry["complete"] = frame.complete

    def interrupt(self):
        for entry in self.contexts.values():
            if not entry["complete"]:
                entry["interrupted"] = True

    def for_turn(self, generated: str) -> tuple[str, dict]:
        generated = " ".join(generated.split())
        for context, entry in self.contexts.items():
            spoken = entry["text"]
            if context in self.used or not spoken:
                continue
            if generated == spoken or generated.startswith(spoken + " "):
                self.used.add(context)
                complete = entry["complete"] and generated == spoken
                return spoken, {
                    "status": "transport_delivered_complete" if complete else "transport_delivered_prefix",
                    "partial_sentence_omitted": not complete, "far_end_playback_verified": False,
                }
        return "", {"status": "unverified", "partial_sentence_omitted": True,
                     "far_end_playback_verified": False}


class TranscriptPlayoutObserver(FrameProcessor):
    """Sits after output, so queued but discarded text is never committed."""
    def __init__(self, ledger: PlayoutLedger):
        super().__init__()
        self.ledger = ledger
        self.delivered_seconds: dict[str, float] = {}
        self.active_context: str | None = None

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TranscriptPlayoutFrame):
                if not frame.text and not frame.complete:
                    self.active_context = frame.context_id
                    self.delivered_seconds.setdefault(frame.context_id, 0.0)
                    self.ledger.note(frame)
                elif (frame.audio_bytes > 0 and frame.sample_rate > 0
                      and self.delivered_seconds.get(frame.context_id, 0) + 1e-9
                      >= frame.audio_bytes / (frame.sample_rate * 2)):
                    self.ledger.note(frame)
                return
            if isinstance(frame, TTSAudioRawFrame):
                # Pipecat re-chunks/resamples audio and drops context_id. The
                # ordered start marker supplies ownership; filler never counts.
                context = getattr(frame, "context_id", None) or self.active_context
                if context in self.delivered_seconds and frame.sample_rate > 0:
                    self.delivered_seconds[context] += len(frame.audio) / (
                        frame.sample_rate * frame.num_channels * 2
                    )
            if isinstance(frame, InterruptionFrame):
                self.ledger.interrupt()
                self.active_context = None
        await self.push_frame(frame, direction)
