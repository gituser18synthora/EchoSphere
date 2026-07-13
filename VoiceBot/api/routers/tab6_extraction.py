from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab6_extraction import (
    CustomExtractionField,
    Tab6ExtractionRequest,
    Tab6ExtractionResponse,
)
from voicebot.api.services.tab_services.tab6_service import (
    add_custom_field,
    delete_custom_field,
    get_tab6,
    save_tab6,
)

router = APIRouter(tags=["Tab 6 — Call Data Extraction"])


# ── GET ───────────────────────────────────────────────────────────────────────
@router.get(
    "/voicebots/{voicebot_id}/config/extraction",
    response_model=Tab6ExtractionResponse,
)
async def get_extraction_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await get_tab6(voicebot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── PUT — full save (Save & Continue button) ──────────────────────────────────
@router.put(
    "/voicebots/{voicebot_id}/config/extraction",
    response_model=Tab6ExtractionResponse,
)
async def save_extraction_config(
    voicebot_id: str,
    body: Tab6ExtractionRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab6(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── POST — + Add Custom Field button ─────────────────────────────────────────
@router.post(
    "/voicebots/{voicebot_id}/config/extraction/custom-fields",
    response_model=Tab6ExtractionResponse,
)
async def add_extraction_custom_field(
    voicebot_id: str,
    field: CustomExtractionField,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await add_custom_field(voicebot_id, field)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ── DELETE — remove a custom field row by field_name ─────────────────────────
@router.delete(
    "/voicebots/{voicebot_id}/config/extraction/custom-fields/{field_name}",
    response_model=Tab6ExtractionResponse,
)
async def delete_extraction_custom_field(
    voicebot_id: str,
    field_name: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await delete_custom_field(voicebot_id, field_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e