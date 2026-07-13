from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab2_persona import Tab2PersonaRequest, Tab2PersonaResponse
from voicebot.api.services.tab_services.tab2_service import get_tab2, save_tab2

router = APIRouter(tags=["Tab 2 — Persona & Behaviour"])


@router.get("/voicebots/{voicebot_id}/config/persona", response_model=Tab2PersonaResponse)
async def get_persona_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await get_tab2(voicebot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/voicebots/{voicebot_id}/config/persona", response_model=Tab2PersonaResponse)
async def save_persona_config(
    voicebot_id: str,
    body: Tab2PersonaRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab2(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e