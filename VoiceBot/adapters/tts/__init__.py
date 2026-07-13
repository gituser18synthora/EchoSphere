# Lazy-load adapters that require optional packages (elevenlabs, azure, google)
# so that "import adapters.tts" or loading sarvam_adapter does not require them.
from adapters.tts.sarvam_adapter import SarvamTTSAdapter

__all__ = [
    "ElevenLabsTTSAdapter",
    "AzureTTSAdapter",
    "GoogleTTSAdapter",
    "SarvamTTSAdapter",
]


def __getattr__(name: str):
    if name == "ElevenLabsTTSAdapter":
        from adapters.tts.elevenlabs_adapter import ElevenLabsTTSAdapter
        return ElevenLabsTTSAdapter
    if name == "AzureTTSAdapter":
        from adapters.tts.azure_adapter import AzureTTSAdapter
        return AzureTTSAdapter
    if name == "GoogleTTSAdapter":
        from adapters.tts.google_adapter import GoogleTTSAdapter
        return GoogleTTSAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
