from voicebot.api.schemas.tab2_persona import (
    EmpathyLevel,
    EscalationThreshold,
    FormattingStyle,
    GreetingStyle,
    IndustryContext,
    InterruptHandling,
    LanguageSimplicity,
    PersonalityType,
    ResponseLength,
    Tab2PersonaRequest,
    Tab2PersonaResponse,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set
from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()

# MongoDB key where all Tab 2 fields are stored
_SECTION = "persona_behaviour"


async def get_tab2(voicebot_id: str) -> Tab2PersonaResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    pb = doc.get(_SECTION) or {}

    # Helper — safely coerce a raw string into an enum, fall back to default
    def _enum(cls, raw, default):
        try:
            return cls(raw)
        except (ValueError, KeyError):
            return default

    data = Tab2PersonaRequest(
        # Section 1: Persona
        agent_role=pb.get("agent_role", ""),
        industry_context=_enum(IndustryContext, pb.get("industry_context"), IndustryContext.OTHER),
        personality_type=_enum(PersonalityType, pb.get("personality_type"), PersonalityType.PROFESSIONAL),
        empathy_level=_enum(EmpathyLevel, pb.get("empathy_level"), EmpathyLevel.MEDIUM),
        enable_proactive_assistance=pb.get("enable_proactive_assistance", False),

        # Section 2: Communication Behaviour
        greeting_style=_enum(GreetingStyle, pb.get("greeting_style"), GreetingStyle.FORMAL),
        response_length=_enum(ResponseLength, pb.get("response_length"), ResponseLength.SHORT),
        interrupt_handling=_enum(InterruptHandling, pb.get("interrupt_handling"), InterruptHandling.ALLOW_INTERRUPTION),
        escalation_threshold=_enum(EscalationThreshold, pb.get("escalation_threshold"), EscalationThreshold.MEDIUM),

        # Section 3: Response Formatting
        formatting_style=_enum(FormattingStyle, pb.get("formatting_style"), FormattingStyle.PLAIN_TEXT),
        language_simplicity=_enum(LanguageSimplicity, pb.get("language_simplicity"), LanguageSimplicity.BASIC),
        enable_confirmation_prompts=pb.get("enable_confirmation_prompts", False),
        enable_response_summaries=pb.get("enable_response_summaries", False),
    )
    return Tab2PersonaResponse(voicebot_id=voicebot_id, data=data)


async def save_tab2(voicebot_id: str, body: Tab2PersonaRequest) -> Tab2PersonaResponse:
    # Flatten all 13 fields under persona_behaviour.*
    set_fields = flatten_for_set(_SECTION, body.model_dump(mode="json"))

    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    await cache.invalidate(voicebot_id)
    return await get_tab2(voicebot_id)
