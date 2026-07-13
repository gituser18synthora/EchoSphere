"""Tests for ModelFactory: correct adapter type and caching."""

from unittest.mock import MagicMock, patch

import pytest

from adapters.base import LLMAdapter, STTAdapter, TTSAdapter
from adapters.factory import ModelFactory


class TestModelFactoryLLM:
    @pytest.mark.asyncio
    async def test_create_llm_openai_returns_openai_adapter(self):
        with patch("adapters.llm.openai_adapter.Settings") as m:
            m.return_value.openai_api_key = "test"
            m.return_value.llm_max_response_latency = 3.0
            llm = ModelFactory.create_llm("openai", "gpt-4o")
        assert isinstance(llm, LLMAdapter)
        assert llm.__class__.__name__ == "OpenAILLMAdapter"

    @pytest.mark.asyncio
    async def test_create_llm_anthropic_returns_anthropic_adapter(self):
        with patch("adapters.llm.anthropic_adapter.Settings") as m:
            m.return_value.anthropic_api_key = "test"
            m.return_value.llm_max_response_latency = 3.0
            llm = ModelFactory.create_llm("anthropic", "claude-3-5-sonnet-20241022")
        assert isinstance(llm, LLMAdapter)
        assert llm.__class__.__name__ == "AnthropicLLMAdapter"

    @pytest.mark.asyncio
    async def test_create_llm_google_returns_google_adapter(self):
        with patch("adapters.llm.google_adapter.Settings") as m:
            m.return_value.google_api_key = "test"
            m.return_value.llm_max_response_latency = 3.0
            llm = ModelFactory.create_llm("google", "gemini-1.5-flash")
        assert isinstance(llm, LLMAdapter)
        assert llm.__class__.__name__ == "GoogleLLMAdapter"

    def test_create_llm_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown LLM provider_id"):
            ModelFactory.create_llm("unknown_llm", "some-model")

    def test_create_llm_caching_same_instance(self):
        with patch("adapters.llm.openai_adapter.Settings") as m:
            m.return_value.openai_api_key = "test"
            m.return_value.llm_max_response_latency = 3.0
            a = ModelFactory.create_llm("openai", "gpt-4o")
            b = ModelFactory.create_llm("openai", "gpt-4o")
        assert a is b


class TestModelFactorySTT:
    def test_create_stt_deepgram_returns_deepgram_adapter(self):
        with patch("adapters.stt.deepgram_adapter.Settings") as m:
            m.return_value.deepgram_api_key = "test"
            m.return_value.stt_tts_max_latency = 2.0
            stt = ModelFactory.create_stt("deepgram")
        assert isinstance(stt, STTAdapter)
        assert stt.__class__.__name__ == "DeepgramSTTAdapter"

    def test_create_stt_whisper_returns_whisper_adapter(self):
        with patch("adapters.stt.whisper_adapter.Settings") as m:
            m.return_value.openai_api_key = "test"
            m.return_value.stt_tts_max_latency = 2.0
            stt = ModelFactory.create_stt("whisper")
        assert isinstance(stt, STTAdapter)
        assert stt.__class__.__name__ == "WhisperSTTAdapter"

    def test_create_stt_assemblyai_returns_assemblyai_adapter(self):
        with patch("adapters.stt.assemblyai_adapter.Settings") as m:
            m.return_value.assemblyai_api_key = "test"
            m.return_value.stt_tts_max_latency = 2.0
            stt = ModelFactory.create_stt("assemblyai")
        assert isinstance(stt, STTAdapter)
        assert stt.__class__.__name__ == "AssemblyAISTTAdapter"

    def test_create_stt_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown STT provider_id"):
            ModelFactory.create_stt("unknown_stt")

    def test_create_stt_caching_same_instance(self):
        with patch("adapters.stt.deepgram_adapter.Settings") as m:
            m.return_value.deepgram_api_key = "test"
            m.return_value.stt_tts_max_latency = 2.0
            a = ModelFactory.create_stt("deepgram")
            b = ModelFactory.create_stt("deepgram")
        assert a is b


class TestModelFactoryTTS:
    def test_create_tts_elevenlabs_returns_elevenlabs_adapter(self):
        with patch("adapters.tts.elevenlabs_adapter.Settings") as m:
            m.return_value.elevenlabs_api_key = "test"
            m.return_value.stt_tts_max_latency = 2.0
            tts = ModelFactory.create_tts("elevenlabs")
        assert isinstance(tts, TTSAdapter)
        assert tts.__class__.__name__ == "ElevenLabsTTSAdapter"

    def test_create_tts_azure_returns_azure_adapter(self):
        with patch("adapters.tts.azure_adapter.Settings") as m:
            m.return_value.azure_speech_key = "test"
            m.return_value.azure_speech_region = "eastus"
            m.return_value.stt_tts_max_latency = 2.0
            tts = ModelFactory.create_tts("azure_tts")
        assert isinstance(tts, TTSAdapter)
        assert tts.__class__.__name__ == "AzureTTSAdapter"

    def test_create_tts_google_returns_google_adapter(self):
        with patch("adapters.tts.google_adapter.Settings") as m:
            m.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.google_adapter.texttospeech.TextToSpeechClient") as client_cls:
                client_cls.return_value = MagicMock()
                tts = ModelFactory.create_tts("google_tts")
        assert isinstance(tts, TTSAdapter)
        assert tts.__class__.__name__ == "GoogleTTSAdapter"

    def test_create_tts_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown TTS provider_id"):
            ModelFactory.create_tts("unknown_tts")

    def test_create_tts_caching_same_instance(self):
        with patch("adapters.tts.elevenlabs_adapter.Settings") as m:
            m.return_value.elevenlabs_api_key = "test"
            m.return_value.stt_tts_max_latency = 2.0
            a = ModelFactory.create_tts("elevenlabs")
            b = ModelFactory.create_tts("elevenlabs")
        assert a is b
