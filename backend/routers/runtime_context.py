"""Runtime Context / User Details configuration — tenant-defined, per bot.

The Studio's "Runtime Context" section: which fields a bot's calls know
about (any domain — loans, patients, properties, leads), where live values
come from (the configured User Details API or a manual test JSON), how
sensitive values are masked, and optional stored per-customer records.

Security model (same as customer_context.py):
- every read resolves through the bot row + `assert_tenant_access`, so a
  schema/record id can never be dereferenced across tenants (404, never 403);
- record responses mask fields the schema marks sensitive — raw values are
  write-only;
- payloads are validated against the tenant's own field definitions with
  types preserved exactly (a number stays a number, nested JSON survives).
"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_tenant_admin,
)
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.customer_context import phone_tail
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.models import (
    ApiConnection,
    RuntimeContextRecord,
    RuntimeContextSchema,
    User,
    VoiceBot,
)
from shared.models.context_models import (
    CONTEXT_FIELD_TYPES,
    CONTEXT_SOURCE_MODES,
    DOMAIN_POLICIES,
)
from shared.runtime_context import (
    build_runtime_context,
    validate_field_definitions,
    validate_payload,
)

router = APIRouter(tags=["Runtime Context"])

_PHONE_PATTERN = r"^\+?[0-9][0-9 \-]{6,18}$"


def _get_bot(db: Session, user: User, bot_id: str) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("Bot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _get_schema(db: Session, bot: VoiceBot) -> RuntimeContextSchema | None:
    return db.scalar(
        select(RuntimeContextSchema).where(
            RuntimeContextSchema.bot_id == bot.id,
            RuntimeContextSchema.is_deleted.is_(False),
        )
    )


def _get_record(db: Session, user: User, record_id: str) -> RuntimeContextRecord:
    row = db.get(RuntimeContextRecord, record_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Context record")
    assert_tenant_access(user, row.tenant_id)
    return row


def _serialize_schema(row: RuntimeContextSchema | None, bot: VoiceBot) -> dict:
    if row is None:
        # Defaults for a bot that has not configured runtime context yet —
        # the UI renders this as the starting state.
        return {
            "id": None, "botId": bot.id, "name": "User details",
            "sourceMode": "manual", "apiConnectionId": None, "responsePath": None,
            "fields": [], "allowAdditional": True, "testPayload": None,
            "missingValuePolicy": None, "domainPolicy": "generic",
            "status": "active", "configured": False,
        }
    return {
        "id": row.id, "botId": row.bot_id, "name": row.name,
        "sourceMode": row.source_mode, "apiConnectionId": row.api_connection_id,
        "responsePath": row.response_path, "fields": row.fields or [],
        "allowAdditional": bool(row.allow_additional),
        "testPayload": row.test_payload,
        "missingValuePolicy": row.missing_value_policy,
        "domainPolicy": row.domain_policy, "status": row.status,
        "configured": True,
    }


def _masked_record_data(data: dict | None, fields: list | None) -> dict:
    """Record payload with schema-sensitive fields masked for reads."""
    ctx = build_runtime_context(
        tenant_id="", bot_id="", field_definitions=fields or [],
        payload=data or {}, payload_source="record",
    )
    return {e.key: e.value for e in ctx.values.values()}


def _serialize_record(row: RuntimeContextRecord, fields: list | None) -> dict:
    tail = phone_tail(row.phone)
    return {
        "id": row.id, "botId": row.bot_id,
        "customerRef": row.customer_ref,
        "phoneMasked": (f"XXXXXX{tail[-4:]}" if tail else None),
        "data": _masked_record_data(row.data, fields),
        "callState": row.call_state or {},
        "updatedAt": row.updated_at.isoformat() + "Z" if row.updated_at else None,
    }


# ── schema configuration ─────────────────────────────────────────────────────


class SchemaPayload(BaseModel):
    name: str = Field(default="User details", max_length=200)
    source_mode: str = Field(default="manual", alias="sourceMode")
    api_connection_id: str | None = Field(default=None, alias="apiConnectionId")
    response_path: str | None = Field(default=None, alias="responsePath", max_length=200)
    fields: list[dict] = Field(default_factory=list)
    allow_additional: bool = Field(default=True, alias="allowAdditional")
    test_payload: dict | None = Field(default=None, alias="testPayload")
    missing_value_policy: str | None = Field(
        default=None, alias="missingValuePolicy", max_length=500
    )
    domain_policy: str = Field(default="generic", alias="domainPolicy")

    model_config = {"populate_by_name": True}


@router.get("/bots/{bot_id}/runtime-context")
def get_runtime_context_config(
    bot_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot(db, user, bot_id)
    return ok(_serialize_schema(_get_schema(db, bot), bot))


@router.put("/bots/{bot_id}/runtime-context")
def upsert_runtime_context_config(
    bot_id: str,
    body: SchemaPayload,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _get_bot(db, user, bot_id)

    errors = validate_field_definitions(body.fields)
    if body.source_mode not in CONTEXT_SOURCE_MODES:
        errors.append({"field": "sourceMode",
                       "message": f"Must be one of {list(CONTEXT_SOURCE_MODES)}."})
    if body.domain_policy not in DOMAIN_POLICIES:
        errors.append({"field": "domainPolicy",
                       "message": f"Must be one of {list(DOMAIN_POLICIES)}."})
    for item in body.fields:
        if isinstance(item, dict) and item.get("type") not in (None, *CONTEXT_FIELD_TYPES):
            pass  # already reported by validate_field_definitions
    if body.source_mode == "api":
        if not body.api_connection_id:
            errors.append({"field": "apiConnectionId",
                           "message": "Select the User Details API connection."})
        else:
            conn = db.get(ApiConnection, body.api_connection_id)
            if conn is None or conn.is_deleted or conn.tenant_id != bot.tenant_id \
                    or (conn.bot_id is not None and conn.bot_id != bot.id):
                errors.append({"field": "apiConnectionId",
                               "message": "Unknown API connection for this bot."})
    if body.test_payload is not None:
        payload_errors, _ = validate_payload(
            body.fields, body.test_payload, allow_additional=body.allow_additional
        )
        errors.extend(
            {"field": f"testPayload.{e['field']}", "message": e["message"]}
            for e in payload_errors
        )
    if errors:
        raise ApiError("The runtime context configuration is invalid.", 422,
                       errors=errors)

    row = _get_schema(db, bot)
    created = row is None
    if row is None:
        row = RuntimeContextSchema(
            id=new_id("rcs"), tenant_id=bot.tenant_id, bot_id=bot.id,
            created_by=user.id,
        )
        db.add(row)
    row.name = body.name
    row.source_mode = body.source_mode
    row.api_connection_id = body.api_connection_id if body.source_mode == "api" else None
    row.response_path = body.response_path
    row.fields = body.fields
    row.allow_additional = body.allow_additional
    row.test_payload = body.test_payload
    row.missing_value_policy = body.missing_value_policy
    row.domain_policy = body.domain_policy
    row.updated_by = user.id
    record_audit(
        db, user=user,
        action="runtime_context.create" if created else "runtime_context.update",
        entity_type="runtime_context_schema", entity_id=row.id,
        target_label=f"{bot.name} runtime context", tenant_id=bot.tenant_id,
        new_value={"sourceMode": row.source_mode, "fields": len(row.fields or []),
                   "domainPolicy": row.domain_policy},
        request=request,
    )
    db.commit()
    db.refresh(row)
    return ok(_serialize_schema(row, bot))


class ValidatePayloadRequest(BaseModel):
    payload: dict = Field(default_factory=dict)
    # Validate against these fields instead of the saved ones (unsaved edits).
    fields: list[dict] | None = None
    allow_additional: bool | None = Field(default=None, alias="allowAdditional")

    model_config = {"populate_by_name": True}


@router.post("/bots/{bot_id}/runtime-context/validate")
def validate_context_payload(
    bot_id: str,
    body: ValidatePayloadRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validate a payload (API response or manual test JSON) against the
    schema and return the effective, source-tagged, masked context — exactly
    what a live call would see."""
    bot = _get_bot(db, user, bot_id)
    saved = _get_schema(db, bot)
    fields = body.fields if body.fields is not None else (saved.fields if saved else [])
    allow_additional = (
        body.allow_additional if body.allow_additional is not None
        else (bool(saved.allow_additional) if saved else True)
    )
    errors, clean = validate_payload(fields, body.payload,
                                     allow_additional=allow_additional)
    ctx = build_runtime_context(
        tenant_id=bot.tenant_id, bot_id=bot.id,
        field_definitions=fields, payload=clean,
        payload_source=("api" if saved and saved.source_mode == "api" else "test"),
        allow_additional=allow_additional,
        missing_value_policy=saved.missing_value_policy if saved else None,
    )
    return ok({
        "valid": not errors,
        "errors": errors,
        "effective": ctx.items_with_sources(),
        "missingRequired": ctx.missing_required(),
        "declaredMissing": ctx.declared_missing(),
        "promptSection": ctx.prompt_section(),
    })


# ── stored records (per-customer payloads, any domain) ──────────────────────


class RecordPayload(BaseModel):
    customer_ref: str | None = Field(default=None, alias="customerRef", max_length=80)
    phone: str | None = Field(default=None, pattern=_PHONE_PATTERN)
    data: dict = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


@router.get("/bots/{bot_id}/runtime-context/records")
def list_context_records(
    bot_id: str,
    params: PageParams = Depends(page_params),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot(db, user, bot_id)
    schema = _get_schema(db, bot)
    stmt = select(RuntimeContextRecord).where(
        RuntimeContextRecord.tenant_id == bot.tenant_id,
        RuntimeContextRecord.bot_id == bot.id,
        RuntimeContextRecord.is_deleted.is_(False),
    )
    if params.search:
        stmt = stmt.where(RuntimeContextRecord.customer_ref.like(f"%{params.search}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(RuntimeContextRecord.updated_at.desc())
        .offset(params.offset).limit(params.page_size)
    ).all()
    fields = schema.fields if schema else []
    return paginated(
        [_serialize_record(r, fields) for r in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


@router.post("/bots/{bot_id}/runtime-context/records", status_code=201)
def create_context_record(
    bot_id: str,
    body: RecordPayload,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _get_bot(db, user, bot_id)
    schema = _get_schema(db, bot)
    fields = schema.fields if schema else []
    errors, clean = validate_payload(
        fields, body.data,
        allow_additional=bool(schema.allow_additional) if schema else True,
    )
    if errors:
        raise ApiError("The context data is invalid for this bot's schema.",
                       422, errors=errors)
    row = RuntimeContextRecord(
        id=new_id("rcr"), tenant_id=bot.tenant_id, bot_id=bot.id,
        customer_ref=body.customer_ref, phone=body.phone, data=clean,
        created_by=user.id, updated_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="runtime_context_record.create",
        entity_type="runtime_context_record", entity_id=row.id,
        target_label=row.customer_ref or row.id, tenant_id=bot.tenant_id,
        request=request,
    )
    db.commit()
    db.refresh(row)
    return ok(_serialize_record(row, fields))


@router.patch("/runtime-context-records/{record_id}")
def update_context_record(
    record_id: str,
    body: RecordPayload,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _get_record(db, user, record_id)
    bot = _get_bot(db, user, row.bot_id)
    schema = _get_schema(db, bot)
    fields = schema.fields if schema else []
    errors, clean = validate_payload(
        fields, body.data,
        allow_additional=bool(schema.allow_additional) if schema else True,
    )
    if errors:
        raise ApiError("The context data is invalid for this bot's schema.",
                       422, errors=errors)
    if body.customer_ref is not None:
        row.customer_ref = body.customer_ref
    if body.phone is not None:
        row.phone = body.phone
    row.data = clean
    row.updated_by = user.id
    record_audit(
        db, user=user, action="runtime_context_record.update",
        entity_type="runtime_context_record", entity_id=row.id,
        target_label=row.customer_ref or row.id, tenant_id=row.tenant_id,
        request=request,
    )
    db.commit()
    db.refresh(row)
    return ok(_serialize_record(row, fields))


@router.delete("/runtime-context-records/{record_id}")
def delete_context_record(
    record_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _get_record(db, user, record_id)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="runtime_context_record.delete",
        entity_type="runtime_context_record", entity_id=row.id,
        target_label=row.customer_ref or row.id, tenant_id=row.tenant_id,
        request=request,
    )
    db.commit()
    return ok({"deleted": True})
