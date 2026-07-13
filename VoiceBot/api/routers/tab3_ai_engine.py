from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab3_ai_engine import Tab3AIEngineRequest, Tab3AIEngineResponse
from voicebot.api.services.tab_services.tab3_service import get_tab3, save_tab3

router = APIRouter(tags=["Tab 3 — AI Engine"])


@router.get("/voicebots/{voicebot_id}/config/ai-engine", response_model=Tab3AIEngineResponse)
async def get_ai_engine_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await get_tab3(voicebot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put("/voicebots/{voicebot_id}/config/ai-engine", response_model=Tab3AIEngineResponse)
async def save_ai_engine_config(
    voicebot_id: str,
    body: Tab3AIEngineRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab3(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e