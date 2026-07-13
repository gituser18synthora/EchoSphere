from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab5_auth import (
    Tab5AuthRequest,
    Tab5AuthResponse,
    VerificationField,
)
from voicebot.api.services.tab_services.tab5_service import (
    add_verification_field,
    delete_verification_field,
    get_tab5,
    save_tab5,
)

router = APIRouter(tags=["Tab 5 — Caller Authentication"])


# ── GET ───────────────────────────────────────────────────────────────────────
@router.get(
    "/voicebots/{voicebot_id}/config/authentication",
    response_model=Tab5AuthResponse,
)
async def get_auth_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await get_tab5(voicebot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── PUT — full save (Save & Continue button) ──────────────────────────────────
@router.put(
    "/voicebots/{voicebot_id}/config/authentication",
    response_model=Tab5AuthResponse,
)
async def save_auth_config(
    voicebot_id: str,
    body: Tab5AuthRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab5(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── POST — Add Field button ───────────────────────────────────────────────────
@router.post(
    "/voicebots/{voicebot_id}/config/authentication/fields",
    response_model=Tab5AuthResponse,
)
async def add_auth_field(
    voicebot_id: str,
    field: VerificationField,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await add_verification_field(voicebot_id, field)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── DELETE — remove a single field row by field_name ─────────────────────────
@router.delete(
    "/voicebots/{voicebot_id}/config/authentication/fields/{field_name}",
    response_model=Tab5AuthResponse,
)
async def delete_auth_field(
    voicebot_id: str,
    field_name: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await delete_verification_field(voicebot_id, field_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e