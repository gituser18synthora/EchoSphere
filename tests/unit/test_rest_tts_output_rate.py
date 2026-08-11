"""REST TTS adapters must advertise the PCM rate they actually emit.

EchoTTSService.run_tts resamples provider audio only when the provider
exposes ``output_sample_rate`` (voice_runtime/services.py) — a missing
attribute means 24 kHz OpenAI or 16 kHz Sarvam PCM gets stamped with the
pipeline rate and plays at the wrong speed.
"""

from shared.providers.base import ProviderConfig
from shared.providers.tts.openai_tts import _OPENAI_PCM_RATE, OpenAITTS
from shared.providers.tts.sarvam import _PCM_RATE, SarvamTTS


def test_openai_tts_advertises_its_pcm_rate(monkeypatch):
    monkeypatch.setenv("UNIT_TEST_TTS_KEY", "unit-test-key-not-real")
    provider = OpenAITTS(ProviderConfig(
        provider="openai", api_key_reference="env:UNIT_TEST_TTS_KEY",
    ))
    assert provider.output_sample_rate == _OPENAI_PCM_RATE == 24000


def test_sarvam_rest_advertises_its_pcm_rate(monkeypatch):
    monkeypatch.setenv("UNIT_TEST_TTS_KEY", "unit-test-key-not-real")
    provider = SarvamTTS(ProviderConfig(
        provider="sarvam", api_key_reference="env:UNIT_TEST_TTS_KEY",
    ))
    # synthesize() decodes the provider WAV and resamples any other rate to
    # _PCM_RATE before yielding, so the advertised rate is the emitted rate.
    assert provider.output_sample_rate == _PCM_RATE == 16000
