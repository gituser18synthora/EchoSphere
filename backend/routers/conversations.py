"""Conversations: metadata in MySQL, transcripts in MongoDB.

Why MongoDB for transcripts: each session's transcript is a nested list of
turns whose per-turn shape varies (api calls, retrieval chunks, confidences,
prompt versions, latencies) and can grow large. That maps naturally to one
document per session; the relational metadata used for lists/filters/analytics
stays in MySQL as the source of truth.
"""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.transcripts import (
    find_transcript_doc,
    recording_descriptor,
    resolve_recording_path,
    ui_turns,
)
from shared.errors import NotFoundError
from shared.ids import new_id
from shared.models.billing_models import BASE_CURRENCY
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from shared.db.mongo import Mongo
from shared.db.mysql import get_db
from shared.models import ConversationSession, User, VoiceBot
from backend.serializers import serialize_conversation

router = APIRouter(tags=["Conversations"])


def _bot_names(db: Session, bot_ids: list[str]) -> dict[str, str]:
    if not bot_ids:
        return {}
    return dict(
        db.execute(select(VoiceBot.id, VoiceBot.name).where(VoiceBot.id.in_(bot_ids))).all()
    )


@router.get("/conversations")
def list_conversations(
    params: PageParams = Depends(page_params),
    tenant_id: str | None = Query(None, alias="tenantId"),
    bot_id: str | None = Query(None, alias="botId"),
    channel: str | None = Query(None),
    sentiment: str | None = Query(None),
    contained: bool | None = Query(None),
    flagged: bool | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    stmt = select(ConversationSession).where(
        ConversationSession.tenant_id == tid, ConversationSession.is_deleted.is_(False)
    )
    if bot_id:
        stmt = stmt.where(ConversationSession.bot_id == bot_id)
    if channel:
        stmt = stmt.where(ConversationSession.channel == channel)
    if sentiment:
        stmt = stmt.where(ConversationSession.sentiment == sentiment)
    if contained is not None:
        stmt = stmt.where(ConversationSession.contained.is_(contained))
    if flagged is not None:
        stmt = stmt.where(ConversationSession.flagged.is_(flagged))
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(
            or_(
                ConversationSession.caller_masked.like(like),
                ConversationSession.escalation_reason.like(like),
                ConversationSession.id.like(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ConversationSession.started_at.desc())
        .offset(params.offset)
        .limit(params.page_size)
    ).all()
    names = _bot_names(db, [c.bot_id for c in rows])
    return paginated(
        [serialize_conversation(c, bot_name=names.get(c.bot_id, "—")) for c in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    currency: str | None = Query(None, description="Display currency for the cost breakdown"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c = db.get(ConversationSession, conversation_id)
    if c is None or c.is_deleted:
        raise NotFoundError("Conversation")
    assert_tenant_access(user, c.tenant_id)
    doc = await find_transcript_doc(c)
    transcript = ui_turns((doc or {}).get("turns"))
    names = _bot_names(db, [c.bot_id])
    # The session link is what makes the cost auditable. Historical rows
    # predate the column, so fall back to the transcript document, which has
    # carried both ids all along — and persist it so the next read is a join.
    session_id = c.session_id or (doc or {}).get("session_id")
    if session_id and not c.session_id:
        c.session_id = session_id
        db.commit()
    return ok(serialize_conversation(
        c, bot_name=names.get(c.bot_id, "—"), transcript=transcript,
        recording=recording_descriptor(c, doc),
        cost_breakdown=_cost_breakdown(db, c, session_id, currency),
    ))


def _cost_breakdown(
    db: Session, c: ConversationSession, session_id: str | None, currency: str | None
) -> dict:
    """Auditable cost breakdown for one conversation, in the display currency.

    The breakdown is rebuilt from the conversation's usage events and their
    stored pricing snapshots, so it reproduces the historical rate that was
    actually applied rather than today's rate table. ``storedTotalUsd`` is the
    cached total the LIST shows: exposing both makes a drift between them
    visible instead of letting the two pages quietly disagree.
    """
    from shared.billing.conversation_cost import conversation_cost, display_rate

    costing = conversation_cost(db, session_id)
    target = (currency or BASE_CURRENCY).upper()
    rate = display_rate(db, target, as_of=c.started_at)
    payload = costing.as_dict(currency=target, rate=rate)
    stored = Decimal(str(c.cost_usd))
    payload["storedTotalUsd"] = str(stored)
    # Tolerance is one unit of stored precision: anything larger means the
    # cache is stale (events recorded after finalize, or a pricing backfill).
    payload["reconciled"] = abs(stored - costing.total_usd) <= Decimal("0.000001")
    return payload


@router.get("/conversations/{conversation_id}/recording")
async def get_conversation_recording(
    conversation_id: str,
    download: bool = Query(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stream the call audio. Same visibility rules as the conversation itself;
    the file reference always comes from the transcript document — clients
    never pass paths."""
    c = db.get(ConversationSession, conversation_id)
    if c is None or c.is_deleted:
        raise NotFoundError("Conversation")
    assert_tenant_access(user, c.tenant_id)
    doc = await find_transcript_doc(c)
    info = (doc or {}).get("recording") or {}
    full = resolve_recording_path(info.get("path"))
    if full is None:
        raise NotFoundError("Recording")
    kwargs: dict = {"media_type": info.get("mimeType") or "audio/wav"}
    if download:
        kwargs["filename"] = f"echosphere-call-{c.id}{full.suffix or '.wav'}"
    return FileResponse(full, **kwargs)


class TurnPayload(BaseModel):
    turn: int
    speaker: str = Field(pattern="^(user|bot)$")
    text: str = Field(max_length=8000)
    intent: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    chunksUsed: list[str] | None = None
    apiCalls: list[dict] | None = None
    promptVersion: str | None = None
    latencyMs: int | None = None
    costUsd: float | None = None


class CreateConversationRequest(BaseModel):
    bot_id: str = Field(alias="botId")
    channel: str = Field(default="voice", pattern="^(voice|whatsapp|web|mobile)$")
    caller_masked: str = Field(default="•••", alias="caller", max_length=50)
    started_at: datetime | None = Field(default=None, alias="startedAt")
    duration_sec: int = Field(default=0, alias="durationSec", ge=0)
    sentiment: str = Field(default="neutral", pattern="^(positive|neutral|negative)$")
    intents: list[str] = Field(default_factory=list)
    contained: bool = True
    escalation_reason: str | None = Field(default=None, alias="escalationReason")
    csat: int | None = Field(default=None, ge=1, le=5)
    cost_usd: float = Field(default=0, alias="costUsd", ge=0)
    language: str = Field(default="en-US", max_length=15)
    qa_score: int | None = Field(default=None, alias="qaScore", ge=0, le=100)
    transcript: list[TurnPayload] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


@router.post("/conversations", status_code=201)
async def create_conversation(
    body: CreateConversationRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = db.get(VoiceBot, body.bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)

    now = datetime.now(timezone.utc)
    c = ConversationSession(
        id=new_id("cv"),
        tenant_id=bot.tenant_id,
        bot_id=bot.id,
        channel=body.channel,
        caller_masked=body.caller_masked,
        started_at=body.started_at or now,
        duration_sec=body.duration_sec,
        sentiment=body.sentiment,
        intents=body.intents,
        contained=body.contained,
        escalation_reason=body.escalation_reason,
        csat=body.csat,
        cost_usd=body.cost_usd,
        language=body.language,
        qa_score=body.qa_score,
        flagged=False,
    )
    db.add(c)
    db.flush()
    await Mongo.transcripts().update_one(
        {"session_id": c.id},
        {
            "$set": {
                "session_id": c.id,
                "tenant_id": c.tenant_id,
                "bot_id": c.bot_id,
                "user_id": user.id,
                "status": "completed",
                "turns": [t.model_dump(exclude_none=True) for t in body.transcript],
                "metadata": {"channel": c.channel, "language": c.language},
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    db.commit()
    names = _bot_names(db, [c.bot_id])
    return ok(
        serialize_conversation(
            c, bot_name=names.get(c.bot_id, "—"),
            transcript=[t.model_dump(exclude_none=True) for t in body.transcript],
        )
    )


class UpdateConversationRequest(BaseModel):
    flagged: bool | None = None
    qa_score: int | None = Field(default=None, alias="qaScore", ge=0, le=100)

    model_config = {"populate_by_name": True}


@router.patch("/conversations/{conversation_id}")
def update_conversation(
    conversation_id: str,
    body: UpdateConversationRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    c = db.get(ConversationSession, conversation_id)
    if c is None or c.is_deleted:
        raise NotFoundError("Conversation")
    assert_tenant_access(user, c.tenant_id)
    before = {"flagged": c.flagged, "qaScore": c.qa_score}
    if body.flagged is not None:
        c.flagged = body.flagged
    if body.qa_score is not None:
        c.qa_score = body.qa_score
    record_audit(
        db, user=user, action="Updated conversation review", entity_type="conversation",
        entity_id=c.id, target_label=c.id, tenant_id=c.tenant_id,
        previous_value=before, new_value={"flagged": c.flagged, "qaScore": c.qa_score},
        request=request,
    )
    db.commit()
    names = _bot_names(db, [c.bot_id])
    return ok(serialize_conversation(c, bot_name=names.get(c.bot_id, "—")))
