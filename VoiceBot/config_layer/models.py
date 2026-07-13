"""Pydantic models for voicebot configuration."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# --- Enums ---


class ConversationStyle(str, Enum):
    PROFESSIONAL_FORMAL = "professional_formal"
    FRIENDLY_CASUAL = "friendly_casual"
    EMPATHETIC = "empathetic"
    CONCISE = "concise"


class ResponseStyle(str, Enum):
    CONCISE_DIRECT = "concise_direct"
    FRIENDLY_DETAILED = "friendly_detailed"
    PROFESSIONAL = "professional"
    EMPATHETIC = "empathetic"


class FallbackAction(str, Enum):
    TRANSFER_TO_AGENT = "transfer_to_agent"
    VOICEMAIL = "voicemail"
    END_CALL = "end_call"


class BelowThresholdAction(str, Enum):
    ASK_CLARIFYING = "ask_clarifying"
    TRANSFER = "transfer"
    REPEAT = "repeat"


class ResponseDepth(str, Enum):
    CONCISE = "concise"
    DETAILED = "detailed"
    ADAPTIVE = "adaptive"


class CRMType(str, Enum):
    SALESFORCE = "salesforce"
    HUBSPOT = "hubspot"
    ZOHO = "zoho"
    CUSTOM = "custom"
    NONE = "none"


class VoicebotStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ShortTermMemoryScope(str, Enum):
    """Short-term memory (per call) — maps to UI "Short-Term Memory (Per Call)"."""
    SESSION_ONLY = "session_only"
    PERSISTED = "persisted"


# --- Section Models ---


class PersonalityConfig(BaseModel):
    greeting_message: str = "Hello! How can I help you today?"
    conversation_style: ConversationStyle = ConversationStyle.PROFESSIONAL_FORMAL
    knowledge_base_source: str = "rag"
    knowledge_base_id: str | None = None


class GoalsConfig(BaseModel):
    book_appointments: bool = False
    capture_lead: bool = False
    answer_faqs: bool = False
    route_to_human: bool = False
    send_sms_followup: bool = False
    crm_integration_type: CRMType = CRMType.NONE
    crm_config: dict[str, Any] = Field(default_factory=dict)


class IntentConfig(BaseModel):
    """
    Intent classification config: always-present intents, goal intents, and goal flag mapping.
    Fully configurable per voicebot; set when configuring and stored in DB; used by IntentEngine.
    """
    always_present_intents: dict[str, str] = Field(
        description="Intent id -> description for intents always available",
    )
    goal_intent_descriptions: dict[str, str] = Field(
        description="Intent id -> description for goal-based intents",
    )
    goal_flag_map: dict[str, str] = Field(
        description="Intent id -> GoalsConfig attribute name (e.g. book_appointment -> book_appointments)",
    )


class EscalationConfig(BaseModel):
    transfer_conditions: str = ""
    max_call_duration: int = 10  # minutes
    fallback_action: FallbackAction = FallbackAction.TRANSFER_TO_AGENT
    transfer_message: str = ""

    def transfer_keywords(self) -> list[str]:
        """Parse comma-separated transfer_conditions into keyword list."""
        return [
            k.strip().lower()
            for k in self.transfer_conditions.split(",")
            if k.strip()
        ]


class AvailabilityConfig(BaseModel):
    working_hours_start: str = "00:00"
    working_hours_end: str = "23:59"
    timezone: str = "UTC"
    enable_24x7: bool = False


class EngineGuardrailsConfig(BaseModel):
    """
    Fine-grained guardrails behavior. Stored under engine.guardrails_config in Mongo.
    When missing from DB, defaults apply (allow caller-echo for confidential patterns).
    """

    allow_user_provided_data: bool = True


class EngineConfig(BaseModel):
    """
    Voice AI Control Hub — Voicebot Engine.
    Maps to UI: AI Engine Configuration, Voice Selection, Prompt Configuration Studio,
    Context & Memory Architecture, Advanced Intelligence Controls.
    """
    # --- AI Engine Configuration ---
    llm_provider_id: str
    llm_model_id: str
    stt_provider_id: str
    tts_provider_id: str

    # --- Voice Selection & Preview ---
    voice_id: str
    voice_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    voice_pitch: float = Field(default=1.0, ge=0.5, le=2.0)

    # --- Prompt Configuration Studio ---
    system_role: str
    primary_objectives: str
    guardrails: str = ""
    guardrails_config: EngineGuardrailsConfig = Field(
        default_factory=EngineGuardrailsConfig,
    )
    response_style: ResponseStyle = ResponseStyle.CONCISE_DIRECT

    # --- Context & Memory Architecture ---
    short_term_memory_scope: ShortTermMemoryScope = ShortTermMemoryScope.SESSION_ONLY
    memory_expiry_days: int = Field(default=180, ge=1, le=3650)
    long_term_crm_linked: bool = True
    context_recall_between_calls: bool = True
    enable_rag: bool = True

    # --- Advanced Intelligence Controls ---
    # confidence_threshold: 0.0–1.0 (UI may show as % 0–100; API should send 0.75 for 75%)
    confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_response_latency: float = Field(default=8.0, gt=0)
    fallback_provider_id: str = "openai"
    fallback_model_id: str = "gpt-4o-mini"


class ConversationIntelligenceConfig(BaseModel):
    primary_language: str = "en"
    auto_language_detection: bool = True
    fallback_language: str = "en"
    intent_detection_model: str = "llm_native"
    min_confidence_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    below_threshold_action: BelowThresholdAction = (
        BelowThresholdAction.ASK_CLARIFYING
    )
    knowledge_source_priority: list[str] = Field(
        default=["crm", "rag", "faq", "llm"]
    )
    context_window_tokens: int = 4096
    cross_session_recall: bool = True
    memory_type: str = "structured"
    memory_expiry_days: int = 150
    response_depth: ResponseDepth = ResponseDepth.CONCISE
    sentiment_adaptation: bool = True
    smart_clarification: bool = True


# --- Root Config Model ---


class VoicebotConfig(BaseModel):
    """
    Complete voicebot configuration.
    Assembled from voicebot_configs MongoDB document.
    Cached in Redis as JSON.
    Consumed by Orchestrator at call start.
    """

    voicebot_id: str
    tenant_id: str
    name: str
    business_name: str
    status: VoicebotStatus = VoicebotStatus.DRAFT
    phone_number_id: str | None = None

    personality: PersonalityConfig = Field(default_factory=PersonalityConfig)
    goals: GoalsConfig = Field(default_factory=GoalsConfig)
    intent_config: IntentConfig = Field(
        default_factory=lambda: IntentConfig(
            always_present_intents={
                "greeting": "Caller is greeting or starting the conversation",
                "goodbye": "Caller wants to end the call",
                "general_query": "Caller is asking a general question",
                "privacy_request": "Caller wants their personal data deleted",
                "unclear": "Caller utterance is unclear or unintelligible",
            },
            goal_intent_descriptions={
                "book_appointment": "Caller wants to schedule or book an appointment",
                "capture_lead": "Caller is a new prospect expressing interest",
                "answer_faq": "Caller is asking a frequently asked question",
                "route_to_human": "Caller wants to speak to a human agent",
                "send_followup": "Caller requests follow-up via SMS or WhatsApp",
            },
            goal_flag_map={
                "book_appointment": "book_appointments",
                "capture_lead": "capture_lead",
                "answer_faq": "answer_faqs",
                "route_to_human": "route_to_human",
                "send_followup": "send_sms_followup",
            },
        )
    )
    escalation: EscalationConfig
    availability: AvailabilityConfig
    engine: EngineConfig
    conversation_intelligence: ConversationIntelligenceConfig

    persona_behaviour: dict[str, Any] | None = None
    caller_authentication: dict[str, Any] | None = None
    call_data_extraction: dict[str, Any] | None = None
    actions_automation: dict[str, Any] | None = None

    def has_any_goal_enabled(self) -> bool:
        g = self.goals
        return any([
            g.book_appointments,
            g.capture_lead,
            g.answer_faqs,
            g.route_to_human,
            g.send_sms_followup,
        ])

    def to_cache_dict(self) -> dict:
        """Serialize to dict for Redis storage."""
        return self.model_dump(mode="json")

    @classmethod
    def from_cache_dict(cls, data: dict) -> "VoicebotConfig":
        """Deserialize from Redis cached dict."""
        return cls.model_validate(data)


class ModelProvider(BaseModel):
    provider_id: str
    type: str  # "llm" | "stt" | "tts"
    adapter_class: str
    display_name: str
    models: list[str]
    min_tier: str
    is_active: bool = True
