"""Voice sessions: issue trusted session tokens for the realtime voice worker.

The API process authenticates the user, verifies bot ownership, then writes
the tenant/bot mapping into Redis. The voice worker (a separate process)
accepts the WebSocket using only that mapping — clients can never supply
tenant identity directly.
"""

import re

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from shared.config import get_settings
from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user
from shared.errors import ApiError, NotFoundError
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import CustomerCollectionContext, User, VoiceBot
from shared.voice_sessions import create_voice_session

router = APIRouter(tags=["Voice Sessions"])

# Same bounds the signed telephony webhook enforces on dialer variables
# (shared.telephony_webhooks._sanitize_variables) — one contract, two doors.
_VARIABLE_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_MAX_VARIABLES = 20
_MAX_VARIABLE_CHARS = 200


class CreateVoiceSessionRequest(BaseModel):
    bot_id: str = Field(alias="botId")
    channel: str = Field(default="browser", pattern="^(browser|phone|sip)$")
    # Per-call test variables (browser test console) — same shape and bounds
    # as dialer variables so a browser call exercises the live code path.
    variables: dict[str, str] | None = None
    # Pin the call to one customer_contexts row (validated against the bot).
    customer_context_id: str | None = Field(
        default=None, alias="customerContextId", max_length=40
    )

    model_config = {"populate_by_name": True}

    @field_validator("variables")
    @classmethod
    def _bounded_variables(cls, value):
        if value is None:
            return value
        if len(value) > _MAX_VARIABLES:
            raise ValueError(f"at most {_MAX_VARIABLES} variables are allowed")
        cleaned: dict[str, str] = {}
        for key, item in value.items():
            if not _VARIABLE_KEY.match(str(key)):
                raise ValueError(f"invalid variable name: {key!r}")
            cleaned[str(key)] = str(item)[:_MAX_VARIABLE_CHARS]
        return cleaned


@router.post("/voice-sessions", status_code=201)
async def create_session(
    body: CreateVoiceSessionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = db.get(VoiceBot, body.bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("Bot")
    assert_tenant_access(user, bot.tenant_id)

    if body.customer_context_id:
        context = db.get(CustomerCollectionContext, body.customer_context_id)
        if (
            context is None or context.is_deleted
            or context.tenant_id != bot.tenant_id or context.bot_id != bot.id
        ):
            raise NotFoundError("Customer context")

    session = await create_voice_session(
        tenant_id=bot.tenant_id,
        bot_id=bot.id,
        user_id=user.id,
        channel=body.channel,
        variables=body.variables,
        customer_context_id=body.customer_context_id,
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
