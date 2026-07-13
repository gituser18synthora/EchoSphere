from enum import Enum
from pydantic import BaseModel, Field


class ResponseStyle(str, Enum):
    CONCISE_DIRECT = "Concise & Direct"
    FRIENDLY_DETAILED = "Friendly & Detailed"
    PROFESSIONAL = "Professional"
    EMPATHETIC = "Empathetic"


class ShortTermMemoryScope(str, Enum):
    SESSION_ONLY = "Session Only"
    PERSISTED = "Persisted"


class Tab3GuardrailsConfig(BaseModel):
    """Mirrors engine.guardrails_config in Mongo / VoicebotConfig."""

    allow_user_provided_data: bool = True


class Tab3AIEngineRequest(BaseModel):
    # Section 1 — AI Engine Configuration
    llm_model_id: str = ""          # "GPT-40 RealTime", "claude-3-5-sonnet" etc
    stt_provider_id: str = ""       # "Whisper Turbo", "Deepgram" etc
    tts_provider_id: str = ""       # "DevenLabs Premium", "Azure TTS" etc

    # Section 2 — Voice Selection & Preview
    voice_id: str = ""              # "Samantha - Confident Female"
    voice_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    voice_pitch: float = Field(default=1.0, ge=0.5, le=2.0)

    # Section 3 — Prompt Configuration Studio
    system_role: str = ""
    primary_objectives: str = ""
    guardrails: str = ""
    guardrails_config: Tab3GuardrailsConfig = Field(
        default_factory=Tab3GuardrailsConfig,
    )
    response_style: ResponseStyle = ResponseStyle.CONCISE_DIRECT

    # Section 4 — Context & Memory Architecture
    short_term_memory_scope: ShortTermMemoryScope = ShortTermMemoryScope.SESSION_ONLY
    memory_expiry_days: int = Field(default=30, ge=1, le=3650)
    long_term_crm_linked: bool = True
    context_recall_between_calls: bool = True
    enable_rag: bool = True

    # Section 5 — Advanced Intelligence Controls
    # UI shows 0-100 as percentage, stored as 0.0-1.0 internally
    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_response_latency: float = Field(default=1.5, gt=0)
    fallback_model_id: str = "OPT-40 Mini"


class Tab3AIEngineResponse(BaseModel):
    voicebot_id: str
    tab: str = "ai_engine"
    data: Tab3AIEngineRequest