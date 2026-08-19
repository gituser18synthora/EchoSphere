"""Bot API connections: request builder, variable mapping, associations and an
SSRF-guarded connection test console.

Raw secrets never live in these tables or responses — only `secret://` refs,
resolved server-side at call time from the environment.
"""

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.config import get_settings
from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_permission,
    resolve_tenant_id,
)
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from backend.core.safe_http import safe_request
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.db.mysql import get_db
from shared.models import ApiConnection, Intent, User, VoiceBot, Workflow
from backend.serializers import serialize_api_connection

router = APIRouter(tags=["API Connections"])

# Variables the request builder may reference. `entities.<name>` is dynamic.
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")
_ALLOWED_VARIABLES = {
    "tenant_id", "bot_id", "call_id", "session_id", "user_id",
    "customer_phone", "intent.code", "intent.name",
}


def _collect_variables(*values) -> set[str]:
    found: set[str] = set()

    def _walk(value):
        if isinstance(value, str):
            found.update(m.group(1) for m in _VARIABLE_PATTERN.finditer(value))
        elif isinstance(value, dict):
            for k, v in value.items():
                _walk(k)
                _walk(v)
        elif isinstance(value, list):
            for v in value:
                _walk(v)

    for value in values:
        _walk(value)
    return found


def _validate_variables(*values) -> None:
    unknown = {
        v for v in _collect_variables(*values)
        if v not in _ALLOWED_VARIABLES and not v.startswith("entities.")
    }
    if unknown:
        raise ApiError(
            f"Unknown template variables: {', '.join(sorted('{{' + v + '}}' for v in unknown))}. "
            "Allowed: " + ", ".join(sorted("{{" + v + "}}" for v in _ALLOWED_VARIABLES))
            + ", {{entities.<name>}}.",
            422, errors=[{"field": "variables", "message": "Unknown variable."}],
        )


def _substitute(value, mapping: dict[str, str]):
    if isinstance(value, str):
        return _VARIABLE_PATTERN.sub(lambda m: str(mapping.get(m.group(1), "")), value)
    if isinstance(value, dict):
        return {_substitute(k, mapping): _substitute(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, mapping) for v in value]
    return value


def _conn_checked(db: Session, conn_id: str, user: User) -> ApiConnection:
    row = db.get(ApiConnection, conn_id)
    if row is None or row.is_deleted:
        raise NotFoundError("API connection")
    assert_tenant_access(user, row.tenant_id)
    return row


def _validate_associations(db: Session, tenant_id: str, *,
                           allowed_intents: list[str] | None,
                           allowed_workflows: list[str] | None) -> None:
    for intent_id in allowed_intents or []:
        intent = db.get(Intent, intent_id)
        if intent is None or intent.is_deleted or intent.tenant_id != tenant_id:
            raise ApiError("A referenced intent does not exist in this workspace.", 422)
    for wf_id in allowed_workflows or []:
        wf = db.get(Workflow, wf_id)
        if wf is None or wf.is_deleted or wf.tenant_id != tenant_id:
            raise ApiError("A referenced workflow does not exist in this workspace.", 422)


@router.get("/api-connections")
def list_api_connections(
    bot_id: str | None = Query(None, alias="botId"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(require_permission(
        "manage_api_connections", "test_api_connections", "integrations.manage")),
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
    description: str = Field(default="", max_length=500)
    method: str = Field(default="GET", pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    url: str = Field(min_length=8, max_length=500)
    auth_type: str = Field(default="none", alias="authType",
                           pattern="^(none|api_key|oauth2|bearer|basic)$")
    secret_ref: str | None = Field(default=None, alias="secretRef", max_length=300)
    headers: dict[str, str] = Field(default_factory=dict)
    query_params: dict[str, str] = Field(default_factory=dict, alias="queryParams")
    path_params: dict[str, str] = Field(default_factory=dict, alias="pathParams")
    body_template: dict | None = Field(default=None, alias="bodyTemplate")
    request_schema: dict | None = Field(default=None, alias="requestSchema")
    response_schema: dict | None = Field(default=None, alias="responseSchema")
    success_condition: str | None = Field(default=None, alias="successCondition", max_length=200)
    success_message: str | None = Field(default=None, alias="successMessage", max_length=500)
    failure_message: str | None = Field(default=None, alias="failureMessage", max_length=500)
    error_mapping: dict = Field(default_factory=dict, alias="errorMapping")
    sensitive_masks: list[str] = Field(default_factory=list, alias="sensitiveMasks")
    allowed_intents: list[str] = Field(default_factory=list, alias="allowedIntents")
    allowed_workflows: list[str] = Field(default_factory=list, alias="allowedWorkflows")
    is_state_changing: bool = Field(default=False, alias="isStateChanging")
    require_confirmation: bool = Field(default=False, alias="requireConfirmation")
    timeout_ms: int = Field(default=4000, alias="timeoutMs", ge=100, le=60000)
    retries: int = Field(default=1, ge=0, le=5)
    response_mapping: list[dict] = Field(default_factory=list, alias="responseMapping")
    bot_id: str | None = Field(default=None, alias="botId")

    model_config = {"populate_by_name": True}


def _check_secret_ref(secret_ref: str | None) -> None:
    if secret_ref and not secret_ref.startswith("secret://"):
        raise ApiError("secretRef must be a masked secret:// reference, never a raw secret.", 422)


@router.post("/api-connections", status_code=201)
def create_api_connection(
    body: ApiConnectionRequest,
    request: Request,
    user: User = Depends(require_permission("manage_api_connections", "integrations.manage")),
    db: Session = Depends(get_db),
):
    _check_secret_ref(body.secret_ref)
    _validate_variables(body.url, body.headers, body.query_params, body.path_params,
                        body.body_template)
    tid = None
    if body.bot_id:
        bot = db.get(VoiceBot, body.bot_id)
        if bot is None or bot.is_deleted:
            raise NotFoundError("VoiceBot")
        assert_tenant_access(user, bot.tenant_id)
        tid = bot.tenant_id
    else:
        tid = resolve_tenant_id(user, None)
    _validate_associations(db, tid, allowed_intents=body.allowed_intents,
                           allowed_workflows=body.allowed_workflows)
    row = ApiConnection(
        id=new_id("api"), tenant_id=tid, bot_id=body.bot_id, name=body.name.strip(),
        description=body.description, method=body.method, url=body.url,
        auth_type=body.auth_type, secret_ref=body.secret_ref, headers=body.headers,
        query_params=body.query_params, path_params=body.path_params,
        body_template=body.body_template, request_schema=body.request_schema,
        response_schema=body.response_schema, success_condition=body.success_condition,
        success_message=body.success_message, failure_message=body.failure_message,
        error_mapping=body.error_mapping, sensitive_masks=body.sensitive_masks,
        allowed_intents=body.allowed_intents, allowed_workflows=body.allowed_workflows,
        is_state_changing=body.is_state_changing,
        require_confirmation=body.require_confirmation,
        timeout_ms=body.timeout_ms, retries=body.retries,
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


class TestConnectionRequest(BaseModel):
    # Sample values for {{variables}} during the test run.
    test_values: dict[str, str] = Field(default_factory=dict, alias="testValues")

    model_config = {"populate_by_name": True}


@router.post("/api-connections/{conn_id}/test")
def test_api_connection(
    conn_id: str,
    request: Request,
    body: TestConnectionRequest | None = None,
    user: User = Depends(require_permission("test_api_connections", "manage_api_connections",
                                            "integrations.manage")),
    db: Session = Depends(get_db),
):
    row = _conn_checked(db, conn_id, user)
    settings = get_settings()

    mapping = {"tenant_id": row.tenant_id, "bot_id": row.bot_id or "",
               "call_id": "test-call", "session_id": "test-session",
               "user_id": user.id, "customer_phone": "+10000000000"}
    if body:
        mapping.update({k: str(v) for k, v in body.test_values.items()})

    url = _substitute(row.url, mapping)
    for key, value in (row.path_params or {}).items():
        url = url.replace("{" + key + "}", _substitute(str(value), mapping))
    headers = _substitute(dict(row.headers or {}), mapping)

    # Auth from the secret reference — resolved server-side, never echoed.
    secret_value = ""
    if row.secret_ref:
        env_key = re.sub(r"[^A-Za-z0-9]", "_", row.secret_ref.removeprefix("secret://")).upper()
        secret_value = settings.resolve_secret(f"env:{env_key}")
    if row.auth_type == "bearer" and secret_value:
        headers["Authorization"] = f"Bearer {secret_value}"
    elif row.auth_type == "api_key" and secret_value:
        headers.setdefault("X-API-Key", secret_value)
    elif row.auth_type == "basic" and secret_value:
        import base64

        headers["Authorization"] = "Basic " + base64.b64encode(secret_value.encode()).decode()

    result = safe_request(
        method=row.method,
        url=url,
        headers=headers,
        params=_substitute(dict(row.query_params or {}), mapping) or None,
        json_body=_substitute(row.body_template, mapping) if row.body_template else None,
        timeout_ms=row.timeout_ms,
        sensitive_headers=set(row.sensitive_masks or []),
    )

    okay = result.ok
    if okay and row.success_condition:
        # Success condition: restricted "status < 400"-style expressions.
        m = re.fullmatch(r"\s*status\s*(==|<=|>=|<|>)\s*(\d{3})\s*", row.success_condition)
        if m:
            op, target = m.group(1), int(m.group(2))
            okay = {
                "==": result.status_code == target, "<": result.status_code < target,
                ">": result.status_code > target, "<=": result.status_code <= target,
                ">=": result.status_code >= target,
            }[op]

    row.last_tested_at = datetime.now(timezone.utc)
    row.last_latency_ms = result.latency_ms
    row.status = "healthy" if okay else "failing"
    record_audit(
        db, user=user, action="Tested API connection", entity_type="api_connection",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id,
        new_value={"ok": okay, "status": result.status_code, "latencyMs": result.latency_ms},
        request=request,
    )
    db.commit()
    return ok({
        "ok": okay,
        "latencyMs": result.latency_ms,
        "status": result.status_code,
        "contentType": result.content_type,
        "body": result.body_preview,
        "truncated": result.truncated,
        "error": result.error,
        "redirectedTo": result.redirected_to,
        "headersSent": result.headers_sent,  # sensitive values masked
        "userMessage": (row.success_message if okay else row.failure_message) or None,
    })


class UpdateApiConnectionRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    method: str | None = Field(default=None, pattern="^(GET|POST|PUT|PATCH|DELETE)$")
    url: str | None = Field(default=None, max_length=500)
    auth_type: str | None = Field(default=None, alias="authType",
                                  pattern="^(none|api_key|oauth2|bearer|basic)$")
    secret_ref: str | None = Field(default=None, alias="secretRef", max_length=300)
    headers: dict[str, str] | None = None
    query_params: dict[str, str] | None = Field(default=None, alias="queryParams")
    path_params: dict[str, str] | None = Field(default=None, alias="pathParams")
    body_template: dict | None = Field(default=None, alias="bodyTemplate")
    request_schema: dict | None = Field(default=None, alias="requestSchema")
    response_schema: dict | None = Field(default=None, alias="responseSchema")
    success_condition: str | None = Field(default=None, alias="successCondition", max_length=200)
    success_message: str | None = Field(default=None, alias="successMessage", max_length=500)
    failure_message: str | None = Field(default=None, alias="failureMessage", max_length=500)
    error_mapping: dict | None = Field(default=None, alias="errorMapping")
    sensitive_masks: list[str] | None = Field(default=None, alias="sensitiveMasks")
    allowed_intents: list[str] | None = Field(default=None, alias="allowedIntents")
    allowed_workflows: list[str] | None = Field(default=None, alias="allowedWorkflows")
    is_state_changing: bool | None = Field(default=None, alias="isStateChanging")
    require_confirmation: bool | None = Field(default=None, alias="requireConfirmation")
    timeout_ms: int | None = Field(default=None, alias="timeoutMs", ge=100, le=60000)
    retries: int | None = Field(default=None, ge=0, le=5)
    response_mapping: list[dict] | None = Field(default=None, alias="responseMapping")
    status: str | None = Field(default=None, pattern="^(healthy|degraded|failing|untested|disabled)$")

    model_config = {"populate_by_name": True}


@router.patch("/api-connections/{conn_id}")
def update_api_connection(
    conn_id: str,
    body: UpdateApiConnectionRequest,
    request: Request,
    user: User = Depends(require_permission("manage_api_connections", "integrations.manage")),
    db: Session = Depends(get_db),
):
    row = _conn_checked(db, conn_id, user)
    _check_secret_ref(body.secret_ref)
    _validate_variables(body.url, body.headers, body.query_params, body.path_params,
                        body.body_template)
    if body.allowed_intents is not None or body.allowed_workflows is not None:
        _validate_associations(
            db, row.tenant_id,
            allowed_intents=body.allowed_intents,
            allowed_workflows=body.allowed_workflows,
        )
    before = {"url": row.url, "method": row.method, "version": row.version}
    changed = False
    for field in ("name", "description", "method", "url", "auth_type", "secret_ref",
                  "headers", "query_params", "path_params", "body_template",
                  "request_schema", "response_schema", "success_condition",
                  "success_message", "failure_message", "error_mapping",
                  "sensitive_masks", "allowed_intents", "allowed_workflows",
                  "is_state_changing", "require_confirmation", "timeout_ms",
                  "retries", "response_mapping", "status"):
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


@router.post("/api-connections/{conn_id}/duplicate", status_code=201)
def duplicate_api_connection(
    conn_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_api_connections", "integrations.manage")),
    db: Session = Depends(get_db),
):
    src = _conn_checked(db, conn_id, user)
    base, name, n = f"{src.name} (copy)", f"{src.name} (copy)", 2
    while db.scalar(select(ApiConnection).where(
        ApiConnection.tenant_id == src.tenant_id, ApiConnection.name == name,
        ApiConnection.is_deleted.is_(False),
    )):
        name, n = f"{base} {n}", n + 1
    clone = ApiConnection(
        id=new_id("api"), tenant_id=src.tenant_id, bot_id=src.bot_id, name=name,
        description=src.description, method=src.method, url=src.url,
        auth_type=src.auth_type, secret_ref=src.secret_ref, headers=src.headers,
        query_params=src.query_params, path_params=src.path_params,
        body_template=src.body_template, request_schema=src.request_schema,
        response_schema=src.response_schema, success_condition=src.success_condition,
        success_message=src.success_message, failure_message=src.failure_message,
        error_mapping=src.error_mapping, sensitive_masks=src.sensitive_masks,
        allowed_intents=src.allowed_intents, allowed_workflows=src.allowed_workflows,
        is_state_changing=src.is_state_changing,
        require_confirmation=src.require_confirmation, timeout_ms=src.timeout_ms,
        retries=src.retries, response_mapping=src.response_mapping,
        status="untested", version=1, created_by=user.id,
    )
    db.add(clone)
    record_audit(
        db, user=user, action="Duplicated API connection", entity_type="api_connection",
        entity_id=clone.id, target_label=name, tenant_id=src.tenant_id,
        new_value={"from": src.id}, request=request,
    )
    db.commit()
    return ok(serialize_api_connection(clone))


@router.delete("/api-connections/{conn_id}")
def delete_api_connection(
    conn_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_permission("manage_api_connections", "integrations.manage")),
    db: Session = Depends(get_db),
):
    row = _conn_checked(db, conn_id, user)
    used_by = db.scalars(
        select(Intent.name).where(
            Intent.api_connection_id == row.id, Intent.is_deleted.is_(False)
        )
    ).all()
    if used_by:
        raise ApiError(
            f"This connection is used by {len(used_by)} intent(s): "
            f"{', '.join(used_by[:5])}. Detach it from those intents first, "
            "or deactivate it instead.",
            409,
        )
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="Archived API connection", entity_type="api_connection",
        entity_id=row.id, target_label=row.name, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})
