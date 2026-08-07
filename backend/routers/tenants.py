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
    require_permission,
    require_super_admin,
    require_tenant_admin,
    resolve_tenant_id,
)
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    validate_password_policy,
)
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.db.mysql import get_db
from shared.models import (
    AiConfigProfile,
    DataRegion,
    Industry,
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
from shared.tenant_languages import validate_language_assignment


def _validate_master_code(db: Session, model, value: str | None, label: str) -> str | None:
    """Resolve a master-data reference by code or name. Only ACTIVE records are
    valid for new assignments; existing tenants keep historical values."""
    if not value:
        return value
    row = db.scalar(
        select(model).where(
            or_(model.code == value, model.name == value),
            model.is_deleted.is_(False),
        )
    )
    if row is None or row.status != "active":
        raise ApiError(f"Unknown or inactive {label}: '{value}'.", 422)
    return row.code

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
    tenant_languages = dict(db.execute(
        select(TenantSetting.tenant_id, TenantSetting.default_languages)
        .where(TenantSetting.tenant_id.in_(ids))
    ).all())

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
                default_languages=tenant_languages.get(t.id),
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
    code: str | None = Field(default=None, max_length=50)
    domain: str = Field(min_length=3, max_length=255)
    industry: str | None = None
    region: str | None = None
    ai_profile_code: str | None = Field(default=None, alias="aiProfileCode")
    plan_code: str = Field(default="starter", alias="planCode")
    admin_email: EmailStr = Field(alias="adminEmail")
    admin_name: str = Field(default="Tenant Admin", alias="adminName")
    admin_password: str | None = Field(
        default=None, alias="adminPassword", min_length=MIN_PASSWORD_LENGTH
    )
    status: str = Field(default="provisioning", pattern="^(active|trial|suspended|provisioning)$")
    seats: int | None = Field(default=None, ge=1)
    default_languages: list[str] | None = Field(
        default=None, alias="defaultLanguages"
    )
    # Post-call intelligence: both OFF unless the Super Admin opts in.
    call_summary_enabled: bool = Field(default=False, alias="callSummaryEnabled")
    use_previous_call_summary: bool = Field(
        default=False, alias="usePreviousCallSummary"
    )

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
    plan = db.scalar(select(Plan).where(Plan.code == body.plan_code, Plan.is_deleted.is_(False)))
    if plan is None or plan.status != "active":
        raise ApiError("Unknown or inactive plan.", 422)

    # Master-data references must be active for NEW tenants (existing tenants
    # keep displaying their historical value even after deactivation).
    industry = _validate_master_code(db, Industry, body.industry, "industry")
    region = _validate_master_code(db, DataRegion, body.region, "data region")
    ai_profile = _validate_master_code(db, AiConfigProfile, body.ai_profile_code, "AI profile")
    default_languages = None
    if body.default_languages is not None:
        default_languages, invalid = validate_language_assignment(
            db, body.default_languages
        )
        if not default_languages:
            raise ApiError("At least one tenant language is required.", 422)
        if invalid:
            raise ApiError(
                f"Unknown or inactive language(s): {', '.join(invalid)}", 422
            )

    code = (body.code or "").strip().lower() or None
    if code and db.scalar(select(Tenant).where(Tenant.code == code)):
        raise ApiError("A tenant with this code already exists.", 409)

    # Multi-table create — one transaction.
    tenant = Tenant(
        id=new_id("tn"),
        name=body.name,
        code=code,
        domain=body.domain.lower(),
        industry=industry,
        region=region,
        ai_profile_code=ai_profile,
        status=body.status,
        health="neutral",
        admin_email=body.admin_email.lower(),
        call_summary_enabled=body.call_summary_enabled,
        use_previous_call_summary=body.use_previous_call_summary,
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
            default_languages=default_languages,
            created_by=user.id,
        )
    )

    admin_user_payload = None
    existing_admin = db.scalar(select(User).where(User.email == body.admin_email.lower()))
    if existing_admin is None:
        import secrets

        # An admin-chosen onboarding password must satisfy the shared policy;
        # the generated temporary password is always above the minimum length.
        if body.admin_password is not None:
            validate_password_policy(body.admin_password, field="adminPassword")
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
    code: str | None = Field(default=None, max_length=50)
    industry: str | None = None
    region: str | None = None
    ai_profile_code: str | None = Field(default=None, alias="aiProfileCode")
    plan_code: str | None = Field(default=None, alias="planCode")
    status: str | None = Field(default=None, pattern="^(active|trial|suspended|provisioning)$")
    health: str | None = Field(default=None, pattern="^(good|warning|serious|critical|neutral)$")
    admin_email: EmailStr | None = Field(default=None, alias="adminEmail")
    default_languages: list[str] | None = Field(
        default=None, alias="defaultLanguages"
    )
    call_summary_enabled: bool | None = Field(
        default=None, alias="callSummaryEnabled"
    )
    use_previous_call_summary: bool | None = Field(
        default=None, alias="usePreviousCallSummary"
    )

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
    before = {"name": t.name, "status": t.status, "health": t.health,
              "region": t.region, "code": t.code,
              "callSummaryEnabled": bool(t.call_summary_enabled),
              "usePreviousCallSummary": bool(t.use_previous_call_summary)}
    for field in ("name", "status", "health",
                  "call_summary_enabled", "use_previous_call_summary"):
        val = getattr(body, field)
        if val is not None:
            setattr(t, field, val)
    if body.industry is not None:
        t.industry = _validate_master_code(db, Industry, body.industry, "industry")
    if body.region is not None:
        t.region = _validate_master_code(db, DataRegion, body.region, "data region")
    if body.ai_profile_code is not None:
        t.ai_profile_code = _validate_master_code(db, AiConfigProfile, body.ai_profile_code, "AI profile")
    if body.code is not None:
        code = body.code.strip().lower() or None
        if code and code != t.code and db.scalar(select(Tenant).where(Tenant.code == code)):
            raise ApiError("A tenant with this code already exists.", 409)
        t.code = code
    if body.plan_code is not None:
        plan = db.scalar(select(Plan).where(Plan.code == body.plan_code, Plan.is_deleted.is_(False)))
        if plan is None or plan.status != "active":
            raise ApiError("Unknown or inactive plan.", 422)
        sub = db.scalar(
            select(Subscription).where(
                Subscription.tenant_id == t.id, Subscription.is_deleted.is_(False)
            )
        )
        if sub is not None and sub.plan_id != plan.id:
            # Snapshot the new plan's limits onto the subscription explicitly —
            # editing the plan definition later never silently changes them.
            sub.plan_id = plan.id
            sub.bot_limit = plan.bot_limit
            sub.minutes_included = plan.minutes_included
            sub.mrr = plan.price_monthly if sub.status == "active" else sub.mrr
            record_audit(
                db, user=user, action="Changed tenant plan", entity_type="tenant",
                entity_id=t.id, target_label=t.name, tenant_id=t.id,
                new_value={"plan": plan.code}, request=request,
            )
    if body.admin_email:
        t.admin_email = body.admin_email.lower()
    if body.default_languages is not None:
        settings = _get_or_create_settings(db, t.id, user)
        before["defaultLanguages"] = settings.default_languages or []
        languages, invalid = validate_language_assignment(
            db,
            body.default_languages,
            existing=settings.default_languages,
        )
        if not languages:
            raise ApiError("At least one tenant language is required.", 422)
        if invalid:
            raise ApiError(
                f"Unknown or inactive language(s): {', '.join(invalid)}", 422
            )
        settings.default_languages = languages
        settings.updated_by = user.id
    t.updated_by = user.id
    record_audit(
        db, user=user, action="Updated tenant", entity_type="tenant", entity_id=t.id,
        target_label=t.name, tenant_id=t.id, previous_value=before,
        new_value={
            "name": t.name,
            "status": t.status,
            "health": t.health,
            "callSummaryEnabled": bool(t.call_summary_enabled),
            "usePreviousCallSummary": bool(t.use_previous_call_summary),
            **(
                {"defaultLanguages": settings.default_languages}
                if body.default_languages is not None else {}
            ),
        },
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


# ── Tenant profile ────────────────────────────────────────────────────────────
#
# Field-level permissions:
#   Tenant Admin edits: display name, logo/branding, website, contact details,
#     address, timezone, default language, support contact, working hours.
#   Super Admin controls (via PATCH /tenants/{id}): tenant code, plan,
#     subscription status, data region, activation status, hard limits.


@router.get("/tenant/profile")
def get_tenant_profile(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(require_permission("view_tenant_profile", "tenants.manage")),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    t = db.get(Tenant, tid)
    if t is None or t.is_deleted:
        raise NotFoundError("Tenant")
    s = _get_or_create_settings(db, tid, user)
    sub = db.scalar(
        select(Subscription).where(
            Subscription.tenant_id == tid, Subscription.is_deleted.is_(False)
        )
    )
    plan = db.get(Plan, sub.plan_id) if sub else None
    region = db.scalar(select(DataRegion).where(
        or_(DataRegion.code == t.region, DataRegion.name == t.region))) if t.region else None
    db.commit()
    branding = s.branding or {}
    return ok({
        "tenantId": t.id,
        "name": t.name,
        "displayName": s.display_name or t.name,
        "code": t.code or "",
        "domain": t.domain,
        "industry": t.industry or "",
        "website": t.website or "",
        "contactName": t.contact_name or "",
        "contactEmail": t.contact_email or "",
        "contactPhone": t.contact_phone or "",
        "address": t.address or "",
        "country": t.country or "",
        "timezone": s.timezone,
        "defaultLanguages": s.default_languages or [],
        "branding": branding,
        "supportEmail": branding.get("supportEmail", ""),
        "supportPhone": branding.get("supportPhone", ""),
        "workingHours": s.business_hours or {},
        # Read-only (Super Admin controlled)
        "dataRegion": t.region or "",
        "dataRegionName": region.name if region else (t.region or ""),
        "dataRegionInfrastructureReady": region.infrastructure_ready if region else False,
        "plan": plan.code if plan else "",
        "planName": plan.name if plan else "",
        "subscriptionStatus": sub.status if sub else "",
        "status": t.status,
        "aiProfileCode": t.ai_profile_code or "",
    })


class TenantProfileRequest(BaseModel):
    """Tenant-admin editable fields ONLY. extra='forbid' means a crafted
    payload containing plan/code/region/status fields is rejected outright."""

    display_name: str | None = Field(default=None, alias="displayName", max_length=200)
    website: str | None = Field(default=None, max_length=300)
    contact_name: str | None = Field(default=None, alias="contactName", max_length=150)
    contact_email: EmailStr | None = Field(default=None, alias="contactEmail")
    contact_phone: str | None = Field(default=None, alias="contactPhone", max_length=30)
    address: str | None = Field(default=None, max_length=500)
    country: str | None = Field(default=None, max_length=100)
    timezone: str | None = Field(default=None, max_length=64)
    default_languages: list[str] | None = Field(default=None, alias="defaultLanguages")
    branding: dict | None = None
    support_email: EmailStr | None = Field(default=None, alias="supportEmail")
    support_phone: str | None = Field(default=None, alias="supportPhone", max_length=30)
    working_hours: dict | None = Field(default=None, alias="workingHours")

    model_config = {"populate_by_name": True, "extra": "forbid"}


@router.put("/tenant/profile")
def update_tenant_profile(
    body: TenantProfileRequest,
    request: Request,
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(require_permission("edit_tenant_profile", "tenants.manage")),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    t = db.get(Tenant, tid)
    if t is None or t.is_deleted:
        raise NotFoundError("Tenant")
    s = _get_or_create_settings(db, tid, user)

    before = {"website": t.website, "contactEmail": t.contact_email,
              "displayName": s.display_name, "timezone": s.timezone}
    for field in ("website", "contact_name", "contact_phone", "address", "country"):
        val = getattr(body, field)
        if val is not None:
            setattr(t, field, val)
    if body.contact_email is not None:
        t.contact_email = body.contact_email.lower()
    if body.display_name is not None:
        s.display_name = body.display_name.strip() or s.display_name
    if body.timezone is not None:
        s.timezone = body.timezone
    if body.default_languages is not None:
        languages, invalid = validate_language_assignment(
            db, body.default_languages, existing=s.default_languages
        )
        if not languages:
            raise ApiError("At least one tenant language is required.", 422)
        if invalid:
            raise ApiError(
                f"Unknown or inactive language(s): {', '.join(invalid)}", 422
            )
        s.default_languages = languages
    branding = dict(s.branding or {})
    if body.branding is not None:
        branding.update(body.branding)
    if body.support_email is not None:
        branding["supportEmail"] = body.support_email.lower()
    if body.support_phone is not None:
        branding["supportPhone"] = body.support_phone
    s.branding = branding
    if body.working_hours is not None:
        s.business_hours = body.working_hours
    t.updated_by = user.id
    s.updated_by = user.id
    record_audit(
        db, user=user, action="Updated tenant profile", entity_type="tenant",
        entity_id=t.id, target_label=t.name, tenant_id=tid,
        previous_value=before,
        new_value={"website": t.website, "contactEmail": t.contact_email,
                   "displayName": s.display_name, "timezone": s.timezone},
        request=request,
    )
    db.commit()
    return get_tenant_profile(tenant_id=tenant_id, user=user, db=db)


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
    if body.default_languages is not None:
        languages, invalid = validate_language_assignment(
            db, body.default_languages, existing=s.default_languages
        )
        if not languages:
            raise ApiError("At least one tenant language is required.", 422)
        if invalid:
            raise ApiError(
                f"Unknown or inactive language(s): {', '.join(invalid)}", 422
            )
        body.default_languages = languages
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
