from voicebot.api.schemas.tab5_auth import (
    AskAs,
    AuthenticationMode,
    FailureHandling,
    OnFailureAction,
    Tab5AuthRequest,
    Tab5AuthResponse,
    VerificationField,
)
from voicebot.api.services.voicebot_service import apply_voicebot_patch, flatten_for_set
from voicebot.config_layer.cache import ConfigCache
from voicebot.config_layer.db import MongoDB

cache = ConfigCache()

_SECTION = "caller_authentication"


def _enum(cls, raw, default):
    try:
        return cls(raw)
    except (ValueError, KeyError):
        return default


def _parse_doc(voicebot_id: str, doc: dict) -> Tab5AuthResponse:
    ca = doc.get(_SECTION) or {}
    fh = ca.get("failure_handling") or {}

    verification_fields = [
        VerificationField(
            field_name=f.get("field_name", ""),
            verify_against=f.get("verify_against", ""),   # free string, no enum
            ask_as=_enum(AskAs, f.get("ask_as"), AskAs.VOICE_PROMPT),
            required=f.get("required", True),
        )
        for f in (ca.get("verification_fields") or [])
    ]

    data = Tab5AuthRequest(
        enable_authentication=ca.get("enable_authentication", False),
        authentication_mode=_enum(
            AuthenticationMode,
            ca.get("authentication_mode"),
            AuthenticationMode.SILENT,
        ),
        verification_fields=verification_fields,
        failure_handling=FailureHandling(
            max_verification_attempts=fh.get("max_verification_attempts", 2),
            on_failure_action=_enum(
                OnFailureAction,
                fh.get("on_failure_action"),
                OnFailureAction.TRANSFER_TO_HUMAN,
            ),
            # failure_message now lives inside failure_handling
            failure_message=fh.get(
                "failure_message",
                "Sorry, we could not verify your identity. "
                "Please contact support or try again later.",
            ),
        ),
    )
    return Tab5AuthResponse(voicebot_id=voicebot_id, data=data)


async def get_tab5(voicebot_id: str) -> Tab5AuthResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    return _parse_doc(voicebot_id, doc)


async def save_tab5(voicebot_id: str, body: Tab5AuthRequest) -> Tab5AuthResponse:
    set_fields = flatten_for_set(_SECTION, body.model_dump(mode="json"))
    result = await apply_voicebot_patch(voicebot_id, set_fields)
    if not result:
        raise ValueError(f"VoiceBot {voicebot_id} not found")
    await cache.invalidate(voicebot_id)
    return await get_tab5(voicebot_id)


async def add_verification_field(
    voicebot_id: str,
    field: VerificationField,
) -> Tab5AuthResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    existing = doc.get(_SECTION, {}).get("verification_fields", [])
    if any(f.get("field_name") == field.field_name for f in existing):
        raise ValueError(f"Field '{field.field_name}' already exists")

    await MongoDB.voicebot_configs().update_one(
        {"voicebot_id": voicebot_id},
        {"$push": {f"{_SECTION}.verification_fields": field.model_dump(mode="json")}},
    )
    await cache.invalidate(voicebot_id)
    return await get_tab5(voicebot_id)


async def delete_verification_field(
    voicebot_id: str,
    field_name: str,
) -> Tab5AuthResponse:
    doc = await MongoDB.voicebot_configs().find_one({"voicebot_id": voicebot_id})
    if not doc:
        raise ValueError(f"VoiceBot {voicebot_id} not found")

    await MongoDB.voicebot_configs().update_one(
        {"voicebot_id": voicebot_id},
        {"$pull": {f"{_SECTION}.verification_fields": {"field_name": field_name}}},
    )
    await cache.invalidate(voicebot_id)
    return await get_tab5(voicebot_id)