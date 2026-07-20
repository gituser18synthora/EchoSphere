"""Knowledge sources and knowledge gaps."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    is_super_admin,
    require_permission,
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.db.mysql import get_db
from backend.models import KnowledgeGap, KnowledgeSource, User, VoiceBot
from backend.serializers import serialize_knowledge, serialize_knowledge_gap

router = APIRouter(tags=["Knowledge"])


@router.get("/knowledge")
def list_knowledge(
    bot_id: str | None = Query(None, alias="botId"),
    scope: str | None = Query(None, pattern="^(bot|tenant|global)$"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    params: PageParams = Depends(page_params),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(KnowledgeSource).where(KnowledgeSource.is_deleted.is_(False))
    if is_super_admin(user) and tenant_id is None and bot_id is None:
        pass  # platform view: all sources
    else:
        tid = resolve_tenant_id(user, tenant_id)
        stmt = stmt.where(
            or_(KnowledgeSource.tenant_id == tid, KnowledgeSource.scope == "global")
        )
    if bot_id:
        # A bot view shows its own sources plus tenant/global shared ones.
        stmt = stmt.where(
            or_(KnowledgeSource.bot_id == bot_id, KnowledgeSource.scope != "bot")
        )
    if scope:
        stmt = stmt.where(KnowledgeSource.scope == scope)
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(or_(KnowledgeSource.name.like(like), KnowledgeSource.detail.like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(KnowledgeSource.created_at.asc()).offset(params.offset).limit(params.page_size)
    ).all()
    return paginated(
        [serialize_knowledge(k) for k in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


class CreateKnowledgeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    type: str = Field(pattern="^(document|url|faq|connector)$")
    detail: str = Field(default="", max_length=500)
    scope: str = Field(default="bot", pattern="^(bot|tenant|global)$")
    bot_id: str | None = Field(default=None, alias="botId")
    tenant_id: str | None = Field(default=None, alias="tenantId")
    size_kb: int = Field(default=0, alias="sizeKb", ge=0)

    model_config = {"populate_by_name": True}


@router.post("/knowledge", status_code=201)
def create_knowledge(
    body: CreateKnowledgeRequest,
    request: Request,
    user: User = Depends(require_permission("manage_knowledge", "knowledge.manage")),
    db: Session = Depends(get_db),
):
    if body.scope == "global":
        if not is_super_admin(user):
            raise ApiError("Only platform administrators can create global sources.", 403)
        tid = None
    else:
        tid = resolve_tenant_id(user, body.tenant_id)

    bot = None
    if body.scope == "bot":
        if not body.bot_id:
            raise ApiError("botId is required for bot-scoped sources.", 422)
        bot = db.get(VoiceBot, body.bot_id)
        if bot is None or bot.is_deleted:
            raise NotFoundError("VoiceBot")
        assert_tenant_access(user, bot.tenant_id)
        tid = bot.tenant_id

    # KB name: trimmed, non-empty, unique within the tenant (case-insensitive).
    name = body.name.strip()
    if not name:
        raise ApiError("Knowledge base name is required.", 422,
                       errors=[{"field": "name", "message": "Name is required."}])
    duplicate = db.scalar(
        select(KnowledgeSource).where(
            KnowledgeSource.tenant_id == tid,
            func.lower(KnowledgeSource.name) == name.lower(),
            KnowledgeSource.is_deleted.is_(False),
        )
    )
    if duplicate is not None:
        raise ApiError(
            f"A knowledge base named '{name}' already exists in this workspace. "
            "Choose a different name.",
            409, errors=[{"field": "name", "message": "Duplicate name."}],
        )
    body.name = name

    row = KnowledgeSource(
        id=new_id("ks"),
        tenant_id=tid,
        bot_id=bot.id if bot else None,
        scope=body.scope,
        type=body.type,
        name=body.name,
        detail=body.detail,
        # "pending" until the first document upload flips it to "indexing" —
        # an empty KB is not "indexing" anything.
        status="pending",
        size_kb=body.size_kb,
        created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Added knowledge source", entity_type="knowledge_source",
        entity_id=row.id, target_label=row.name, tenant_id=tid,
        new_value={"name": row.name, "type": row.type, "scope": row.scope}, request=request,
    )
    db.commit()
    return ok(serialize_knowledge(row))


class UpdateKnowledgeRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    detail: str | None = Field(default=None, max_length=500)
    status: str | None = Field(default=None, pattern="^(indexed|indexing|failed|pending|stale)$")
    resync: bool = False


def _get_source_checked(db: Session, source_id: str, user: User) -> KnowledgeSource:
    row = db.get(KnowledgeSource, source_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Knowledge source")
    if row.scope == "global":
        if not is_super_admin(user):
            raise ApiError("Global sources are managed by platform administrators.", 403)
    else:
        assert_tenant_access(user, row.tenant_id)
    return row


@router.patch("/knowledge/{source_id}")
def update_knowledge(
    source_id: str,
    body: UpdateKnowledgeRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _get_source_checked(db, source_id, user)
    before = {"name": row.name, "status": row.status}
    if body.name:
        row.name = body.name
    if body.detail is not None:
        row.detail = body.detail
    if body.resync:
        row.status = "indexing"
        row.last_sync_at = datetime.now(timezone.utc)
    elif body.status:
        row.status = body.status
        if body.status == "indexed":
            row.last_sync_at = datetime.now(timezone.utc)
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Re-synced knowledge source" if body.resync else "Updated knowledge source",
        entity_type="knowledge_source", entity_id=row.id, target_label=row.name,
        tenant_id=row.tenant_id, previous_value=before,
        new_value={"name": row.name, "status": row.status}, request=request,
    )
    db.commit()
    return ok(serialize_knowledge(row))


@router.delete("/knowledge/{source_id}")
def delete_knowledge(
    source_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _get_source_checked(db, source_id, user)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="Archived knowledge source", entity_type="knowledge_source",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})


@router.get("/knowledge-gaps")
def list_knowledge_gaps(
    tenant_id: str | None = Query(None, alias="tenantId"),
    bot_id: str | None = Query(None, alias="botId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    stmt = select(KnowledgeGap).where(
        KnowledgeGap.tenant_id == tid, KnowledgeGap.is_deleted.is_(False)
    )
    if bot_id:
        stmt = stmt.where(KnowledgeGap.bot_id == bot_id)
    rows = db.scalars(stmt.order_by(KnowledgeGap.frequency.desc()).limit(50)).all()
    return ok([serialize_knowledge_gap(g) for g in rows])
