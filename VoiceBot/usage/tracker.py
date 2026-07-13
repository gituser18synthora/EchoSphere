"""Per-call usage accumulation for STT, LLM, and TTS (voicebot Kafka billing)."""

from dataclasses import dataclass, field

from voicebot.adapters.base import LLMResponse

# FreeSWITCH / voice pipeline: PCM 8 kHz, 16-bit mono
_PCM_SAMPLE_RATE = 8000
_PCM_BYTES_PER_SAMPLE = 2


def pcm_audio_duration_seconds(audio_bytes: bytes) -> float:
    if not audio_bytes:
        return 0.0
    return len(audio_bytes) / (_PCM_SAMPLE_RATE * _PCM_BYTES_PER_SAMPLE)


@dataclass
class CallUsageStats:
    stt_audio_seconds: float = 0.0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    tts_audio_seconds: float = 0.0
    tts_character_count: int = 0

    def record_stt(self, audio_bytes: bytes) -> None:
        self.stt_audio_seconds += pcm_audio_duration_seconds(audio_bytes)

    def record_tts(self, audio_bytes: bytes, *, characters: int = 0) -> None:
        self.tts_audio_seconds += pcm_audio_duration_seconds(audio_bytes)
        if characters > 0:
            self.tts_character_count += characters

    def record_llm(self, response: LLMResponse) -> None:
        self.llm_input_tokens += int(response.input_tokens or 0)
        self.llm_output_tokens += int(response.output_tokens or 0)

    @property
    def llm_total_tokens(self) -> int:
        return self.llm_input_tokens + self.llm_output_tokens
