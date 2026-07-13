from enum import Enum
from pydantic import BaseModel, Field


class IntentDetectionModel(str, Enum):
    LLM_NATIVE = "LLM Native Intent Parsing"
    CUSTOM = "Custom"


class BelowThresholdAction(str, Enum):
    ASK_CLARIFYING = "Ask Clarying Question"
    TRANSFER = "Transfer to Human"
    REPEAT = "Repeat"


class KnowledgeSource(str, Enum):
    CRM = "CRM Customer Data"
    RAG = "RAG Knowledge Base"
    FAQ = "FAQ Structured Answers"
    LLM = "LLM General Knowledge"


class ContextWindowTokens(str, Enum):
    TOKENS_2048 = "2048"
    TOKENS_4006 = "4006"
    TOKENS_8192 = "8192"
    TOKENS_16384 = "16384"


class MemoryType(str, Enum):
    STRUCTURED = "Structured Memory (CRM Fields)"
    SESSION_ONLY = "Session Only"
    FULL_TRANSCRIPT = "Full Transcript"


class ResponseDepth(str, Enum):
    CONCISE = "Concise"
    DETAILED = "Detailed"
    ADAPTIVE = "Adaptive"


class Tab4ConversationRequest(BaseModel):
    # Section 1 — Language Engine
    primary_language: str = "English"
    fallback_language: str = "English"
    auto_language_detection: bool = True

    # Section 2 — Intent Recognition & Accuracy Layer
    intent_detection_model: IntentDetectionModel = IntentDetectionModel.LLM_NATIVE
    below_threshold_action: BelowThresholdAction = BelowThresholdAction.ASK_CLARIFYING
    # Stored as 0–100 (percentage) exactly as shown in UI slider
    min_confidence_threshold: int = Field(default=75, ge=1, le=100)

    # Section 3 — Knowledge Source Priority (ordered list, index = priority)
    knowledge_source_priority: list[KnowledgeSource] = [
        KnowledgeSource.CRM,
        KnowledgeSource.RAG,
        KnowledgeSource.FAQ,
        KnowledgeSource.LLM,
    ]

    # Section 4 — Context Stitching & Memory Control
    cross_session_recall: bool = True
    context_window_tokens: ContextWindowTokens = ContextWindowTokens.TOKENS_4006
    memory_type: MemoryType = MemoryType.STRUCTURED
    memory_expiry_days: int = Field(default=150, ge=1, le=3650)

    # Section 5 — Response Strategy
    sentiment_adaptation: bool = True
    smart_clarification: bool = True
    response_depth: ResponseDepth = ResponseDepth.CONCISE


class Tab4ConversationResponse(BaseModel):
    voicebot_id: str
    tab: str = "conversation_intelligence"
    data: Tab4ConversationRequest