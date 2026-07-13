from datetime import datetime

from voicebot.api.schemas.tab1_setup import (
    AvailabilityConfigPayload,
    CRMCredentials,
    CRMType,
    EscalationConfigPayload,
    FallbackAction,
    Tab1SetupRequest,
    GoalsConfig,
    Tab1SetupResponse,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set
from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()



def _enum(cls, raw, default):
    try:
        return cls(raw)
    except (ValueError, KeyError):
        return default


async def create_tab1(voicebot_id: str, body: Tab1SetupRequest) -> Tab1SetupResponse:
    """
    INSERT brand new voicebot_configs document.
    Called on POST /voicebots/config/setup (first time).
    tenant_id comes from request body.
    """
    doc = {
        "voicebot_id": voicebot_id,
        "tenant_id": body.tenant_id,          # ← from request body
        "name": body.voicebot_name,
        "business_name": body.business_name,
        "status": "draft",
        "phone_number_id": body.availability.phone_number_id,

        "crm_integration_type": body.crm_integration_type.value,
        "crm_config": {
            "crm_account_id": body.crm_credentials.crm_account_id,
            "api_key": body.crm_credentials.api_key,
            "webhook_url": body.crm_credentials.webhook_url,
        },
        
        "goals": body.goals.model_dump(),

        "escalation": {
            "max_call_duration": body.escalation.max_call_duration,
            "fallback_action": body.escalation.fallback_action.value,
            "transfer_message": body.escalation.transfer_message,
            "transfer_conditions": "",
        },

        "availability": {
            "enable_24x7": body.availability.enable_24x7,
            "working_hours_start": body.availability.working_hours_start,
            "working_hours_end": body.availability.working_hours_end,
            "timezone": body.availability.timezone,
        },

        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    await MongoDB.voicebot_configs().insert_one(doc)
    return Tab1SetupResponse(
        voicebot_id=voicebot_id,
        tenant_id=body.tenant_id,
        data=body,
    )


async def get_tab1(voicebot_id: str) -> Tab1SetupResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    crm_cfg = doc.get("crm_config") or {}
    esc = doc.get("escalation") or {}
    av = doc.get("availability") or {}
    goals_doc = doc.get("goals") or {}

    data = Tab1SetupRequest(
        tenant_id=doc.get("tenant_id", ""),   # ← read back from doc
        voicebot_name=doc.get("name", ""),
        business_name=doc.get("business_name", ""),

        crm_integration_type=_enum(
            CRMType,
            doc.get("crm_integration_type"),
            CRMType.NONE,
        ),
        crm_credentials=CRMCredentials(
            crm_account_id=crm_cfg.get("crm_account_id", ""),
            api_key=crm_cfg.get("api_key", ""),
            webhook_url=crm_cfg.get("webhook_url", ""),
        ),
        
        goals=GoalsConfig(
            book_appointments=goals_doc.get("book_appointments", False),
            capture_lead=goals_doc.get("capture_lead", False),
            answer_faqs=goals_doc.get("answer_faqs", False),
            route_to_human=goals_doc.get("route_to_human", False),
            send_sms_followup=goals_doc.get("send_sms_followup", False),
        ),

        escalation=EscalationConfigPayload(
            max_call_duration=esc.get("max_call_duration", 10),
            fallback_action=_enum(
                FallbackAction,
                esc.get("fallback_action"),
                FallbackAction.TRANSFER_TO_AGENT,
            ),
            transfer_message=esc.get("transfer_message", ""),
        ),

        availability=AvailabilityConfigPayload(
            phone_number_id=doc.get("phone_number_id"),
            enable_24x7=av.get("enable_24x7", False),
            working_hours_start=av.get("working_hours_start", "09:00"),
            working_hours_end=av.get("working_hours_end", "09:00"),
            timezone=av.get("timezone", "UTC"),
        ),
    )
    return Tab1SetupResponse(
        voicebot_id=voicebot_id,
        tenant_id=doc.get("tenant_id", ""),
        data=data,
    )


async def save_tab1(voicebot_id: str, body: Tab1SetupRequest) -> Tab1SetupResponse:
    """PATCH existing document. Called on PUT."""
    esc_patch = body.escalation.model_dump(mode="json")
    av_patch = body.availability.model_dump(mode="json", exclude={"phone_number_id"})

    set_fields: dict = {
        "tenant_id": body.tenant_id,          # ← update tenant_id if changed
        "name": body.voicebot_name,
        "business_name": body.business_name,
        "crm_integration_type": body.crm_integration_type.value,
        "crm_config.crm_account_id": body.crm_credentials.crm_account_id,
        "crm_config.api_key": body.crm_credentials.api_key,
        "crm_config.webhook_url": body.crm_credentials.webhook_url,
        "updated_at": datetime.utcnow(),
    }

    if body.availability.phone_number_id is not None:
        set_fields["phone_number_id"] = body.availability.phone_number_id
    
    set_fields.update(flatten_for_set("goals", body.goals.model_dump()))
    set_fields.update(flatten_for_set("escalation", esc_patch))
    set_fields.update(flatten_for_set("availability", av_patch))

    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    await cache.invalidate(voicebot_id)
    return await get_tab1(voicebot_id)