"""Voice sessions: issue trusted session tokens for the realtime voice worker.

The API process authenticates the user, verifies bot ownership, then writes
the tenant/bot mapping into Redis. The voice worker (a separate process)
accepts the WebSocket using only that mapping — clients can never supply
tenant identity directly.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from shared.config import get_settings
from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user
from shared.errors import ApiError, NotFoundError
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import User, VoiceBot
from shared.voice_sessions import create_voice_session

router = APIRouter(tags=["Voice Sessions"])


class CreateVoiceSessionRequest(BaseModel):
    bot_id: str = Field(alias="botId")
    channel: str = Field(default="browser", pattern="^(browser|phone|sip)$")

    model_config = {"populate_by_name": True}


@router.post("/voice-sessions", status_code=201)
async def create_session(
    body: CreateVoiceSessionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = db.get(VoiceBot, body.bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("Bot not found")
    assert_tenant_access(user, bot.tenant_id)

    session = await create_voice_session(
        tenant_id=bot.tenant_id,
        bot_id=bot.id,
        user_id=user.id,
        channel=body.channel,
    )
    settings = get_settings()
    record_audit(
        db, user=user, action="voice.session.create", entity_type="voice_session",
        entity_id=session["session_id"], target_label=bot.name,
        tenant_id=bot.tenant_id, request=request,
    )
    db.commit()
    return ok(
        {
            "sessionId": session["session_id"],
            "botId": bot.id,
            "channel": body.channel,
            "wsPath": f"/ws/voice/{session['session_id']}",
            "workerPort": settings.voice_worker_port,
            "expiresInSeconds": settings.voice_session_timeout,
        }
    )
