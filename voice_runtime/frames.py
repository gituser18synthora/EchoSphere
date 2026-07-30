"""Custom pipeline frames used between the brain and the TTS router.

DataFrames so they queue in-order with the token TextFrames they relate to.
"""

from dataclasses import dataclass

from pipecat.frames.frames import DataFrame


@dataclass
class SwitchVoiceLanguageFrame(DataFrame):
    """Ask the TTS router to switch the active conversation locale.

    Emitted by the brain when the detected transcript language changes. The
    switch applies from the next bot reply; provider connections are reused
    where the mapping allows it.
    """

    language: str = ""


@dataclass
class TTSFlushHintFrame(DataFrame):
    """Ask the TTS router to flush buffered text mid-turn.

    Emitted by the brain when the LLM token stream pauses for longer than the
    configured interval, so already-buffered text starts rendering instead of
    waiting for the next sentence boundary.
    """

    reason: str = "llm_pause"
