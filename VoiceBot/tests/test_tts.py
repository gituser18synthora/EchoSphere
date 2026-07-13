"""Tests for TTS adapters: synthesize success, stream yields chunks, errors."""

from unittest.mock import MagicMock, patch

import pytest

from adapters.base import AdapterException, TTSResponse
from adapters.tts.elevenlabs_adapter import ElevenLabsTTSAdapter
from adapters.tts.azure_adapter import AzureTTSAdapter
from adapters.tts.google_adapter import GoogleTTSAdapter


class TestElevenLabsTTSAdapter:
    @pytest.mark.asyncio
    async def test_synthesize_success_returns_tts_response(self):
        fake_pcm = b"\x00\x01" * 1000  # 22050 Hz 16-bit chunk
        with patch("adapters.tts.elevenlabs_adapter.Settings") as settings_mock:
            settings_mock.return_value.elevenlabs_api_key = "test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.elevenlabs_adapter.ElevenLabs") as client_cls:
                client = MagicMock()
                client.text_to_speech.convert.return_value = iter([fake_pcm])
                client_cls.return_value = client
                adapter = ElevenLabsTTSAdapter()
                result = await adapter.synthesize(
                    text="Hello",
                    voice_id="voice_xyz",
                )
        assert isinstance(result, TTSResponse)
        assert result.sample_rate == 8000
        assert len(result.audio_bytes) > 0
        assert result.duration_ms >= 0

    @pytest.mark.asyncio
    async def test_synthesize_stream_yields_chunks(self):
        chunk1 = b"\x00\x01" * 100
        chunk2 = b"\x00\x01" * 100
        with patch("adapters.tts.elevenlabs_adapter.Settings") as settings_mock:
            settings_mock.return_value.elevenlabs_api_key = "test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.elevenlabs_adapter.ElevenLabs") as client_cls:
                client = MagicMock()
                client.text_to_speech.convert.return_value = iter([chunk1, chunk2])
                client_cls.return_value = client
                adapter = ElevenLabsTTSAdapter()
                chunks = []
                async for c in adapter.synthesize_stream(
                    text="Hi",
                    voice_id="voice_xyz",
                ):
                    chunks.append(c)
        assert len(chunks) >= 1
        assert sum(len(c) for c in chunks) > 0

    @pytest.mark.asyncio
    async def test_synthesize_auth_failure_raises_adapter_exception(self):
        with patch("adapters.tts.elevenlabs_adapter.Settings") as settings_mock:
            settings_mock.return_value.elevenlabs_api_key = "test"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.elevenlabs_adapter.ElevenLabs") as client_cls:
                client = MagicMock()
                client.text_to_speech.convert.side_effect = Exception("401 Unauthorized")
                client_cls.return_value = client
                adapter = ElevenLabsTTSAdapter()
                with pytest.raises(AdapterException, match="authentication"):
                    await adapter.synthesize(
                        text="Hello",
                        voice_id="voice_xyz",
                    )


class TestAzureTTSAdapter:
    @pytest.mark.asyncio
    async def test_synthesize_success_returns_tts_response(self):
        import azure.cognitiveservices.speech as speechsdk
        mock_result = MagicMock()
        mock_result.reason = speechsdk.ResultReason.SynthesizingAudioCompleted
        mock_result.error_details = None
        pull_stream = MagicMock()
        read_calls = [0]
        def read_impl(buf):
            read_calls[0] += 1
            if read_calls[0] == 1:
                return 3200
            return 0
        pull_stream.read = read_impl
        with patch("adapters.tts.azure_adapter.Settings") as settings_mock:
            settings_mock.return_value.azure_speech_key = "key"
            settings_mock.return_value.azure_speech_region = "eastus"
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.azure_adapter.speechsdk.SpeechConfig"):
                with patch("adapters.tts.azure_adapter.speechsdk.audio.PullAudioOutputStream", return_value=pull_stream):
                    with patch("adapters.tts.azure_adapter.speechsdk.audio.AudioConfig"):
                        with patch("adapters.tts.azure_adapter.speechsdk.SpeechSynthesizer") as syn_cls:
                            synthesizer = MagicMock()
                            synthesizer.speak_text_async.return_value.get.return_value = mock_result
                            syn_cls.return_value = synthesizer
                            adapter = AzureTTSAdapter()
                            result = await adapter.synthesize(
                                text="Hello",
                                voice_id="en-US-JennyNeural",
                            )
        assert isinstance(result, TTSResponse)
        assert result.sample_rate == 8000


class TestGoogleTTSAdapter:
    @pytest.mark.asyncio
    async def test_synthesize_success_returns_tts_response(self):
        mock_response = MagicMock()
        mock_response.audio_content = b"\x00\x01" * 800  # 8kHz 16-bit
        with patch("adapters.tts.google_adapter.Settings") as settings_mock:
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.google_adapter.texttospeech.TextToSpeechClient") as client_cls:
                client = MagicMock()
                client.synthesize_speech.return_value = mock_response
                client_cls.return_value = client
                adapter = GoogleTTSAdapter()
                result = await adapter.synthesize(
                    text="Hello",
                    voice_id="en-US-Standard-A",
                )
        assert isinstance(result, TTSResponse)
        assert result.sample_rate == 8000
        assert len(result.audio_bytes) > 0

    @pytest.mark.asyncio
    async def test_synthesize_stream_yields_chunk(self):
        mock_response = MagicMock()
        mock_response.audio_content = b"\x00\x01" * 800
        with patch("adapters.tts.google_adapter.Settings") as settings_mock:
            settings_mock.return_value.stt_tts_max_latency = 2.0
            with patch("adapters.tts.google_adapter.texttospeech.TextToSpeechClient") as client_cls:
                client = MagicMock()
                client.synthesize_speech.return_value = mock_response
                client_cls.return_value = client
                adapter = GoogleTTSAdapter()
                chunks = []
                async for c in adapter.synthesize_stream(
                    text="Hi",
                    voice_id="en-US-Standard-A",
                ):
                    chunks.append(c)
        assert len(chunks) == 1
        assert len(chunks[0]) > 0
