"""Workflows: per-bot journey definitions (nodes/edges as JSON documents)."""

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
from backend.core.errors import NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.models import User, VoiceBot, Workflow
from backend.serializers import serialize_workflow

router = APIRouter(tags=["Workflows"])


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _updated_by_name(db: Session, w: Workflow) -> str:
    if w.updated_by:
        u = db.get(User, w.updated_by)
        if u:
            return u.name
    if w.created_by:
        u = db.get(User, w.created_by)
        if u:
            return u.name
    return "—"


@router.get("/bots/{bot_id}/workflow")
def get_bot_workflow(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    w = db.scalar(
        select(Workflow)
        .where(Workflow.bot_id == bot.id, Workflow.is_deleted.is_(False))
        .order_by(Workflow.version.desc())
    )
    if w is None:
        raise NotFoundError("Workflow")
    return ok(serialize_workflow(w, updated_by_name=_updated_by_name(db, w)))


@router.get("/workflows")
def list_workflows(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    rows = db.scalars(
        select(Workflow)
        .where(Workflow.tenant_id == tid, Workflow.is_deleted.is_(False))
        .order_by(Workflow.created_at.asc())
    ).all()
    return ok([serialize_workflow(w, updated_by_name=_updated_by_name(db, w)) for w in rows])


class SaveWorkflowRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    nodes: list[dict] | None = None
    edges: list[dict] | None = None
    issues: list[dict] | None = None
    status: str | None = Field(default=None, pattern="^(draft|pending_approval|approved)$")


@router.put("/bots/{bot_id}/workflow")
def save_bot_workflow(
    bot_id: str,
    body: SaveWorkflowRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    w = db.scalar(
        select(Workflow)
        .where(Workflow.bot_id == bot.id, Workflow.is_deleted.is_(False))
        .order_by(Workflow.version.desc())
    )
    if w is None:
        w = Workflow(
            id=new_id("wf"), tenant_id=bot.tenant_id, bot_id=bot.id,
            name=body.name or f"{bot.name} journey", version=0, status="draft",
            created_by=user.id,
        )
        db.add(w)
    before = {"version": w.version, "status": w.status}
    if body.name:
        w.name = body.name
    if body.nodes is not None:
        w.nodes = body.nodes
    if body.edges is not None:
        w.edges = body.edges
    if body.issues is not None:
        w.issues = body.issues
    if body.status is not None:
        w.status = body.status
    w.version += 1
    w.updated_by = user.id
    record_audit(
        db, user=user, action="Saved workflow", entity_type="workflow", entity_id=w.id,
        target_label=f"{w.name} v{w.version}", tenant_id=bot.tenant_id,
        previous_value=before, new_value={"version": w.version, "status": w.status},
        request=request,
    )
    db.commit()
    return ok(serialize_workflow(w, updated_by_name=user.name))
