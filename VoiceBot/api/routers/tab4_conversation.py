from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab4_conversation import (
    Tab4ConversationRequest,
    Tab4ConversationResponse,
)
from voicebot.api.services.tab_services.tab4_service import get_tab4, save_tab4

router = APIRouter(tags=["Tab 4 — Conversation Intelligence"])


@router.get(
    "/voicebots/{voicebot_id}/config/conversation",
    response_model=Tab4ConversationResponse,
)
async def get_conversation_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await get_tab4(voicebot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.put(
    "/voicebots/{voicebot_id}/config/conversation",
    response_model=Tab4ConversationResponse,
)
async def save_conversation_config(
    voicebot_id: str,
    body: Tab4ConversationRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab4(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e