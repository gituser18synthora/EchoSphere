# Lazy-load adapters that require optional packages (deepgram, assemblyai)
# so that "import adapters.stt" or loading sarvam_adapter does not require them.
from adapters.stt.whisper_adapter import WhisperSTTAdapter
from adapters.stt.sarvam_adapter import SarvamSTTAdapter

__all__ = [
    "WhisperSTTAdapter",
    "DeepgramSTTAdapter",
    "AssemblyAISTTAdapter",
    "SarvamSTTAdapter",
]


def __getattr__(name: str):
    if name == "DeepgramSTTAdapter":
        from adapters.stt.deepgram_adapter import DeepgramSTTAdapter
        return DeepgramSTTAdapter
    if name == "AssemblyAISTTAdapter":
        from adapters.stt.assemblyai_adapter import AssemblyAISTTAdapter
        return AssemblyAISTTAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
