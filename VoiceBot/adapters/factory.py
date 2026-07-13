"""ModelFactory: instantiates LLM, STT, and TTS adapters from config via dynamic import."""
 
import importlib
import threading
from typing import Any
 
from adapters.base import LLMAdapter, STTAdapter, TTSAdapter
 
ADAPTER_REGISTRY = {
    # LLM
    "openai": "adapters.llm.openai_adapter.OpenAILLMAdapter",
    "anthropic": "adapters.llm.anthropic_adapter.AnthropicLLMAdapter",
    "google": "adapters.llm.google_adapter.GoogleLLMAdapter",
    # STT
    "whisper": "adapters.stt.whisper_adapter.WhisperSTTAdapter",
    "deepgram": "adapters.stt.deepgram_adapter.DeepgramSTTAdapter",
    "assemblyai": "adapters.stt.assemblyai_adapter.AssemblyAISTTAdapter",
    "sarvam_stt": "adapters.stt.sarvam_adapter.SarvamSTTAdapter",
    # TTS
    "elevenlabs": "adapters.tts.elevenlabs_adapter.ElevenLabsTTSAdapter",
    "azure_tts": "adapters.tts.azure_adapter.AzureTTSAdapter",
    "google_tts": "adapters.tts.google_adapter.GoogleTTSAdapter",
    "sarvam_tts": "adapters.tts.sarvam_adapter.SarvamTTSAdapter",
}
 
# Thread-safe caches — keyed by (provider_id, model_id) for LLM,
# (provider_id,) for STT/TTS. Shared safely across concurrent calls
# because adapters are stateless after construction.
_llm_cache: dict[tuple[str, str], LLMAdapter] = {}
_stt_cache: dict[tuple[str, ...], STTAdapter] = {}
_tts_cache: dict[tuple[str, ...], TTSAdapter] = {}
_cache_lock = threading.Lock()
 
 
def _load_class(path: str) -> type:
    """Load a class from a dotted path string."""
    module_path, _, class_name = path.rpartition(".")
    if not module_path or not class_name:
        raise ValueError(f"Invalid adapter path: {path}")
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise ValueError(f"Adapter module not found for path {path}: {e}") from e
    cls = getattr(module, class_name, None)
    if cls is None:
        raise ValueError(f"Adapter class {class_name} not found in {module_path}")
    return cls
 
 
class ModelFactory:
    """Creates and caches LLM, STT, and TTS adapters by provider_id.
 
    All adapters are constructed once and reused across calls — they are
    stateless after __init__ and safe for concurrent use.
    """
 
    @staticmethod
    def create_llm(provider_id: str, model_id: str, **kwargs: Any) -> LLMAdapter:
        key = (provider_id, model_id)
        with _cache_lock:
            if key in _llm_cache:
                return _llm_cache[key]
            if provider_id not in ADAPTER_REGISTRY:
                raise ValueError(f"Unknown LLM provider_id: {provider_id}")
            cls = _load_class(ADAPTER_REGISTRY[provider_id])
            adapter = cls(model_id=model_id, **kwargs)
            _llm_cache[key] = adapter
            return adapter
 
    @staticmethod
    def create_stt(provider_id: str, **kwargs: Any) -> STTAdapter:
        key = (provider_id,)
        with _cache_lock:
            if key in _stt_cache:
                return _stt_cache[key]
            if provider_id not in ADAPTER_REGISTRY:
                raise ValueError(f"Unknown STT provider_id: {provider_id}")
            cls = _load_class(ADAPTER_REGISTRY[provider_id])
            adapter = cls(**kwargs)
            _stt_cache[key] = adapter
            return adapter
 
    @staticmethod
    def create_tts(provider_id: str, **kwargs: Any) -> TTSAdapter:
        key = (provider_id,)
        with _cache_lock:
            if key in _tts_cache:
                return _tts_cache[key]
            if provider_id not in ADAPTER_REGISTRY:
                raise ValueError(f"Unknown TTS provider_id: {provider_id}")
            cls = _load_class(ADAPTER_REGISTRY[provider_id])
            adapter = cls(**kwargs)
            _tts_cache[key] = adapter
            return adapter
 
    @staticmethod
    def clear_cache() -> None:
        """Clear all adapter caches. Useful for testing or forced reload."""
        with _cache_lock:
            _llm_cache.clear()
            _stt_cache.clear()
            _tts_cache.clear()