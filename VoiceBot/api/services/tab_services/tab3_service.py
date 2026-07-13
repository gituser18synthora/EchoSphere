from voicebot.api.schemas.tab3_ai_engine import (
    ResponseStyle,
    ShortTermMemoryScope,
    Tab3AIEngineRequest,
    Tab3AIEngineResponse,
    Tab3GuardrailsConfig,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set
from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()

# Tab 3 writes to the "engine" key — this is the EXACT key the
# VoiceBotOrchestrator and ConfigLoader already read. Do NOT rename.
_SECTION = "engine"


async def get_tab3(voicebot_id: str) -> Tab3AIEngineResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    e = doc.get(_SECTION) or {}
    gc = e.get("guardrails_config") or {}
    if not isinstance(gc, dict):
        gc = {}

    def _enum(cls, raw, default):
        try:
            return cls(raw)
        except (ValueError, KeyError):
            return default

    data = Tab3AIEngineRequest(
        # Section 1
        llm_model_id=e.get("llm_model_id", ""),
        stt_provider_id=e.get("stt_provider_id", ""),
        tts_provider_id=e.get("tts_provider_id", ""),

        # Section 2
        voice_id=e.get("voice_id", ""),
        voice_speed=e.get("voice_speed", 1.0),
        voice_pitch=e.get("voice_pitch", 1.0),

        # Section 3
        system_role=e.get("system_role", ""),
        primary_objectives=e.get("primary_objectives", ""),
        guardrails=e.get("guardrails", ""),
        guardrails_config=Tab3GuardrailsConfig(
            allow_user_provided_data=gc.get("allow_user_provided_data", True),
        ),
        response_style=_enum(ResponseStyle, e.get("response_style"), ResponseStyle.CONCISE_DIRECT),

        # Section 4
        short_term_memory_scope=_enum(
            ShortTermMemoryScope,
            e.get("short_term_memory_scope"),
            ShortTermMemoryScope.SESSION_ONLY,
        ),
        memory_expiry_days=e.get("memory_expiry_days", 30),
        long_term_crm_linked=e.get("long_term_crm_linked", True),
        context_recall_between_calls=e.get("context_recall_between_calls", True),
        enable_rag=e.get("enable_rag", True),

        # Section 5
        confidence_threshold=e.get("confidence_threshold", 0.75),
        max_response_latency=e.get("max_response_latency", 1.5),
        fallback_model_id=e.get("fallback_model_id", "OPT-40 Mini"),
    )
    return Tab3AIEngineResponse(voicebot_id=voicebot_id, data=data)


async def save_tab3(voicebot_id: str, body: Tab3AIEngineRequest) -> Tab3AIEngineResponse:
    set_fields = flatten_for_set(_SECTION, body.model_dump(mode="json"))

    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    await cache.invalidate(voicebot_id)
    return await get_tab3(voicebot_id)