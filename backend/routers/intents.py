"""Intents and entity definitions."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.db.mysql import get_db
from backend.models import EntityDef, Intent, User, VoiceBot
from backend.serializers import serialize_entity, serialize_intent

router = APIRouter(tags=["Intents & Entities"])


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


@router.get("/bots/{bot_id}/intents")
def list_intents(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(Intent)
        .where(Intent.bot_id == bot.id, Intent.is_deleted.is_(False))
        .order_by(Intent.created_at.asc())
    ).all()
    return ok([serialize_intent(i) for i in rows])


class IntentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    samples: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.7, alias="confidenceThreshold", ge=0, le=1)
    route: str = Field(default="", max_length=200)
    entities: list[str] = Field(default_factory=list)
    status: str = Field(default="active", pattern="^(active|needs_samples|disabled)$")

    model_config = {"populate_by_name": True}


@router.post("/bots/{bot_id}/intents", status_code=201)
def create_intent(
    bot_id: str,
    body: IntentRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    exists = db.scalar(
        select(Intent).where(
            Intent.bot_id == bot.id, Intent.name == body.name, Intent.is_deleted.is_(False)
        )
    )
    if exists:
        raise ApiError("An intent with this name already exists on this bot.", 409)
    row = Intent(
        id=new_id("in"), tenant_id=bot.tenant_id, bot_id=bot.id, name=body.name,
        description=body.description, samples=body.samples,
        confidence_threshold=body.confidence_threshold, route=body.route,
        entities=body.entities,
        status="needs_samples" if len(body.samples) < 3 else body.status,
        version=1, created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created intent", entity_type="intent", entity_id=row.id,
        target_label=f"{row.name} ({bot.name})", tenant_id=bot.tenant_id,
        new_value={"name": row.name}, request=request,
    )
    db.commit()
    return ok(serialize_intent(row))


class UpdateIntentRequest(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    samples: list[str] | None = None
    confidence_threshold: float | None = Field(
        default=None, alias="confidenceThreshold", ge=0, le=1
    )
    route: str | None = Field(default=None, max_length=200)
    entities: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|needs_samples|disabled)$")

    model_config = {"populate_by_name": True}


@router.patch("/intents/{intent_id}")
def update_intent(
    intent_id: str,
    body: UpdateIntentRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(Intent, intent_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Intent")
    assert_tenant_access(user, row.tenant_id)
    before = {"samples": len(row.samples or []), "status": row.status}
    changed = False
    for field in ("description", "samples", "confidence_threshold", "route", "entities", "status"):
        val = getattr(body, field)
        if val is not None:
            setattr(row, field, val)
            changed = True
    if changed:
        row.version += 1
        row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated intent", entity_type="intent", entity_id=row.id,
        target_label=row.name, tenant_id=row.tenant_id, previous_value=before,
        new_value={"samples": len(row.samples or []), "status": row.status},
        request=request,
    )
    db.commit()
    return ok(serialize_intent(row))


@router.delete("/intents/{intent_id}")
def delete_intent(
    intent_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(Intent, intent_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Intent")
    assert_tenant_access(user, row.tenant_id)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="Archived intent", entity_type="intent", entity_id=row.id,
        target_label=row.name, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})


# ── Entities ─────────────────────────────────────────────────────────────────


@router.get("/entities")
def list_entities(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    rows = db.scalars(
        select(EntityDef)
        .where(EntityDef.tenant_id == tid, EntityDef.is_deleted.is_(False))
        .order_by(EntityDef.created_at.asc())
    ).all()
    return ok([serialize_entity(e) for e in rows])


class EntityRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field(default="custom", pattern="^(system|custom|regex)$")
    example: str = Field(default="", max_length=300)
    pii: bool = False
    tenant_id: str | None = Field(default=None, alias="tenantId")

    model_config = {"populate_by_name": True}


@router.post("/entities", status_code=201)
def create_entity(
    body: EntityRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, body.tenant_id)
    exists = db.scalar(
        select(EntityDef).where(
            EntityDef.tenant_id == tid, EntityDef.name == body.name,
            EntityDef.is_deleted.is_(False),
        )
    )
    if exists:
        raise ApiError("An entity with this name already exists.", 409)
    row = EntityDef(
        id=new_id("en"), tenant_id=tid, name=body.name, kind=body.kind,
        example=body.example, pii=body.pii, used_by=[], created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created entity", entity_type="entity", entity_id=row.id,
        target_label=row.name, tenant_id=tid, new_value={"name": row.name, "pii": row.pii},
        request=request,
    )
    db.commit()
    return ok(serialize_entity(row))
