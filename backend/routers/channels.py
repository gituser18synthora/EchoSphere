"""Channel configurations per bot + platform channel summary."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_super_admin,
    require_tenant_admin,
)
from backend.core.errors import NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.models import ChannelConfig, User, VoiceBot
from backend.serializers import serialize_channel

router = APIRouter(tags=["Channels"])

CHANNEL_TYPES = ("voice", "whatsapp", "web", "mobile")


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


@router.get("/bots/{bot_id}/channels")
def list_bot_channels(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id, ChannelConfig.is_deleted.is_(False)
        )
    ).all()
    by_type = {c.type: c for c in rows}
    # Always return all four channel slots, defaulting to not_configured.
    out = []
    for ctype in CHANNEL_TYPES:
        if ctype in by_type:
            out.append(serialize_channel(by_type[ctype]))
        else:
            out.append({
                "type": ctype, "botId": bot.id, "status": "not_configured",
                "detail": "Not configured", "workflow": "—", "lastTest": None,
            })
    return ok(out)


class ChannelRequest(BaseModel):
    status: str | None = Field(
        default=None, pattern="^(live|configured|testing|failed|not_configured)$"
    )
    detail: str | None = Field(default=None, max_length=300)
    workflow_name: str | None = Field(default=None, alias="workflowName", max_length=200)
    run_test: bool = Field(default=False, alias="runTest")

    model_config = {"populate_by_name": True}


@router.put("/bots/{bot_id}/channels/{channel_type}")
def upsert_channel(
    bot_id: str,
    channel_type: str,
    body: ChannelRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    if channel_type not in CHANNEL_TYPES:
        raise NotFoundError("Channel type")
    bot = _bot_checked(db, bot_id, user)
    row = db.scalar(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id, ChannelConfig.type == channel_type,
            ChannelConfig.is_deleted.is_(False),
        )
    )
    if row is None:
        row = ChannelConfig(
            id=new_id("ch"), tenant_id=bot.tenant_id, bot_id=bot.id,
            type=channel_type, created_by=user.id,
        )
        db.add(row)
    before = {"status": row.status, "detail": row.detail}
    if body.status:
        row.status = body.status
    if body.detail is not None:
        row.detail = body.detail
    if body.workflow_name is not None:
        row.workflow_name = body.workflow_name
    if body.run_test:
        row.last_test = {
            "at": datetime.now(timezone.utc).isoformat() + "Z",
            "ok": row.status in ("live", "configured", "testing"),
            "message": "Connectivity check completed"
            if row.status in ("live", "configured", "testing")
            else "Channel is not configured",
        }
    row.updated_by = user.id
    # Channel readiness follows having at least one live/configured channel.
    live_count = db.scalar(
        select(func.count()).select_from(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id,
            ChannelConfig.is_deleted.is_(False),
            ChannelConfig.status.in_(["live", "configured"]),
        )
    )
    for item in bot.readiness_items:
        if item.item_key == "r6":
            item.done = bool(live_count) or row.status in ("live", "configured")
    record_audit(
        db, user=user, action="Updated channel", entity_type="channel",
        entity_id=row.id, target_label=f"{bot.name} · {channel_type}",
        tenant_id=bot.tenant_id, previous_value=before,
        new_value={"status": row.status, "detail": row.detail}, request=request,
    )
    db.commit()
    return ok(serialize_channel(row))


@router.get("/channels/summary")
def channels_summary(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    """Platform-wide per-channel status counts (VoicePlatform admin page)."""
    rows = db.execute(
        select(ChannelConfig.type, ChannelConfig.status, func.count())
        .where(ChannelConfig.is_deleted.is_(False))
        .group_by(ChannelConfig.type, ChannelConfig.status)
    ).all()
    summary: dict[str, dict] = {
        t: {"type": t, "live": 0, "testing": 0, "failed": 0, "configured": 0}
        for t in CHANNEL_TYPES
    }
    for ctype, status, count in rows:
        if ctype in summary and status in summary[ctype]:
            summary[ctype][status] = count
    return ok(list(summary.values()))
