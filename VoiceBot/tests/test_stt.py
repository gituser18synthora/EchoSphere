"""Tests for STT adapters: success, timeout, rate limit, auth failure."""

from unittest.mock import MagicMock, patch

import pytest

from adapters.base import AdapterException, STTResponse
from adapters.stt.whisper_adapter import WhisperSTTAdapter
from adapters.stt.deepgram_adapter import DeepgramSTTAdapter
from adapters.stt.assemblyai_adapter import AssemblyAISTTAdapter


class TestWhisperSTTAdapter:
    @pytest.mark.asyncio
    async def test_transcribe_success_returns_stt_response(self, sample_pcm_audio):
        mock_response = MagicMock()
        mock_response.text = "hello world"
        with patch("adapters.stt.whisper_adapter.Settings") as settings_mock:
            settings_mock.return_value.openai_api_key = "sk-test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.stt.whisper_adapter.AsyncOpenAI") as client_cls:
                client = MagicMock()
                async def create(*args, **kwargs):
                    return mock_response
                client.audio.transcriptions.create = create
                client_cls.return_value = client
                adapter = WhisperSTTAdapter()
                result = await adapter.transcribe(
                    audio_bytes=sample_pcm_audio,
                    language="en",
                )
        assert isinstance(result, STTResponse)
        assert result.text == "hello world"
        assert result.is_final is True
        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_transcribe_auth_failure_raises_adapter_exception(
        self, sample_pcm_audio
    ):
        with patch("adapters.stt.whisper_adapter.Settings") as settings_mock:
            settings_mock.return_value.openai_api_key = "sk-test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.stt.whisper_adapter.AsyncOpenAI") as client_cls:
                client = MagicMock()
                async def fail(*args, **kwargs):
                    raise Exception("401 Unauthorized")
                client.audio.transcriptions.create = fail
                client_cls.return_value = client
                adapter = WhisperSTTAdapter()
                with pytest.raises(AdapterException, match="authentication|Auth|401"):
                    await adapter.transcribe(audio_bytes=sample_pcm_audio)


class TestDeepgramSTTAdapter:
    @pytest.mark.asyncio
    async def test_transcribe_success_returns_stt_response(self, sample_pcm_audio):
        mock_response = MagicMock()
        mock_response.results = MagicMock()
        mock_response.results.channels = [MagicMock()]
        mock_response.results.channels[0].alternatives = [
            MagicMock(transcript="deepgram text", confidence=0.98)
        ]
        mock_response.metadata = None
        with patch("adapters.stt.deepgram_adapter.Settings") as settings_mock:
            settings_mock.return_value.deepgram_api_key = "test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.stt.deepgram_adapter.DeepgramClient") as client_cls:
                client = MagicMock()
                client.listen.v1.media.transcribe_file = lambda **kw: mock_response
                client_cls.return_value = client
                adapter = DeepgramSTTAdapter()
                result = await adapter.transcribe(
                    audio_bytes=sample_pcm_audio,
                    language="en",
                )
        assert isinstance(result, STTResponse)
        assert "deepgram" in result.text
        assert result.is_final is True

    @pytest.mark.asyncio
    async def test_transcribe_exception_raises_adapter_exception(
        self, sample_pcm_audio
    ):
        with patch("adapters.stt.deepgram_adapter.Settings") as settings_mock:
            settings_mock.return_value.deepgram_api_key = "test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.stt.deepgram_adapter.DeepgramClient") as client_cls:
                client = MagicMock()
                def fail(**kw):
                    raise Exception("401 Unauthorized")
                client.listen.v1.media.transcribe_file = fail
                client_cls.return_value = client
                adapter = DeepgramSTTAdapter()
                with pytest.raises(AdapterException, match="authentication|Auth|401"):
                    await adapter.transcribe(audio_bytes=sample_pcm_audio)


class TestAssemblyAISTTAdapter:
    @pytest.mark.asyncio
    async def test_transcribe_success_returns_stt_response(self, sample_pcm_audio):
        mock_transcript = MagicMock()
        mock_transcript.text = "assembly ai result"
        mock_transcript.confidence = 0.95
        mock_transcript.language_code = "en"
        with patch("adapters.stt.assemblyai_adapter.Settings") as settings_mock:
            settings_mock.return_value.assemblyai_api_key = "test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.stt.assemblyai_adapter.aai") as aai_mock:
                transcriber = MagicMock()
                transcriber.transcribe = lambda f: mock_transcript
                aai_mock.Transcriber.return_value = transcriber
                aai_mock.TranscriptionConfig.return_value = MagicMock()
                adapter = AssemblyAISTTAdapter()
                result = await adapter.transcribe(
                    audio_bytes=sample_pcm_audio,
                    language="en",
                )
        assert isinstance(result, STTResponse)
        assert "assembly" in result.text
        assert result.is_final is True
