"""Super Admin master-data management: industries, data regions, plans,
AI configuration profiles, providers, supported languages and voice profiles.

Every mutation is permission-guarded server-side, audited, and reference-safe:
records used by tenants/bots/subscriptions can be deactivated or archived but
never permanently deleted.
"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import get_current_user, require_permission
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from shared.db.mysql import get_db
from shared.models import (
    AiConfigProfile,
    AuditLog,
    BotLanguage,
    DataRegion,
    Industry,
    Plan,
    ProviderDef,
    Subscription,
    SupportedLanguage,
    Tenant,
    User,
    VoiceBot,
    VoiceBotSetting,
    VoiceProfile,
)
from backend.serializers import (
    serialize_ai_profile,
    serialize_data_region,
    serialize_industry,
    serialize_language,
    serialize_plan,
    serialize_provider,
    serialize_tenant,
    serialize_voice,
)

router = APIRouter(tags=["Master Data"])


# ── Usage counting (reference protection) ────────────────────────────────────


def _industry_usage(db: Session, row: Industry) -> int:
    return db.scalar(
        select(func.count()).select_from(Tenant).where(
            or_(Tenant.industry == row.code, Tenant.industry == row.name),
            Tenant.is_deleted.is_(False),
        )
    ) or 0


def _region_usage(db: Session, row: DataRegion) -> int:
    return db.scalar(
        select(func.count()).select_from(Tenant).where(
            or_(Tenant.region == row.code, Tenant.region == row.name),
            Tenant.is_deleted.is_(False),
        )
    ) or 0


def _plan_usage(db: Session, row: Plan) -> int:
    return db.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.plan_id == row.id, Subscription.is_deleted.is_(False)
        )
    ) or 0


def _ai_profile_usage(db: Session, row: AiConfigProfile) -> int:
    return db.scalar(
        select(func.count()).select_from(Tenant).where(
            Tenant.ai_profile_code == row.code, Tenant.is_deleted.is_(False)
        )
    ) or 0


def _provider_usage(db: Session, row: ProviderDef) -> int:
    profiles = db.scalar(
        select(func.count()).select_from(AiConfigProfile).where(
            AiConfigProfile.is_deleted.is_(False),
            or_(
                AiConfigProfile.stt_provider == row.code,
                AiConfigProfile.tts_provider == row.code,
                AiConfigProfile.llm_provider == row.code,
                AiConfigProfile.embedding_provider == row.code,
            ),
        )
    ) or 0
    bots = db.scalar(
        select(func.count()).select_from(VoiceBotSetting).where(
            or_(
                VoiceBotSetting.stt_provider == row.code,
                VoiceBotSetting.tts_provider == row.code,
                VoiceBotSetting.llm_provider == row.code,
            )
        )
    ) or 0
    return profiles + bots


def _language_usage(db: Session, row: SupportedLanguage) -> int:
    return db.scalar(
        select(func.count()).select_from(BotLanguage).where(
            BotLanguage.language_code == row.code
        )
    ) or 0


def _voice_usage(db: Session, row: VoiceProfile) -> int:
    bots = db.scalar(
        select(func.count()).select_from(VoiceBot).where(
            VoiceBot.voice_id == row.id, VoiceBot.is_deleted.is_(False)
        )
    ) or 0
    settings = db.scalar(
        select(func.count()).select_from(VoiceBotSetting).where(
            VoiceBotSetting.voice_id == row.id
        )
    ) or 0
    return bots + settings


# ── Type registry ─────────────────────────────────────────────────────────────

_EDITABLE = {
    "industries": {
        "name", "description", "icon", "sort_order",
        "default_prompt_template_id", "default_guardrail_profile_id",
        "default_workflow_template_id",
    },
    "data-regions": {
        "name", "description", "country", "region", "cloud_provider",
        "storage_region", "database_region", "recording_region",
        "transcript_region", "infrastructure_ready", "sort_order",
    },
    "plans": {
        "name", "description", "price_monthly", "price_annual", "currency",
        "bot_limit", "minutes_included", "seats_included", "kb_limit",
        "storage_gb_included", "languages_included", "concurrent_call_limit",
        "monthly_call_limit", "monthly_token_limit", "monthly_embedding_limit",
        "recording_retention_days", "transcript_retention_days",
        "analytics_retention_days", "features", "overage_rates", "is_public",
        "is_recommended", "sort_order",
    },
    "ai-profiles": {
        "name", "description", "stt_provider", "stt_model", "llm_provider",
        "llm_model", "tts_provider", "tts_model", "default_voice",
        "embedding_provider", "embedding_model", "embedding_dimension",
        "reranking_model", "retrieval_top_k", "retrieval_threshold",
        "temperature", "max_output_tokens", "response_timeout_ms",
        "fallback_providers", "cost_category", "sort_order",
    },
    "providers": {
        "name", "description", "website", "requires_api_key", "secret_ref",
        "config", "sort_order",
    },
    "languages": {
        "name", "native_name", "iso_code", "script", "direction",
        "provider_support", "is_default", "sort_order",
    },
    "voices": {
        "name", "gender", "languages", "locale", "accent", "styles",
        "description", "latency_ms", "premium", "sample_text", "provider",
        "provider_voice_id", "speaking_rate", "pitch", "is_default",
        "sort_order",
    },
}

_TYPES: dict[str, dict] = {
    "industries": dict(
        model=Industry, serializer=serialize_industry, usage=_industry_usage,
        perm="manage_industries", label="Industry", search=("code", "name", "description"),
    ),
    "data-regions": dict(
        model=DataRegion, serializer=serialize_data_region, usage=_region_usage,
        perm="manage_data_regions", label="Data Region", search=("code", "name", "country"),
    ),
    "plans": dict(
        model=Plan, serializer=serialize_plan, usage=_plan_usage,
        perm="manage_plans", label="Plan", search=("code", "name", "description"),
    ),
    "ai-profiles": dict(
        model=AiConfigProfile, serializer=serialize_ai_profile, usage=_ai_profile_usage,
        perm="manage_ai_profiles", label="AI Configuration Profile",
        search=("code", "name", "description"),
    ),
    "providers": dict(
        model=ProviderDef, serializer=serialize_provider, usage=_provider_usage,
        perm="manage_master_data", label="Provider", search=("code", "name", "kind"),
    ),
    "languages": dict(
        model=SupportedLanguage, serializer=serialize_language, usage=_language_usage,
        perm="manage_languages", label="Language", search=("code", "name", "native_name"),
    ),
    "voices": dict(
        model=VoiceProfile, serializer=serialize_voice, usage=_voice_usage,
        perm="manage_master_data", label="Voice", search=("name", "provider", "accent"),
    ),
}

# Languages/voices don't share the exact same lifecycle columns.
_STATUSLESS = {"languages"}  # uses `enabled` instead of `status`


def _spec(mtype: str) -> dict:
    spec = _TYPES.get(mtype)
    if spec is None:
        raise NotFoundError("Master data type")
    return spec


def _guard(mtype: str, user: User) -> None:
    from backend.core.deps import has_permission

    spec = _spec(mtype)
    if not (has_permission(user, spec["perm"]) or has_permission(user, "manage_master_data")):
        from shared.errors import ForbiddenError

        raise ForbiddenError()


def _serialize(db: Session, mtype: str, row, names: dict | None = None) -> dict:
    spec = _spec(mtype)
    usage = spec["usage"](db, row)
    if mtype in ("languages", "voices"):
        return spec["serializer"](row, usage=usage)
    return spec["serializer"](row, usage=usage, names=names)


def _user_names(db: Session, rows: list) -> dict[str, str]:
    ids = {r.created_by for r in rows if getattr(r, "created_by", None)} | {
        r.updated_by for r in rows if getattr(r, "updated_by", None)
    }
    if not ids:
        return {}
    return dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all())


# ── List ──────────────────────────────────────────────────────────────────────


@router.get("/master/{mtype}")
def list_master(
    mtype: str,
    kind: str | None = Query(None),  # providers only
    include_inactive: bool = Query(True, alias="includeInactive"),
    params: PageParams = Depends(page_params),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    model = spec["model"]

    stmt = select(model)
    if hasattr(model, "is_deleted"):
        stmt = stmt.where(model.is_deleted.is_(False))
    if mtype == "providers" and kind:
        stmt = stmt.where(ProviderDef.kind == kind)
    if not include_inactive:
        if mtype in _STATUSLESS:
            stmt = stmt.where(model.enabled.is_(True))
        else:
            stmt = stmt.where(model.status == "active")
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(or_(*[getattr(model, f).like(like) for f in spec["search"]]))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    sortable = {
        "name": getattr(model, "name", None),
        "code": getattr(model, "code", None),
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
        "sortOrder": getattr(model, "sort_order", None),
    }
    col = sortable.get(params.sort_by or "") or getattr(model, "sort_order", model.created_at)
    stmt = stmt.order_by(col.desc() if params.sort_dir == "desc" else col.asc())

    rows = db.scalars(stmt.offset(params.offset).limit(params.page_size)).all()
    names = _user_names(db, rows)
    return paginated(
        [_serialize(db, mtype, r, names) for r in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


# ── Create / update ───────────────────────────────────────────────────────────


class MasterPayload(BaseModel):
    """Free-shape payload — validated per type against the editable-field set."""

    model_config = {"extra": "allow"}

    code: str | None = Field(default=None, max_length=50)
    name: str | None = Field(default=None, max_length=150)
    kind: str | None = None  # providers only


def _camel_to_snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _apply_fields(row, payload: dict, allowed: set[str]) -> dict:
    changed = {}
    for key, value in payload.items():
        snake = _camel_to_snake(key)
        if snake in allowed and value is not None:
            if getattr(row, snake, None) != value:
                changed[snake] = value
                setattr(row, snake, value)
    return changed


_ID_PREFIX = {
    "industries": "ind", "data-regions": "dr", "plans": "pl",
    "ai-profiles": "aip", "providers": "prov", "languages": "lang", "voices": "vp",
}


@router.post("/master/{mtype}", status_code=201)
def create_master(
    mtype: str,
    body: MasterPayload,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    model = spec["model"]
    payload = body.model_dump(exclude_unset=True)

    name = (payload.get("name") or "").strip()
    if not name:
        raise ApiError("Name is required.", 422, errors=[{"field": "name", "message": "Name is required."}])

    row = model(id=new_id(_ID_PREFIX[mtype]))
    if hasattr(model, "code"):
        code = (payload.get("code") or "").strip().replace(" ", "_")
        if mtype == "languages":
            # Locale codes keep their case (en-IN); column is 15 chars.
            if len(code) > 15:
                raise ApiError("Language code must be at most 15 characters.", 422)
        else:
            code = code.lower()
        if mtype != "voices":
            if not code:
                raise ApiError("Code is required.", 422, errors=[{"field": "code", "message": "Code is required."}])
            dup_stmt = select(model).where(model.code == code)
            if mtype == "providers":
                p_kind = payload.get("kind")
                if p_kind not in ("voice", "stt", "tts", "llm", "embedding"):
                    raise ApiError("kind must be one of voice, stt, tts, llm, embedding.", 422)
                dup_stmt = dup_stmt.where(ProviderDef.kind == p_kind)
                row.kind = p_kind
            if db.scalar(dup_stmt) is not None:
                raise ApiError(
                    f"A {spec['label'].lower()} with code '{code}' already exists.", 409
                )
            row.code = code
    row.name = name
    _apply_fields(row, payload, _EDITABLE[mtype])
    if hasattr(row, "created_by"):
        row.created_by = user.id
    db.add(row)
    record_audit(
        db, user=user, action=f"Created {spec['label'].lower()}",
        entity_type=f"master:{mtype}", entity_id=row.id, target_label=name,
        new_value={"name": name, "code": getattr(row, "code", None)}, request=request,
    )
    db.commit()
    return ok(_serialize(db, mtype, row))


def _get_row(db: Session, mtype: str, item_id: str):
    model = _spec(mtype)["model"]
    row = db.get(model, item_id)
    if row is None or (hasattr(row, "is_deleted") and row.is_deleted):
        raise NotFoundError(_spec(mtype)["label"])
    return row


@router.patch("/master/{mtype}/{item_id}")
def update_master(
    mtype: str,
    item_id: str,
    body: MasterPayload,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    row = _get_row(db, mtype, item_id)
    payload = body.model_dump(exclude_unset=True)
    changed = _apply_fields(row, payload, _EDITABLE[mtype])
    if changed.get("is_default") and mtype in ("voices", "languages"):
        # Exactly one platform default at a time.
        model = spec["model"]
        for other in db.scalars(select(model).where(model.is_default.is_(True))).all():
            if other.id != row.id:
                other.is_default = False
    if hasattr(row, "updated_by"):
        row.updated_by = user.id
    if changed:
        record_audit(
            db, user=user, action=f"Updated {spec['label'].lower()}",
            entity_type=f"master:{mtype}", entity_id=row.id,
            target_label=getattr(row, "name", row.id),
            new_value={k: v for k, v in changed.items() if not isinstance(v, (dict, list))} or {"fields": list(changed)},
            request=request,
        )
    db.commit()
    return ok(_serialize(db, mtype, row))


# ── Lifecycle: activate / deactivate / archive ────────────────────────────────


class StatusRequest(BaseModel):
    status: str = Field(pattern="^(active|inactive|archived)$")


@router.post("/master/{mtype}/{item_id}/status")
def set_master_status(
    mtype: str,
    item_id: str,
    body: StatusRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    row = _get_row(db, mtype, item_id)
    if mtype in _STATUSLESS:
        before = "active" if row.enabled else "inactive"
        row.enabled = body.status == "active"
    else:
        before = row.status
        row.status = body.status
    if hasattr(row, "updated_by"):
        row.updated_by = user.id
    action = {"active": "Activated", "inactive": "Deactivated", "archived": "Archived"}[body.status]
    record_audit(
        db, user=user, action=f"{action} {spec['label'].lower()}",
        entity_type=f"master:{mtype}", entity_id=row.id,
        target_label=getattr(row, "name", row.id),
        previous_value={"status": before}, new_value={"status": body.status},
        request=request,
    )
    db.commit()
    return ok(_serialize(db, mtype, row))


@router.delete("/master/{mtype}/{item_id}")
def delete_master(
    mtype: str,
    item_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    row = _get_row(db, mtype, item_id)
    usage = spec["usage"](db, row)
    if usage > 0:
        raise ApiError(
            f"This {spec['label'].lower()} is used by {usage} existing "
            f"record{'s' if usage != 1 else ''} and cannot be deleted. "
            "Deactivate or archive it instead.",
            409,
        )
    # Unreferenced → archive (soft delete). Hard removal stays disabled.
    if mtype in _STATUSLESS:
        row.enabled = False
    if hasattr(row, "is_deleted"):
        from backend.core.softdelete import soft_delete

        soft_delete(row, user)
    elif hasattr(row, "status"):
        row.status = "archived"
    record_audit(
        db, user=user, action=f"Archived {spec['label'].lower()}",
        entity_type=f"master:{mtype}", entity_id=row.id,
        target_label=getattr(row, "name", row.id), request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})


# ── Audit trail per record ────────────────────────────────────────────────────


@router.get("/master/{mtype}/{item_id}/audit")
def master_audit(
    mtype: str,
    item_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == f"master:{mtype}", AuditLog.entity_id == item_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
    ).all()
    return ok([
        {
            "id": a.id, "actor": a.actor_name or "System", "action": a.action,
            "previousValue": a.previous_value, "newValue": a.new_value,
            "time": a.created_at.isoformat() + "Z",
        }
        for a in rows
    ])


# ── Plan extras: duplicate + tenants using ────────────────────────────────────


@router.post("/master/plans/{item_id}/duplicate", status_code=201)
def duplicate_plan(
    item_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_plans", "manage_master_data")),
    db: Session = Depends(get_db),
):
    src = _get_row(db, "plans", item_id)
    base_code = f"{src.code}_copy"
    code, n = base_code, 2
    while db.scalar(select(Plan).where(Plan.code == code)) is not None:
        code, n = f"{base_code}{n}", n + 1
    clone = Plan(id=new_id("pl"), code=code, name=f"{src.name} (copy)", status="inactive",
                 created_by=user.id)
    for field in _EDITABLE["plans"] - {"name"}:
        setattr(clone, field, getattr(src, field))
    clone.is_recommended = False
    db.add(clone)
    record_audit(
        db, user=user, action="Duplicated plan", entity_type="master:plans",
        entity_id=clone.id, target_label=clone.name,
        new_value={"from": src.code, "code": code}, request=request,
    )
    db.commit()
    return ok(_serialize(db, "plans", clone))


@router.get("/master/plans/{item_id}/tenants")
def plan_tenants(
    item_id: str,
    user: User = Depends(require_permission("manage_plans", "manage_master_data")),
    db: Session = Depends(get_db),
):
    plan = _get_row(db, "plans", item_id)
    rows = db.execute(
        select(Tenant.id, Tenant.name, Tenant.domain, Subscription.status, Subscription.mrr)
        .join(Subscription, Subscription.tenant_id == Tenant.id)
        .where(Subscription.plan_id == plan.id, Subscription.is_deleted.is_(False),
               Tenant.is_deleted.is_(False))
        .order_by(Tenant.name)
    ).all()
    return ok([
        {"id": r[0], "name": r[1], "domain": r[2], "subscriptionStatus": r[3], "mrr": float(r[4])}
        for r in rows
    ])


# ── Onboarding options (DB-driven, active values only) ────────────────────────


@router.get("/onboarding/options")
def onboarding_options(
    user: User = Depends(require_permission("tenants.manage", "manage_master_data")),
    db: Session = Depends(get_db),
):
    industries = db.scalars(
        select(Industry).where(Industry.is_deleted.is_(False), Industry.status == "active")
        .order_by(Industry.sort_order)
    ).all()
    regions = db.scalars(
        select(DataRegion).where(DataRegion.is_deleted.is_(False), DataRegion.status == "active")
        .order_by(DataRegion.sort_order)
    ).all()
    plans = db.scalars(
        select(Plan).where(Plan.is_deleted.is_(False), Plan.status == "active",
                           Plan.is_public.is_(True))
        .order_by(Plan.sort_order)
    ).all()
    profiles = db.scalars(
        select(AiConfigProfile).where(
            AiConfigProfile.is_deleted.is_(False), AiConfigProfile.status == "active"
        ).order_by(AiConfigProfile.sort_order)
    ).all()
    languages = db.scalars(
        select(SupportedLanguage).where(SupportedLanguage.enabled.is_(True))
        .order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
    ).all()
    return ok({
        "industries": [{"code": i.code, "name": i.name, "icon": i.icon or ""} for i in industries],
        "dataRegions": [
            {"code": r.code, "name": r.name, "infrastructureReady": r.infrastructure_ready}
            for r in regions
        ],
        "plans": [
            {"code": p.code, "name": p.name, "description": p.description or "",
             "priceMonthly": float(p.price_monthly), "minutesIncluded": p.minutes_included,
             "botLimit": p.bot_limit, "seatsIncluded": p.seats_included,
             "isRecommended": p.is_recommended}
            for p in plans
        ],
        "aiProfiles": [
            {"code": a.code, "name": a.name, "description": a.description or "",
             "costCategory": a.cost_category}
            for a in profiles
        ],
        "languages": [
            {"code": l.code, "name": l.name, "nativeName": l.native_name or "",
             "direction": l.direction}
            for l in languages
        ],
    })
