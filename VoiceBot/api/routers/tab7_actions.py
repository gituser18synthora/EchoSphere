from fastapi import APIRouter, Depends, HTTPException

from voicebot.api.dependencies import get_voicebot_or_404
from voicebot.api.schemas.tab7_actions import (
    ReorderBody,
    Tab7ActionsRequest,
    Tab7ActionsResponse,
    ToolConfig,
    ToolConfigResponse,
)
from voicebot.api.services.tab_services.tab7_service import (
    configure_tool,
    get_tab7,
    reorder_end_of_call,
    reorder_start_of_call,
    save_tab7,
)

router = APIRouter(prefix="/voicebots/{voicebot_id}/config", tags=["Tab 7 — Actions"])


@router.get("/actions", response_model=Tab7ActionsResponse)
async def get_actions_config(
    voicebot_id: str,
    _doc: dict = Depends(get_voicebot_or_404),
):
    return await get_tab7(voicebot_id)


@router.put("/actions", response_model=Tab7ActionsResponse)
async def save_actions_config(
    voicebot_id: str,
    body: Tab7ActionsRequest,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await save_tab7(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.put("/actions/tools/{tool_key}/config", response_model=ToolConfigResponse)
async def configure_tool_endpoint(
    voicebot_id: str,
    tool_key: str,
    body: ToolConfig,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await configure_tool(voicebot_id, tool_key, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/actions/start-of-call/reorder", response_model=Tab7ActionsResponse)
async def reorder_start_of_call_endpoint(
    voicebot_id: str,
    body: ReorderBody,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await reorder_start_of_call(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.patch("/actions/end-of-call/reorder", response_model=Tab7ActionsResponse)
async def reorder_end_of_call_endpoint(
    voicebot_id: str,
    body: ReorderBody,
    _doc: dict = Depends(get_voicebot_or_404),
):
    try:
        return await reorder_end_of_call(voicebot_id, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
