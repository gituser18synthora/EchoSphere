from pydantic_settings import BaseSettings, SettingsConfigDict
import os

# Resolve .env paths: voicebot/.env and project root .env (so root .env is used when present)
_CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
_VOICEBOT_DIR = os.path.normpath(os.path.join(_CONFIG_DIR, ".."))
_PROJECT_ROOT = os.path.normpath(os.path.join(_VOICEBOT_DIR, ".."))
_ENV_PATHS = [
    os.path.join(_PROJECT_ROOT, ".env"),
    os.path.join(_VOICEBOT_DIR, ".env"),
]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_PATHS,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # MongoDB — server URI from .env (empty allowed for tests; connect() validates)
    mongo_uri: str = ""
    mongo_db_name: str = "VoicebotDB"

    # Redis (default for local dev if omitted from .env)
    redis_url: str = "redis://localhost:6379"

    # LLM (default "" so missing env does not raise ValidationError; pydantic-settings reads .env)
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    google_api_key: str = ""

    # STT (whisper uses openai_api_key)
    deepgram_api_key: str = ""
    assemblyai_api_key: str = ""
    sarvam_api_key: str = ""

    # TTS
    elevenlabs_api_key: str = ""
    azure_speech_key: str = ""
    azure_speech_region: str = ""

    # Adapter timeouts (seconds) — hard network timeout before raising AdapterException
    llm_max_response_latency: float = 10.0
    stt_tts_max_latency: float = 8.0

    mcp_server_url: str = "http://localhost:8001"
    mcp_api_key: str = ""
