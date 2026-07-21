"""Provider factory/config validation, mock providers, PCM + TTS-text utils."""

import os

import pytest

from shared.providers.base import ProviderConfig, ProviderError
from shared.providers.factory import (
    _REGISTRY,
    clear_provider_cache,
    get_llm_provider,
    get_stt_provider,
    get_tts_provider,
)

os.environ.setdefault("FAKE_TEST_KEY", "test-key")
KEY_REF = "env:FAKE_TEST_KEY"


class TestFactory:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ProviderError):
            get_stt_provider(ProviderConfig(provider="nope"))

    def test_mock_providers_construct(self):
        assert get_stt_provider(ProviderConfig(provider="mock"))
        assert get_tts_provider(ProviderConfig(provider="mock"))
        assert get_llm_provider(ProviderConfig(provider="mock"))

    def test_openai_family_requires_key(self, monkeypatch):
        clear_provider_cache()
        monkeypatch.delenv("MISSING_KEY_VAR", raising=False)
        with pytest.raises(ProviderError) as excinfo:
            get_stt_provider(
                ProviderConfig(provider="whisper", api_key_reference="env:MISSING_KEY_VAR")
            )
        assert excinfo.value.category == "auth"
        clear_provider_cache()

    def test_registry_covers_required_kinds(self):
        kinds = {k for k, _ in _REGISTRY}
        assert kinds == {"stt", "tts", "llm"}
        providers = {p for _, p in _REGISTRY}
        for required in ("openai", "deepgram", "assemblyai", "sarvam", "elevenlabs",
                         "azure", "google", "anthropic", "mock"):
            assert required in providers


class TestMockRoundTrip:
    async def test_tts_to_stt(self):
        tts = get_tts_provider(ProviderConfig(provider="mock"))
        stt = get_stt_provider(ProviderConfig(provider="mock"))
        result = await tts.synthesize("hello round trip")
        transcript = await stt.transcribe(result.audio)
        assert transcript.text == "hello round trip"

    async def test_llm_stream_joins_to_generate(self):
        llm = get_llm_provider(ProviderConfig(provider="mock"))
        gen = await llm.generate([{"role": "user", "content": "ping"}])
        streamed = "".join([t async for t in llm.stream([{"role": "user", "content": "ping"}])])
        assert gen.text.strip() == streamed.strip()

    async def test_llm_grounded_mode_quotes_context(self):
        llm = get_llm_provider(ProviderConfig(provider="mock"))
        out = await llm.generate(
            [{"role": "user", "content": "what is the grace period"}],
            system="rules...\nContext:\n[1] The grace period is 30 days.",
        )
        assert "30 days" in out.text


class TestPCM:
    def test_wav_round_trip(self):
        from shared.audio.pcm import pcm_to_wav_bytes, wav_to_pcm

        pcm = bytes(range(256)) * 8
        wav = pcm_to_wav_bytes(pcm, 16000)
        out, rate = wav_to_pcm(wav)
        assert out == pcm and rate == 16000

    def test_resample_halves_length(self):
        from shared.audio.pcm import resample_pcm

        one_second_16k = b"\x00\x01" * 16000
        out = resample_pcm(one_second_16k, 16000, 8000)
        assert len(out) == 16000  # 8000 samples * 2 bytes

    def test_same_rate_passthrough(self):
        from shared.audio.pcm import resample_pcm

        pcm = b"\x01\x02" * 100
        assert resample_pcm(pcm, 16000, 16000) == pcm


class TestTTSText:
    def test_sentence_split_with_abbreviations(self):
        from shared.audio.text import split_into_sentences

        parts = split_into_sentences("Hello world. This is Dr. Smith. Bye.")
        assert len(parts) == 3
        assert parts[1] == "This is Dr. Smith."

    def test_devanagari_danda(self):
        from shared.audio.text import split_into_sentences

        parts = split_into_sentences("नमस्ते। ठीक है।")
        assert len(parts) >= 1

    def test_sanitize_strips_markup(self):
        from shared.audio.text import sanitize_for_tts

        out = sanitize_for_tts("**bold** and `code`")
        assert "*" not in out and "`" not in out
