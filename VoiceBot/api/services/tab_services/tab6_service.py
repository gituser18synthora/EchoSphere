from voicebot.api.schemas.tab6_extraction import (
    CustomExtractionField,
    DataType,
    ExtractionMethod,
    StandardFields,
    StorageDestination,
    Tab6ExtractionRequest,
    Tab6ExtractionResponse,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set
from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()

_SECTION = "call_data_extraction"

_DEFAULT_DESTINATIONS = [
    {"destination": "Salesforce",        "destination_type": "CRM",       "enabled": True},
    {"destination": "HubSpot",           "destination_type": "CRM",       "enabled": True},
    {"destination": "Zendesk",           "destination_type": "Ticketing", "enabled": True},
    {"destination": "Internal Database", "destination_type": "Built-in",  "enabled": True},
    {"destination": "Custom Webhook",    "destination_type": "Enterprise", "enabled": True},
]


def _enum(cls, raw, default):
    try:
        return cls(raw)
    except (ValueError, KeyError):
        return default


def _parse_doc(voicebot_id: str, doc: dict) -> Tab6ExtractionResponse:
    cd = doc.get(_SECTION) or {}
    sf = cd.get("standard_fields") or {}

    standard_fields = StandardFields(
        customer_name=sf.get("customer_name", True),
        caller_phone_number=sf.get("caller_phone_number", True),
        call_intent_reason=sf.get("call_intent_reason", True),
        sentiment=sf.get("sentiment", True),
        language_detected=sf.get("language_detected", False),  # Off by default
        goal_outcome=sf.get("goal_outcome", True),
        call_duration=sf.get("call_duration", True),
    )

    custom_fields = [
        CustomExtractionField(
            field_name=f.get("field_name", ""),
            data_type=_enum(DataType, f.get("data_type"), DataType.STRING),
            extraction_method=_enum(
                ExtractionMethod,
                f.get("extraction_method"),
                ExtractionMethod.ENTITY_EXTRACTION,
            ),
            extraction_prompt=f.get("extraction_prompt", ""),
            required=f.get("required", False),
        )
        for f in (cd.get("custom_fields") or [])
    ]

    # Use saved destinations if present, else fall back to defaults
    raw_destinations = cd.get("storage_destinations") or _DEFAULT_DESTINATIONS
    storage_destinations = [
        StorageDestination(
            destination=d.get("destination", ""),
            destination_type=d.get("destination_type", ""),
            enabled=d.get("enabled", True),
        )
        for d in raw_destinations
    ]

    data = Tab6ExtractionRequest(
        standard_fields=standard_fields,
        custom_fields=custom_fields,
        storage_destinations=storage_destinations,
    )
    return Tab6ExtractionResponse(voicebot_id=voicebot_id, data=data)


async def get_tab6(voicebot_id: str) -> Tab6ExtractionResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return _parse_doc(voicebot_id, doc)


async def save_tab6(voicebot_id: str, body: Tab6ExtractionRequest) -> Tab6ExtractionResponse:
    """Full save — replaces entire call_data_extraction section."""
    set_fields = flatten_for_set(_SECTION, body.model_dump(mode="json"))
    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    await cache.invalidate(voicebot_id)
    return await get_tab6(voicebot_id)


async def add_custom_field(
    voicebot_id: str,
    field: CustomExtractionField,
) -> Tab6ExtractionResponse:
    """Append one row — used by + Add Custom Field button."""
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    existing = doc.get(_SECTION, {}).get("custom_fields", [])
    if any(f.get("field_name") == field.field_name for f in existing):
        raise ValueError(f"Custom field '{field.field_name}' already exists")

    await MongoDB.voicebot_configs().update_one(
        {"voicebot_id": voicebot_id},
        {"$push": {f"{_SECTION}.custom_fields": field.model_dump(mode="json")}},
    )
    await cache.invalidate(voicebot_id)
    return await get_tab6(voicebot_id)


async def delete_custom_field(
    voicebot_id: str,
    field_name: str,
) -> Tab6ExtractionResponse:
    """Remove one row by field_name."""
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    await MongoDB.voicebot_configs().update_one(
        {"voicebot_id": voicebot_id},
        {"$pull": {f"{_SECTION}.custom_fields": {"field_name": field_name}}},
    )
    await cache.invalidate(voicebot_id)
    return await get_tab6(voicebot_id)