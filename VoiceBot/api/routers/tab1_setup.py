import uuid

from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab1_setup import Tab1SetupRequest, Tab1SetupResponse
from voicebot.api.services.tab_services.tab1_service import (
    create_tab1,
    get_tab1,
    save_tab1,
)

router = APIRouter(tags=["Tab 1 — Setup"])


# ── CREATE — first time, no voicebot_id yet ───────────────────────────────────
@router.post("/voicebots/config/setup", response_model=Tab1SetupResponse, status_code=201)
async def create_setup_config(body: Tab1SetupRequest):
    voicebot_id = f"vb_{uuid.uuid4().hex[:12]}"
    try:
        return await create_tab1(voicebot_id, body)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ── GET — load existing config into tab ───────────────────────────────────────
@router.get("/voicebots/{voicebot_id}/config/setup", response_model=Tab1SetupResponse)
async def get_setup_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await get_tab1(voicebot_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ── UPDATE — re-save after voicebot exists ────────────────────────────────────
@router.put("/voicebots/{voicebot_id}/config/setup", response_model=Tab1SetupResponse)
async def save_setup_config(
    voicebot_id: str,
    body: Tab1SetupRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab1(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e