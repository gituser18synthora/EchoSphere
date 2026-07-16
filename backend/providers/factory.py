"""Provider factory — builds STT/TTS/LLM providers from typed config.

Registry maps (kind, provider) → import path. SDK imports are lazy so an
uninstalled optional SDK only fails when that provider is actually selected.
"""

import importlib
import threading

from backend.providers.base import LLMProvider, ProviderConfig, ProviderError, STTProvider, TTSProvider

_REGISTRY: dict[tuple[str, str], str] = {
    ("stt", "openai"): "backend.providers.stt.whisper:WhisperSTT",
    ("stt", "whisper"): "backend.providers.stt.whisper:WhisperSTT",
    ("stt", "deepgram"): "backend.providers.stt.deepgram:DeepgramSTT",
    ("stt", "assemblyai"): "backend.providers.stt.assemblyai:AssemblyAISTT",
    ("stt", "sarvam"): "backend.providers.stt.sarvam:SarvamSTT",
    ("stt", "mock"): "backend.providers.stt.mock:MockSTT",
    ("tts", "openai"): "backend.providers.tts.openai_tts:OpenAITTS",
    ("tts", "elevenlabs"): "backend.providers.tts.elevenlabs:ElevenLabsTTS",
    ("tts", "azure"): "backend.providers.tts.azure_tts:AzureTTS",
    ("tts", "google"): "backend.providers.tts.google_tts:GoogleTTS",
    ("tts", "sarvam"): "backend.providers.tts.sarvam:SarvamTTS",
    ("tts", "mock"): "backend.providers.tts.mock:MockTTS",
    ("llm", "openai"): "backend.providers.llm.openai_llm:OpenAILLM",
    ("llm", "anthropic"): "backend.providers.llm.anthropic_llm:AnthropicLLM",
    ("llm", "google"): "backend.providers.llm.google_llm:GoogleLLM",
    ("llm", "mock"): "backend.providers.llm.mock:MockLLM",
}

_cache: dict[str, object] = {}
_lock = threading.Lock()


def _build(kind: str, config: ProviderConfig):
    key = (kind, config.provider.lower())
    path = _REGISTRY.get(key)
    if path is None:
        raise ProviderError(config.provider, "invalid_input", f"Unknown {kind} provider")
    module_name, class_name = path.split(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ProviderError(
            config.provider, "invalid_input",
            f"SDK for {kind} provider '{config.provider}' is not installed: {exc.name}",
        ) from exc
    cls = getattr(module, class_name)
    return cls(config)


def _cached(kind: str, config: ProviderConfig):
    cache_key = f"{kind}:{config.provider}:{config.model}:{config.voice}:{config.language}"
    instance = _cache.get(cache_key)
    if instance is None:
        with _lock:
            instance = _cache.get(cache_key)
            if instance is None:
                instance = _build(kind, config)
                _cache[cache_key] = instance
    return instance


def get_stt_provider(config: ProviderConfig) -> STTProvider:
    return _cached("stt", config)


def get_tts_provider(config: ProviderConfig) -> TTSProvider:
    return _cached("tts", config)


def get_llm_provider(config: ProviderConfig) -> LLMProvider:
    return _cached("llm", config)


def clear_provider_cache() -> None:
    with _lock:
        _cache.clear()
