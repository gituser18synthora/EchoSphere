"""Provider notification that a bounded final-transcript retry was exhausted."""

from dataclasses import dataclass

from pipecat.frames.frames import SystemFrame


@dataclass
class STTFinalMissingFrame(SystemFrame):
    generation: int = 0
