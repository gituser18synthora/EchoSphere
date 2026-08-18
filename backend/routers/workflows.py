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
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import User, VoiceBot, Workflow
from shared.readiness import refresh_readiness
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


# The node kinds the runtime interpreter executes
# (shared/orchestration/workflow_engine.py::build_definition_graph).
NODE_KINDS = (
    "start", "message", "ask", "intent", "condition",
    "api", "knowledge", "handover", "end",
)
_TERMINAL_KINDS = ("end", "handover")


def validate_definition(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Structural validation of a workflow document.

    Returns (hard_errors, issues): hard errors reject the save (the document
    could not execute or is corrupt); issues are stored on the workflow and
    surfaced in the builder (drafts may be saved incomplete).
    """
    errors: list[dict] = []
    issues: list[dict] = []

    ids = [str(n.get("id") or "") for n in nodes]
    if any(not i for i in ids):
        errors.append({"field": "nodes", "message": "Every node needs an id."})
    duplicates = sorted({i for i in ids if i and ids.count(i) > 1})
    if duplicates:
        errors.append({"field": "nodes",
                       "message": f"Duplicate node ids: {', '.join(duplicates)}."})
    for n in nodes:
        if n.get("kind") not in NODE_KINDS:
            errors.append({
                "field": "nodes",
                "message": f"Unknown node kind '{n.get('kind')}' on node "
                           f"'{n.get('id')}'. Allowed: {', '.join(NODE_KINDS)}.",
            })
    starts = [n for n in nodes if n.get("kind") == "start"]
    if nodes and not starts:
        errors.append({"field": "nodes", "message": "A start node is required."})
    if len(starts) > 1:
        errors.append({"field": "nodes", "message": "Only one start node is allowed."})
    node_ids = set(ids)
    for e in edges:
        if str(e.get("from") or "") not in node_ids or str(e.get("to") or "") not in node_ids:
            errors.append({
                "field": "edges",
                "message": f"Connection '{e.get('id') or '?'}' references a node "
                           "that doesn't exist.",
            })
    if errors:
        return errors, issues

    adjacency: dict[str, list[str]] = {}
    for e in edges:
        adjacency.setdefault(str(e["from"]), []).append(str(e["to"]))

    start_id = str(starts[0]["id"]) if starts else None
    reachable: set[str] = set()
    stack = [start_id] if start_id else []
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(adjacency.get(current, []))

    for n in nodes:
        nid, kind = str(n["id"]), n.get("kind")
        config = n.get("config") or {}
        out = adjacency.get(nid, [])
        if nid not in reachable:
            issues.append({"nodeId": nid, "level": "warning",
                           "message": "Not connected to the start node — this step never runs."})
            continue
        if kind == "condition":
            if not config.get("variable"):
                issues.append({"nodeId": nid, "level": "error",
                               "message": "Condition needs a variable to evaluate."})
            if len(out) < 2:
                issues.append({"nodeId": nid, "level": "warning",
                               "message": "Condition should have true and false branches."})
        if kind == "ask" and not config.get("variable"):
            issues.append({"nodeId": nid, "level": "warning",
                           "message": "No variable name set — the node id will be used."})
        if kind == "intent" and not out:
            issues.append({"nodeId": nid, "level": "error",
                           "message": "Intent node needs at least one outgoing branch."})
        if kind not in _TERMINAL_KINDS and kind != "start" and not out:
            issues.append({"nodeId": nid, "level": "warning",
                           "message": "Dead end — no outgoing connection."})
    if start_id and nodes and not any(
        n.get("kind") in _TERMINAL_KINDS and str(n["id"]) in reachable for n in nodes
    ):
        issues.append({"nodeId": start_id, "level": "warning",
                       "message": "No end or handover step is reachable — the flow cannot finish."})
    return errors, issues


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
    if body.nodes is not None or body.edges is not None:
        nodes = body.nodes if body.nodes is not None else (w.nodes or [])
        edges = body.edges if body.edges is not None else (w.edges or [])
        errors, computed_issues = validate_definition(nodes, edges)
        if errors:
            raise ApiError("Workflow validation failed.", 422, errors=errors)
        w.nodes = nodes
        w.edges = edges
        # Issues are server-computed and authoritative — client-supplied
        # issues are ignored so stale warnings can't be persisted.
        w.issues = computed_issues
    if body.status is not None:
        w.status = body.status
    w.version += 1
    w.updated_by = user.id
    refresh_readiness(db, bot, keys=("r5",))
    record_audit(
        db, user=user, action="Saved workflow", entity_type="workflow", entity_id=w.id,
        target_label=f"{w.name} v{w.version}", tenant_id=bot.tenant_id,
        previous_value=before, new_value={"version": w.version, "status": w.status},
        request=request,
    )
    db.commit()
    return ok(serialize_workflow(w, updated_by_name=user.name))
