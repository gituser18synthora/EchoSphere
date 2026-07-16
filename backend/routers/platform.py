"""Platform governance & operations: alerts, health, models, guardrails,
phone numbers, SIP trunks, reference templates, system settings."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    get_current_user,
    is_super_admin,
    require_super_admin,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.models import (
    ApprovedModel,
    Guardrail,
    HealthMetric,
    PhoneNumber,
    PlatformAlert,
    PlatformTemplate,
    SipTrunk,
    SystemSetting,
    Tenant,
    User,
    VoiceBot,
)
from backend.serializers import (
    serialize_alert,
    serialize_guardrail,
    serialize_health_metric,
    serialize_model,
    serialize_phone_number,
    serialize_sip_trunk,
)

router = APIRouter(tags=["Platform"])


# ── Alerts ───────────────────────────────────────────────────────────────────


@router.get("/alerts")
def list_alerts(
    status: str | None = Query(None, pattern="^(open|acknowledged|resolved)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(PlatformAlert).where(PlatformAlert.is_deleted.is_(False))
    if not is_super_admin(user):
        # Tenant members see only alerts scoped to their tenant.
        stmt = stmt.where(
            PlatformAlert.scope == "tenant", PlatformAlert.tenant_id == user.tenant_id
        )
    if status:
        stmt = stmt.where(PlatformAlert.status == status)
    rows = db.scalars(stmt.order_by(PlatformAlert.occurred_at.desc()).limit(100)).all()
    return ok([serialize_alert(a) for a in rows])


class AlertUpdateRequest(BaseModel):
    status: str = Field(pattern="^(open|acknowledged|resolved)$")


@router.patch("/alerts/{alert_id}")
def update_alert(
    alert_id: str,
    body: AlertUpdateRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.get(PlatformAlert, alert_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Alert")
    before = {"status": row.status}
    row.status = body.status
    record_audit(
        db, user=user, action=f"Alert {body.status}", entity_type="alert",
        entity_id=row.id, target_label=row.title, previous_value=before,
        new_value={"status": row.status}, request=request,
    )
    db.commit()
    return ok(serialize_alert(row))


# ── Health ───────────────────────────────────────────────────────────────────


@router.get("/health-metrics")
def platform_health(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(HealthMetric).order_by(HealthMetric.sort_order)).all()
    return ok([serialize_health_metric(h) for h in rows])


# ── Approved models ──────────────────────────────────────────────────────────


@router.get("/models")
def list_models(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(ApprovedModel)
        .where(ApprovedModel.is_deleted.is_(False))
        .order_by(ApprovedModel.created_at.asc())
    ).all()
    return ok([serialize_model(m) for m in rows])


class ModelUpdateRequest(BaseModel):
    status: str = Field(pattern="^(approved|testing|deprecated)$")


@router.patch("/models/{model_id}")
def update_model(
    model_id: str,
    body: ModelUpdateRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.get(ApprovedModel, model_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Model")
    before = {"status": row.status}
    row.status = body.status
    row.updated_by = user.id
    record_audit(
        db, user=user,
        action="Approved model for production" if body.status == "approved" else "Updated model status",
        entity_type="approved_model", entity_id=row.id,
        target_label=f"{row.name} · {row.purpose}", previous_value=before,
        new_value={"status": row.status}, request=request,
    )
    db.commit()
    return ok(serialize_model(row))


# ── Guardrails ───────────────────────────────────────────────────────────────


@router.get("/guardrails")
def list_guardrails(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Guardrail)
        .where(Guardrail.is_deleted.is_(False))
        .order_by(Guardrail.created_at.asc())
    ).all()
    return ok([serialize_guardrail(g) for g in rows])


class GuardrailUpdateRequest(BaseModel):
    enabled: bool | None = None
    enforcement: str | None = Field(default=None, pattern="^(block|flag|redact)$")


@router.patch("/guardrails/{guardrail_id}")
def update_guardrail(
    guardrail_id: str,
    body: GuardrailUpdateRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.get(Guardrail, guardrail_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Guardrail")
    before = {"enabled": row.enabled, "enforcement": row.enforcement}
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.enforcement:
        row.enforcement = body.enforcement
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated guardrail", entity_type="guardrail",
        entity_id=row.id, target_label=row.name, previous_value=before,
        new_value={"enabled": row.enabled, "enforcement": row.enforcement},
        request=request,
    )
    db.commit()
    return ok(serialize_guardrail(row))


# ── Phone numbers ────────────────────────────────────────────────────────────


@router.get("/phone-numbers")
def list_phone_numbers(
    user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    rows = db.scalars(
        select(PhoneNumber)
        .where(PhoneNumber.is_deleted.is_(False))
        .order_by(PhoneNumber.created_at.asc())
    ).all()
    tenant_names = dict(db.execute(select(Tenant.id, Tenant.name)).all())
    bot_names = dict(db.execute(select(VoiceBot.id, VoiceBot.name)).all())
    return ok([
        serialize_phone_number(
            p,
            tenant_name=tenant_names.get(p.tenant_id),
            bot_name=bot_names.get(p.bot_id),
        )
        for p in rows
    ])


class PhoneNumberRequest(BaseModel):
    number: str = Field(min_length=5, max_length=30)
    country: str = Field(default="US", max_length=5)
    provider: str = Field(default="", max_length=50)
    tenant_id: str | None = Field(default=None, alias="tenantId")
    bot_id: str | None = Field(default=None, alias="botId")
    status: str = Field(default="available", pattern="^(assigned|available|porting|error)$")
    monthly_cost: float = Field(default=0, alias="monthlyCost", ge=0)

    model_config = {"populate_by_name": True}


@router.post("/phone-numbers", status_code=201)
def create_phone_number(
    body: PhoneNumberRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if db.scalar(select(PhoneNumber).where(PhoneNumber.number == body.number)):
        raise ApiError("This phone number already exists.", 409)
    row = PhoneNumber(
        id=new_id("pn"), number=body.number, country=body.country,
        provider=body.provider, tenant_id=body.tenant_id, bot_id=body.bot_id,
        status="assigned" if body.tenant_id else body.status,
        monthly_cost=body.monthly_cost, created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Added phone number", entity_type="phone_number",
        entity_id=row.id, target_label=row.number, tenant_id=body.tenant_id,
        request=request,
    )
    db.commit()
    tenant_names = dict(db.execute(select(Tenant.id, Tenant.name)).all())
    bot_names = dict(db.execute(select(VoiceBot.id, VoiceBot.name)).all())
    return ok(serialize_phone_number(
        row, tenant_name=tenant_names.get(row.tenant_id), bot_name=bot_names.get(row.bot_id)
    ))


# ── SIP trunks ───────────────────────────────────────────────────────────────


@router.get("/sip-trunks")
def list_sip_trunks(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(SipTrunk).where(SipTrunk.is_deleted.is_(False)).order_by(SipTrunk.name)
    ).all()
    return ok([serialize_sip_trunk(t) for t in rows])


# ── Reference templates (governance libraries, journeys, action blocks) ─────


@router.get("/templates")
def list_templates(
    kind: str = Query(..., max_length=40),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(PlatformTemplate)
        .where(PlatformTemplate.kind == kind, PlatformTemplate.is_deleted.is_(False))
        .order_by(PlatformTemplate.sort_order, PlatformTemplate.created_at)
    ).all()
    return ok([
        {
            "id": t.id, "kind": t.kind, "name": t.name,
            "description": t.description or "", "status": t.status,
            **(t.payload or {}),
        }
        for t in rows
    ])


# ── System settings ──────────────────────────────────────────────────────────


@router.get("/system-settings")
def get_system_settings(
    user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    rows = db.scalars(select(SystemSetting).order_by(SystemSetting.key)).all()
    return ok([
        {"key": s.key, "value": s.value, "description": s.description or ""}
        for s in rows
    ])


class SystemSettingRequest(BaseModel):
    key: str = Field(min_length=1, max_length=100)
    value: dict | list | str | int | bool | None = None
    description: str | None = Field(default=None, max_length=500)


@router.put("/system-settings")
def upsert_system_setting(
    body: SystemSettingRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == body.key))
    before = {"value": row.value} if row else None
    if row is None:
        row = SystemSetting(id=new_id("sys"), key=body.key, created_by=user.id)
        db.add(row)
    row.value = body.value
    if body.description is not None:
        row.description = body.description
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated system setting", entity_type="system_setting",
        entity_id=row.id, target_label=row.key, previous_value=before,
        new_value={"value": row.value}, request=request,
    )
    db.commit()
    return ok({"key": row.key, "value": row.value, "description": row.description or ""})
