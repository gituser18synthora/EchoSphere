from voicebot.api.schemas.tab4_conversation import (
    BelowThresholdAction,
    ContextWindowTokens,
    IntentDetectionModel,
    KnowledgeSource,
    MemoryType,
    ResponseDepth,
    Tab4ConversationRequest,
    Tab4ConversationResponse,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set
from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()

# Tab 4 writes to "conversation_intelligence" — exact key ConfigLoader already reads
_SECTION = "conversation_intelligence"


async def get_tab4(voicebot_id: str) -> Tab4ConversationResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    ci = doc.get(_SECTION) or {}

    def _enum(cls, raw, default):
        try:
            return cls(raw)
        except (ValueError, KeyError):
            return default

    # knowledge_source_priority stored as list of strings — coerce each to enum
    raw_priority = ci.get("knowledge_source_priority", [])
    if raw_priority:
        priority = []
        for item in raw_priority:
            coerced = _enum(KnowledgeSource, item, None)
            if coerced:
                priority.append(coerced)
        if not priority:
            priority = [
                KnowledgeSource.CRM,
                KnowledgeSource.RAG,
                KnowledgeSource.FAQ,
                KnowledgeSource.LLM,
            ]
    else:
        priority = [
            KnowledgeSource.CRM,
            KnowledgeSource.RAG,
            KnowledgeSource.FAQ,
            KnowledgeSource.LLM,
        ]

    data = Tab4ConversationRequest(
        # Section 1
        primary_language=ci.get("primary_language", "English"),
        fallback_language=ci.get("fallback_language", "English"),
        auto_language_detection=ci.get("auto_language_detection", True),

        # Section 2
        intent_detection_model=_enum(
            IntentDetectionModel,
            ci.get("intent_detection_model"),
            IntentDetectionModel.LLM_NATIVE,
        ),
        below_threshold_action=_enum(
            BelowThresholdAction,
            ci.get("below_threshold_action"),
            BelowThresholdAction.ASK_CLARIFYING,
        ),
        min_confidence_threshold=ci.get("min_confidence_threshold", 75),

        # Section 3
        knowledge_source_priority=priority,

        # Section 4
        cross_session_recall=ci.get("cross_session_recall", True),
        context_window_tokens=_enum(
            ContextWindowTokens,
            ci.get("context_window_tokens"),
            ContextWindowTokens.TOKENS_4006,
        ),
        memory_type=_enum(
            MemoryType,
            ci.get("memory_type"),
            MemoryType.STRUCTURED,
        ),
        memory_expiry_days=ci.get("memory_expiry_days", 150),

        # Section 5
        sentiment_adaptation=ci.get("sentiment_adaptation", True),
        smart_clarification=ci.get("smart_clarification", True),
        response_depth=_enum(
            ResponseDepth,
            ci.get("response_depth"),
            ResponseDepth.CONCISE,
        ),
    )
    return Tab4ConversationResponse(voicebot_id=voicebot_id, data=data)


async def save_tab4(voicebot_id: str, body: Tab4ConversationRequest) -> Tab4ConversationResponse:
    set_fields = flatten_for_set(_SECTION, body.model_dump(mode="json"))

    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    await cache.invalidate(voicebot_id)
    return await get_tab4(voicebot_id)