"""Bot API connections + connection test console."""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
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
from backend.models import ApiConnection, User, VoiceBot
from backend.serializers import serialize_api_connection

router = APIRouter(tags=["API Connections"])


@router.get("/api-connections")
def list_api_connections(
    bot_id: str | None = Query(None, alias="botId"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    stmt = select(ApiConnection).where(
        ApiConnection.tenant_id == tid, ApiConnection.is_deleted.is_(False)
    )
    if bot_id:
        stmt = stmt.where(ApiConnection.bot_id == bot_id)
    rows = db.scalars(stmt.order_by(ApiConnection.created_at.asc())).all()
    return ok([serialize_api_connection(a) for a in rows])


class ApiConnectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    method: str = Field(default="GET", pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    url: str = Field(min_length=8, max_length=500)
    auth_type: str = Field(default="none", alias="authType", pattern="^(none|api_key|oauth2|bearer)$")
    secret_ref: str | None = Field(default=None, alias="secretRef", max_length=300)
    timeout_ms: int = Field(default=4000, alias="timeoutMs", ge=100, le=60000)
    retries: int = Field(default=1, ge=0, le=5)
    response_mapping: list[dict] = Field(default_factory=list, alias="responseMapping")
    bot_id: str | None = Field(default=None, alias="botId")

    model_config = {"populate_by_name": True}


@router.post("/api-connections", status_code=201)
def create_api_connection(
    body: ApiConnectionRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    if body.secret_ref and not body.secret_ref.startswith("secret://"):
        raise ApiError("secretRef must be a masked secret:// reference, never a raw secret.", 422)
    tid = None
    if body.bot_id:
        bot = db.get(VoiceBot, body.bot_id)
        if bot is None or bot.is_deleted:
            raise NotFoundError("VoiceBot")
        assert_tenant_access(user, bot.tenant_id)
        tid = bot.tenant_id
    else:
        tid = resolve_tenant_id(user, None)
    row = ApiConnection(
        id=new_id("api"), tenant_id=tid, bot_id=body.bot_id, name=body.name,
        method=body.method, url=body.url, auth_type=body.auth_type,
        secret_ref=body.secret_ref, timeout_ms=body.timeout_ms, retries=body.retries,
        response_mapping=body.response_mapping, status="untested", version=1,
        created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created API connection", entity_type="api_connection",
        entity_id=row.id, target_label=row.name, tenant_id=tid,
        new_value={"name": row.name, "method": row.method, "url": row.url},
        request=request,
    )
    db.commit()
    return ok(serialize_api_connection(row))


@router.post("/api-connections/{conn_id}/test")
def test_api_connection(
    conn_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.get(ApiConnection, conn_id)
    if row is None or row.is_deleted:
        raise NotFoundError("API connection")
    assert_tenant_access(user, row.tenant_id)

    # Real outbound test with the configured timeout. Never echoes secrets.
    import httpx

    started = time.monotonic()
    status_code = 0
    body_preview = ""
    okay = False
    try:
        with httpx.Client(timeout=row.timeout_ms / 1000, follow_redirects=True) as client:
            resp = client.request(row.method, row.url)
            status_code = resp.status_code
            body_preview = resp.text[:500]
            okay = 200 <= resp.status_code < 400
    except httpx.TimeoutException:
        status_code = 504
        body_preview = f'{{"error":"upstream timeout after {row.timeout_ms}ms"}}'
    except httpx.HTTPError as e:
        status_code = 502
        body_preview = f'{{"error":"connection failed: {type(e).__name__}"}}'
    latency_ms = round((time.monotonic() - started) * 1000)

    row.last_tested_at = datetime.now(timezone.utc)
    row.last_latency_ms = latency_ms
    row.status = "healthy" if okay else ("degraded" if latency_ms > row.timeout_ms * 0.6 and okay else "failing")
    record_audit(
        db, user=user, action="Tested API connection", entity_type="api_connection",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id,
        new_value={"ok": okay, "status": status_code, "latencyMs": latency_ms},
        request=request,
    )
    db.commit()
    return ok({"ok": okay, "latencyMs": latency_ms, "status": status_code, "body": body_preview})


class UpdateApiConnectionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    method: str | None = Field(default=None, pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    url: str | None = Field(default=None, max_length=500)
    auth_type: str | None = Field(default=None, alias="authType", pattern="^(none|api_key|oauth2|bearer)$")
    secret_ref: str | None = Field(default=None, alias="secretRef", max_length=300)
    timeout_ms: int | None = Field(default=None, alias="timeoutMs", ge=100, le=60000)
    retries: int | None = Field(default=None, ge=0, le=5)
    response_mapping: list[dict] | None = Field(default=None, alias="responseMapping")

    model_config = {"populate_by_name": True}


@router.patch("/api-connections/{conn_id}")
def update_api_connection(
    conn_id: str,
    body: UpdateApiConnectionRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(ApiConnection, conn_id)
    if row is None or row.is_deleted:
        raise NotFoundError("API connection")
    assert_tenant_access(user, row.tenant_id)
    if body.secret_ref and not body.secret_ref.startswith("secret://"):
        raise ApiError("secretRef must be a masked secret:// reference, never a raw secret.", 422)
    before = {"url": row.url, "method": row.method, "version": row.version}
    changed = False
    for field in ("name", "method", "url", "auth_type", "secret_ref", "timeout_ms", "retries", "response_mapping"):
        val = getattr(body, field)
        if val is not None:
            setattr(row, field, val)
            changed = True
    if changed:
        row.version += 1
        row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated API connection", entity_type="api_connection",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id,
        previous_value=before, new_value={"url": row.url, "version": row.version},
        request=request,
    )
    db.commit()
    return ok(serialize_api_connection(row))


@router.delete("/api-connections/{conn_id}")
def delete_api_connection(
    conn_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(ApiConnection, conn_id)
    if row is None or row.is_deleted:
        raise NotFoundError("API connection")
    assert_tenant_access(user, row.tenant_id)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="Archived API connection", entity_type="api_connection",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})
