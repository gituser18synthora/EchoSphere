"""Tenant management (Super Admin) + tenant settings."""

from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import extract, func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    get_current_user,
    is_super_admin,
    require_super_admin,
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.security import hash_password
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.db.mysql import get_db
from backend.models import (
    Plan,
    Role,
    Subscription,
    Tenant,
    TenantSetting,
    UsageRecord,
    User,
    VoiceBot,
)
from backend.serializers import serialize_tenant, serialize_tenant_settings

router = APIRouter(tags=["Tenants"])


def _month_usage(db: Session, tenant_ids: list[str]) -> dict[str, dict]:
    """Current-month rollups per tenant (calls, minutes, ai cost)."""
    if not tenant_ids:
        return {}
    today = date.today()
    rows = db.execute(
        select(
            UsageRecord.tenant_id,
            func.coalesce(func.sum(UsageRecord.calls), 0),
            func.coalesce(func.sum(UsageRecord.minutes), 0),
            func.coalesce(
                func.sum(
                    UsageRecord.cost_llm + UsageRecord.cost_tts + UsageRecord.cost_stt
                ),
                0,
            ),
        )
        .where(
            UsageRecord.tenant_id.in_(tenant_ids),
            UsageRecord.bot_id.is_(None),
            extract("year", UsageRecord.date) == today.year,
            extract("month", UsageRecord.date) == today.month,
        )
        .group_by(UsageRecord.tenant_id)
    ).all()
    return {r[0]: {"calls": int(r[1]), "minutes": float(r[2]), "ai_cost": float(r[3])} for r in rows}


def _tenant_context(db: Session, tenants: list[Tenant]) -> list[dict]:
    ids = [t.id for t in tenants]
    if not ids:
        return []
    user_counts = dict(
        db.execute(
            select(User.tenant_id, func.count())
            .where(User.tenant_id.in_(ids), User.is_deleted.is_(False))
            .group_by(User.tenant_id)
        ).all()
    )
    bot_counts = dict(
        db.execute(
            select(VoiceBot.tenant_id, func.count())
            .where(VoiceBot.tenant_id.in_(ids), VoiceBot.is_deleted.is_(False))
            .group_by(VoiceBot.tenant_id)
        ).all()
    )
    subs = db.execute(
        select(Subscription.tenant_id, Plan.code, Subscription.mrr)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.tenant_id.in_(ids), Subscription.is_deleted.is_(False))
    ).all()
    sub_map = {s[0]: {"plan": s[1], "mrr": float(s[2])} for s in subs}
    usage = _month_usage(db, ids)

    out = []
    for t in tenants:
        u = usage.get(t.id, {})
        s = sub_map.get(t.id, {})
        out.append(
            serialize_tenant(
                t,
                plan=s.get("plan"),
                users=user_counts.get(t.id, 0),
                bots=bot_counts.get(t.id, 0),
                calls_month=u.get("calls", 0),
                minutes_month=u.get("minutes", 0.0),
                mrr=s.get("mrr", 0.0),
                ai_cost_month=u.get("ai_cost", 0.0),
            )
        )
    return out


@router.get("/tenants")
def list_tenants(
    params: PageParams = Depends(page_params),
    status: str | None = Query(None),
    plan: str | None = Query(None),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    stmt = select(Tenant).where(Tenant.is_deleted.is_(False))
    if status:
        stmt = stmt.where(Tenant.status == status)
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(or_(Tenant.name.like(like), Tenant.domain.like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Tenant.created_at.asc()).offset(params.offset).limit(params.page_size)
    ).all()
    data = _tenant_context(db, rows)
    if plan:
        data = [d for d in data if d["plan"] == plan]
    return paginated(data, page=params.page, page_size=params.page_size, total=total)


@router.get("/tenants/{tenant_id}")
def get_tenant(
    tenant_id: str,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    if not is_super_admin(user) and user.tenant_id != tenant_id:
        raise NotFoundError("Tenant")
    t = db.get(Tenant, tenant_id)
    if t is None or t.is_deleted:
        raise NotFoundError("Tenant")
    return ok(_tenant_context(db, [t])[0])


class CreateTenantRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=3, max_length=255)
    industry: str | None = None
    region: str | None = None
    plan_code: str = Field(default="starter", alias="planCode")
    admin_email: EmailStr = Field(alias="adminEmail")
    admin_name: str = Field(default="Tenant Admin", alias="adminName")
    admin_password: str | None = Field(default=None, alias="adminPassword", min_length=8)
    status: str = Field(default="provisioning", pattern="^(active|trial|suspended|provisioning)$")
    seats: int | None = Field(default=None, ge=1)

    model_config = {"populate_by_name": True}


@router.post("/tenants", status_code=201)
def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Tenant).where(Tenant.domain == body.domain.lower())):
        raise ApiError("A tenant with this domain already exists.", 409)
    plan = db.scalar(select(Plan).where(Plan.code == body.plan_code))
    if plan is None:
        raise ApiError("Unknown plan.", 422)

    # Multi-table create — one transaction.
    tenant = Tenant(
        id=new_id("tn"),
        name=body.name,
        domain=body.domain.lower(),
        industry=body.industry,
        region=body.region,
        status=body.status,
        health="neutral",
        admin_email=body.admin_email.lower(),
        created_by=user.id,
    )
    db.add(tenant)
    db.flush()  # tenant row must exist before subscription/settings/admin user FKs

    db.add(
        Subscription(
            id=new_id("sub"),
            tenant_id=tenant.id,
            plan_id=plan.id,
            seats=body.seats or plan.seats_included,
            bot_limit=plan.bot_limit,
            minutes_included=plan.minutes_included,
            status="trial" if body.status == "trial" else "active",
            mrr=0 if body.status == "trial" else plan.price_monthly,
            created_by=user.id,
        )
    )

    db.add(
        TenantSetting(
            id=new_id("tset"),
            tenant_id=tenant.id,
            display_name=body.name,
            created_by=user.id,
        )
    )

    admin_user_payload = None
    existing_admin = db.scalar(select(User).where(User.email == body.admin_email.lower()))
    if existing_admin is None:
        import secrets

        password = body.admin_password or secrets.token_urlsafe(12)
        role = db.scalar(select(Role).where(Role.code == "tenant_admin"))
        if role is None:
            raise ApiError("tenant_admin role is missing — run the seed.", 500)
        admin = User(
            id=new_id("usr"),
            email=body.admin_email.lower(),
            name=body.admin_name,
            password_hash=hash_password(password),
            role_id=role.id,
            tenant_id=tenant.id,
            status="invited" if body.admin_password is None else "active",
            created_by=user.id,
        )
        db.add(admin)
        admin_user_payload = {"email": admin.email}
        if body.admin_password is None:
            admin_user_payload["temporaryPassword"] = password

    record_audit(
        db, user=user, action="Created tenant", entity_type="tenant",
        entity_id=tenant.id, target_label=tenant.name, tenant_id=tenant.id,
        new_value={"name": tenant.name, "domain": tenant.domain, "plan": plan.code},
        request=request,
    )
    db.commit()
    data = _tenant_context(db, [tenant])[0]
    if admin_user_payload:
        data["adminUser"] = admin_user_payload
    return ok(data)


class UpdateTenantRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    industry: str | None = None
    region: str | None = None
    status: str | None = Field(default=None, pattern="^(active|trial|suspended|provisioning)$")
    health: str | None = Field(default=None, pattern="^(good|warning|serious|critical|neutral)$")
    admin_email: EmailStr | None = Field(default=None, alias="adminEmail")

    model_config = {"populate_by_name": True}


@router.patch("/tenants/{tenant_id}")
def update_tenant(
    tenant_id: str,
    body: UpdateTenantRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    t = db.get(Tenant, tenant_id)
    if t is None or t.is_deleted:
        raise NotFoundError("Tenant")
    before = {"name": t.name, "status": t.status, "health": t.health}
    for field in ("name", "industry", "region", "status", "health"):
        val = getattr(body, field)
        if val is not None:
            setattr(t, field, val)
    if body.admin_email:
        t.admin_email = body.admin_email.lower()
    t.updated_by = user.id
    record_audit(
        db, user=user, action="Updated tenant", entity_type="tenant", entity_id=t.id,
        target_label=t.name, tenant_id=t.id, previous_value=before,
        new_value={"name": t.name, "status": t.status, "health": t.health},
        request=request,
    )
    db.commit()
    return ok(_tenant_context(db, [t])[0])


@router.delete("/tenants/{tenant_id}")
def delete_tenant(
    tenant_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    t = db.get(Tenant, tenant_id)
    if t is None or t.is_deleted:
        raise NotFoundError("Tenant")
    if hard:
        guard_hard_delete()
    soft_delete(t, user)
    t.status = "suspended"
    record_audit(
        db, user=user, action="Archived tenant", entity_type="tenant", entity_id=t.id,
        target_label=t.name, tenant_id=t.id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": t.id})


# ── Tenant settings ───────────────────────────────────────────────────────────


class TenantSettingsRequest(BaseModel):
    display_name: str | None = Field(default=None, alias="displayName", max_length=200)
    timezone: str | None = None
    default_languages: list[str] | None = Field(default=None, alias="defaultLanguages")
    branding: dict | None = None
    business_hours: dict | None = Field(default=None, alias="businessHours")
    holidays: list | None = None
    notifications: list | None = None
    security: dict | None = None
    retention_days: int | None = Field(default=None, alias="retentionDays", ge=1, le=3650)

    model_config = {"populate_by_name": True}


def _get_or_create_settings(db: Session, tenant_id: str, user: User) -> TenantSetting:
    s = db.scalar(select(TenantSetting).where(TenantSetting.tenant_id == tenant_id))
    if s is None:
        s = TenantSetting(id=new_id("tset"), tenant_id=tenant_id, created_by=user.id)
        db.add(s)
        db.flush()
    return s


@router.get("/tenant/settings")
def get_tenant_settings(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    if db.get(Tenant, tid) is None:
        raise NotFoundError("Tenant")
    s = _get_or_create_settings(db, tid, user)
    db.commit()
    return ok(serialize_tenant_settings(s))


@router.put("/tenant/settings")
def update_tenant_settings(
    body: TenantSettingsRequest,
    request: Request,
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    s = _get_or_create_settings(db, tid, user)
    before = serialize_tenant_settings(s)
    for field in (
        "display_name", "timezone", "default_languages", "branding",
        "business_hours", "holidays", "notifications", "security", "retention_days",
    ):
        val = getattr(body, field)
        if val is not None:
            setattr(s, field, val)
    s.updated_by = user.id
    record_audit(
        db, user=user, action="Updated tenant settings", entity_type="tenant_settings",
        entity_id=s.id, target_label=s.display_name, tenant_id=tid,
        previous_value=before, new_value=serialize_tenant_settings(s), request=request,
    )
    db.commit()
    return ok(serialize_tenant_settings(s))
