"""Intents and entity definitions: full CRUD, duplication, activation and
test consoles. All mutations are permission-enforced and audited."""

import re

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.db.mysql import get_db
from shared.models import ApiConnection, EntityDef, Intent, User, VoiceBot, Workflow
from shared.bot_config import invalidate_bot_config_sync
from shared.orchestration.entity_extractor import extract_entities, extract_entity
from backend.serializers import serialize_entity, serialize_intent

router = APIRouter(tags=["Intents & Entities"])

_ENTITY_KINDS = "^(system|custom|regex|api)$"
_DATA_TYPES = (
    "text|number|integer|decimal|date|date_range|time|duration|currency|"
    "percentage|phone|email|account_number|policy_number|claim_number|"
    "card_last4|person_name|location|product|list|regex|api"
)
# Prohibited authentication secrets must never be collected as entities.
# Loose separators on purpose: "otp_code", "card-pin", "passwordField" all match.
_FORBIDDEN_ENTITY_NAMES = re.compile(
    r"(cvv|cvc|otp|password|passcode|(card|atm)[\s_-]?pin|one[\s_-]?time[\s_-]?pass)", re.I
)


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _normalize_phrase(phrase: str) -> str:
    return re.sub(r"\s+", " ", phrase.strip().lower())


def _validate_samples(samples: list[str]) -> list[str]:
    cleaned, seen = [], set()
    for phrase in samples:
        phrase = phrase.strip()
        if not phrase:
            continue
        norm = _normalize_phrase(phrase)
        if norm in seen:
            raise ApiError(
                f"Duplicate training phrase: '{phrase}'. Each phrase must be unique.",
                422, errors=[{"field": "samples", "message": f"Duplicate phrase: {phrase}"}],
            )
        seen.add(norm)
        cleaned.append(phrase)
    return cleaned


def _validate_intent_refs(db: Session, tenant_id: str, bot_id: str, *,
                          entities: list[str] | None, optional_entities: list[str] | None,
                          workflow_id: str | None, api_connection_id: str | None,
                          kb_ids: list[str] | None) -> None:
    wanted = set(entities or []) | set(optional_entities or [])
    if wanted:
        existing = set(db.scalars(
            select(EntityDef.name).where(
                EntityDef.tenant_id == tenant_id, EntityDef.is_deleted.is_(False)
            )
        ).all())
        missing = wanted - existing
        if missing:
            raise ApiError(
                f"Unknown entities: {', '.join(sorted(missing))}. Create them first.",
                422, errors=[{"field": "entities", "message": "Unknown entity reference."}],
            )
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None or wf.is_deleted or wf.tenant_id != tenant_id:
            raise ApiError("The referenced workflow does not exist in this workspace.", 422)
    if api_connection_id:
        conn = db.get(ApiConnection, api_connection_id)
        if conn is None or conn.is_deleted or conn.tenant_id != tenant_id:
            raise ApiError("The referenced API connection does not exist in this workspace.", 422)
    if kb_ids:
        from shared.models import KnowledgeSource

        for kb_id in kb_ids:
            kb = db.get(KnowledgeSource, kb_id)
            if kb is None or kb.is_deleted or (kb.scope != "global" and kb.tenant_id != tenant_id):
                raise ApiError("A referenced knowledge base does not exist in this workspace.", 422)


@router.get("/bots/{bot_id}/intents")
def list_intents(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(Intent)
        .where(Intent.bot_id == bot.id, Intent.is_deleted.is_(False))
        .order_by(Intent.priority.asc(), Intent.created_at.asc())
    ).all()
    return ok([serialize_intent(i) for i in rows])


class IntentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    code: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=500)
    samples: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    confidence_threshold: float = Field(default=0.7, alias="confidenceThreshold", ge=0, le=1)
    route: str = Field(default="", max_length=200)
    entities: list[str] = Field(default_factory=list)
    optional_entities: list[str] = Field(default_factory=list, alias="optionalEntities")
    workflow_id: str | None = Field(default=None, alias="workflowId")
    api_connection_id: str | None = Field(default=None, alias="apiConnectionId")
    kb_ids: list[str] = Field(default_factory=list, alias="kbIds")
    priority: int = Field(default=100, ge=0, le=1000)
    fallback_behavior: str | None = Field(default=None, alias="fallbackBehavior",
                                          pattern="^(clarify|handoff|llm)$")
    handoff_enabled: bool = Field(default=False, alias="handoffEnabled")
    status: str = Field(default="active", pattern="^(active|needs_samples|disabled)$")

    model_config = {"populate_by_name": True}


@router.post("/bots/{bot_id}/intents", status_code=201)
def create_intent(
    bot_id: str,
    body: IntentRequest,
    request: Request,
    user: User = Depends(require_permission("manage_intents", "bots.manage")),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    name = body.name.strip()
    if db.scalar(select(Intent).where(
        Intent.bot_id == bot.id, Intent.name == name, Intent.is_deleted.is_(False)
    )):
        raise ApiError("An intent with this name already exists on this bot.", 409)
    code = (body.code or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"))[:80]
    if db.scalar(select(Intent).where(
        Intent.bot_id == bot.id, Intent.code == code, Intent.is_deleted.is_(False)
    )):
        raise ApiError(f"An intent with code '{code}' already exists on this bot.", 409)
    samples = _validate_samples(body.samples)
    _validate_intent_refs(
        db, bot.tenant_id, bot.id, entities=body.entities,
        optional_entities=body.optional_entities, workflow_id=body.workflow_id,
        api_connection_id=body.api_connection_id, kb_ids=body.kb_ids,
    )
    row = Intent(
        id=new_id("in"), tenant_id=bot.tenant_id, bot_id=bot.id, name=name, code=code,
        category=body.category, description=body.description, samples=samples,
        languages=body.languages, confidence_threshold=body.confidence_threshold,
        route=body.route, entities=body.entities, optional_entities=body.optional_entities,
        workflow_id=body.workflow_id, api_connection_id=body.api_connection_id,
        kb_ids=body.kb_ids, priority=body.priority,
        fallback_behavior=body.fallback_behavior, handoff_enabled=body.handoff_enabled,
        status="needs_samples" if len(samples) < 3 else body.status,
        version=1, created_by=user.id,
    )
    db.add(row)
    _sync_entity_usage(db, bot.tenant_id)
    record_audit(
        db, user=user, action="Created intent", entity_type="intent", entity_id=row.id,
        target_label=f"{row.name} ({bot.name})", tenant_id=bot.tenant_id,
        new_value={"name": row.name, "code": code}, request=request,
    )
    db.commit()
    invalidate_bot_config_sync(bot.tenant_id, bot.id)
    return ok(serialize_intent(row))


def _sync_entity_usage(db: Session, tenant_id: str) -> None:
    """Refresh EntityDef.used_by from live intents (best-effort denorm)."""
    db.flush()
    intents = db.scalars(
        select(Intent).where(Intent.tenant_id == tenant_id, Intent.is_deleted.is_(False))
    ).all()
    usage: dict[str, list[str]] = {}
    for intent in intents:
        for ent in (intent.entities or []) + (intent.optional_entities or []):
            usage.setdefault(ent, []).append(intent.name)
    for entity in db.scalars(
        select(EntityDef).where(EntityDef.tenant_id == tenant_id, EntityDef.is_deleted.is_(False))
    ).all():
        entity.used_by = sorted(set(usage.get(entity.name, [])))


class UpdateIntentRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    code: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    samples: list[str] | None = None
    languages: list[str] | None = None
    confidence_threshold: float | None = Field(
        default=None, alias="confidenceThreshold", ge=0, le=1
    )
    route: str | None = Field(default=None, max_length=200)
    entities: list[str] | None = None
    optional_entities: list[str] | None = Field(default=None, alias="optionalEntities")
    workflow_id: str | None = Field(default=None, alias="workflowId")
    api_connection_id: str | None = Field(default=None, alias="apiConnectionId")
    kb_ids: list[str] | None = Field(default=None, alias="kbIds")
    priority: int | None = Field(default=None, ge=0, le=1000)
    fallback_behavior: str | None = Field(default=None, alias="fallbackBehavior",
                                          pattern="^(clarify|handoff|llm)$")
    handoff_enabled: bool | None = Field(default=None, alias="handoffEnabled")
    status: str | None = Field(default=None, pattern="^(active|needs_samples|disabled|archived)$")

    model_config = {"populate_by_name": True}


def _intent_checked(db: Session, intent_id: str, user: User) -> Intent:
    row = db.get(Intent, intent_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Intent")
    assert_tenant_access(user, row.tenant_id)
    return row


@router.patch("/intents/{intent_id}")
def update_intent(
    intent_id: str,
    body: UpdateIntentRequest,
    request: Request,
    user: User = Depends(require_permission("manage_intents", "bots.manage")),
    db: Session = Depends(get_db),
):
    row = _intent_checked(db, intent_id, user)
    before = {"samples": len(row.samples or []), "status": row.status}
    if body.samples is not None:
        body.samples = _validate_samples(body.samples)
    _validate_intent_refs(
        db, row.tenant_id, row.bot_id,
        entities=body.entities if body.entities is not None else row.entities,
        optional_entities=body.optional_entities if body.optional_entities is not None else row.optional_entities,
        workflow_id=body.workflow_id, api_connection_id=body.api_connection_id,
        kb_ids=body.kb_ids,
    )
    if body.name and body.name.strip() != row.name:
        if db.scalar(select(Intent).where(
            Intent.bot_id == row.bot_id, Intent.name == body.name.strip(),
            Intent.is_deleted.is_(False), Intent.id != row.id,
        )):
            raise ApiError("An intent with this name already exists on this bot.", 409)
        row.name = body.name.strip()
    changed = False
    for field in ("code", "category", "description", "samples", "languages",
                  "confidence_threshold", "route", "entities", "optional_entities",
                  "workflow_id", "api_connection_id", "kb_ids", "priority",
                  "fallback_behavior", "handoff_enabled", "status"):
        val = getattr(body, field)
        if val is not None:
            setattr(row, field, val)
            changed = True
    if changed:
        row.version += 1
        row.updated_by = user.id
    _sync_entity_usage(db, row.tenant_id)
    record_audit(
        db, user=user, action="Updated intent", entity_type="intent", entity_id=row.id,
        target_label=row.name, tenant_id=row.tenant_id, previous_value=before,
        new_value={"samples": len(row.samples or []), "status": row.status},
        request=request,
    )
    db.commit()
    invalidate_bot_config_sync(row.tenant_id, row.bot_id)
    return ok(serialize_intent(row))


@router.post("/intents/{intent_id}/duplicate", status_code=201)
def duplicate_intent(
    intent_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_intents", "bots.manage")),
    db: Session = Depends(get_db),
):
    src = _intent_checked(db, intent_id, user)
    base, name, n = f"{src.name} (copy)", f"{src.name} (copy)", 2
    while db.scalar(select(Intent).where(
        Intent.bot_id == src.bot_id, Intent.name == name, Intent.is_deleted.is_(False)
    )):
        name, n = f"{base} {n}", n + 1
    clone = Intent(
        id=new_id("in"), tenant_id=src.tenant_id, bot_id=src.bot_id, name=name,
        code=f"{src.code}_copy" if src.code else None, category=src.category,
        description=src.description, samples=src.samples, languages=src.languages,
        confidence_threshold=src.confidence_threshold, route=src.route,
        entities=src.entities, optional_entities=src.optional_entities,
        workflow_id=src.workflow_id, api_connection_id=src.api_connection_id,
        kb_ids=src.kb_ids, priority=src.priority, fallback_behavior=src.fallback_behavior,
        handoff_enabled=src.handoff_enabled, status="disabled", version=1,
        created_by=user.id,
    )
    db.add(clone)
    record_audit(
        db, user=user, action="Duplicated intent", entity_type="intent",
        entity_id=clone.id, target_label=name, tenant_id=src.tenant_id,
        new_value={"from": src.id}, request=request,
    )
    db.commit()
    return ok(serialize_intent(clone))


@router.delete("/intents/{intent_id}")
def delete_intent(
    intent_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_permission("manage_intents", "bots.manage")),
    db: Session = Depends(get_db),
):
    row = _intent_checked(db, intent_id, user)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    _sync_entity_usage(db, row.tenant_id)
    record_audit(
        db, user=user, action="Archived intent", entity_type="intent", entity_id=row.id,
        target_label=row.name, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    invalidate_bot_config_sync(row.tenant_id, row.bot_id)
    return ok({"archived": True, "id": row.id})


class IntentTestRequest(BaseModel):
    utterance: str = Field(min_length=1, max_length=1000)
    language: str = Field(default="en-US", max_length=15)


@router.post("/bots/{bot_id}/intents/test")
def test_intents(
    bot_id: str,
    body: IntentTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Run one utterance through the real runtime router: matched intent,
    confidence, extracted entities, routing decision. Read-only."""
    from shared.models import KnowledgeSource
    from shared.orchestration.router import TurnRouter

    bot = _bot_checked(db, bot_id, user)
    intents = db.scalars(
        select(Intent).where(
            Intent.bot_id == bot.id, Intent.is_deleted.is_(False), Intent.status == "active"
        )
    ).all()
    has_kbs = bool(db.scalar(
        select(KnowledgeSource.id).where(
            KnowledgeSource.is_deleted.is_(False),
            KnowledgeSource.status.in_(("indexed", "stale")),
            ((KnowledgeSource.bot_id == bot.id)
             | ((KnowledgeSource.tenant_id == bot.tenant_id) & (KnowledgeSource.scope == "tenant"))
             | (KnowledgeSource.scope == "global")),
        ).limit(1)
    ))
    router_ = TurnRouter(
        intents=[{"name": i.name, "samples": i.samples or [], "route": i.route,
                  "confidence_threshold": i.confidence_threshold} for i in intents],
        has_knowledge_bases=has_kbs,
    )
    decision = router_.decide(body.utterance)

    matched = next((i for i in intents if i.name == decision.intent), None)
    extracted = []
    if matched is not None:
        wanted = set((matched.entities or []) + (matched.optional_entities or []))
        defs = db.scalars(
            select(EntityDef).where(
                EntityDef.tenant_id == bot.tenant_id,
                EntityDef.name.in_(wanted) if wanted else EntityDef.id.is_(None),
                EntityDef.is_deleted.is_(False),
            )
        ).all() if wanted else []
        extracted = extract_entities(body.utterance, [serialize_entity(e) for e in defs])

    return ok({
        "utterance": body.utterance,
        "language": body.language,
        "route": decision.kind.value,
        "action": decision.action,
        "matchedIntent": decision.intent,
        "confidence": round(decision.confidence, 3),
        "reason": decision.reason,
        "consideredKb": decision.considered_kb,
        "workflowId": matched.workflow_id if matched else None,
        "apiConnectionId": matched.api_connection_id if matched else None,
        "fallbackBehavior": (matched.fallback_behavior if matched else None) or "clarify",
        "entities": extracted,
    })


# ── Entities ─────────────────────────────────────────────────────────────────


@router.get("/entities")
def list_entities(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    rows = db.scalars(
        select(EntityDef)
        .where(EntityDef.tenant_id == tid, EntityDef.is_deleted.is_(False))
        .order_by(EntityDef.created_at.asc())
    ).all()
    return ok([serialize_entity(e) for e in rows])


class EntityRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    code: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=500)
    kind: str = Field(default="custom", pattern=_ENTITY_KINDS)
    data_type: str = Field(default="text", alias="dataType", pattern=f"^({_DATA_TYPES})$")
    languages: list[str] = Field(default_factory=list)
    synonyms: dict[str, list[str]] = Field(default_factory=dict)
    allowed_values: list[str] = Field(default_factory=list, alias="allowedValues")
    regex_pattern: str | None = Field(default=None, alias="regexPattern", max_length=500)
    validation_rules: dict = Field(default_factory=dict, alias="validationRules")
    normalization_rules: dict = Field(default_factory=dict, alias="normalizationRules")
    masking_enabled: bool = Field(default=False, alias="maskingEnabled")
    require_confirmation: bool = Field(default=False, alias="requireConfirmation")
    retention_days: int | None = Field(default=None, alias="retentionDays", ge=0, le=3650)
    example: str = Field(default="", max_length=300)
    pii: bool = False
    tenant_id: str | None = Field(default=None, alias="tenantId")

    model_config = {"populate_by_name": True}


def _validate_entity(body) -> None:
    if _FORBIDDEN_ENTITY_NAMES.search(body.name or ""):
        raise ApiError(
            "Authentication secrets (CVV, PIN, OTP, passwords) must never be "
            "collected through entities.",
            422, errors=[{"field": "name", "message": "Prohibited entity."}],
        )
    if body.regex_pattern:
        try:
            re.compile(body.regex_pattern)
        except re.error as exc:
            raise ApiError(
                f"Invalid regular expression: {exc.msg}.", 422,
                errors=[{"field": "regexPattern", "message": "Invalid regex."}],
            )
    if body.kind == "regex" and not body.regex_pattern:
        raise ApiError("Regex entities require a regexPattern.", 422,
                       errors=[{"field": "regexPattern", "message": "Required for regex entities."}])


@router.post("/entities", status_code=201)
def create_entity(
    body: EntityRequest,
    request: Request,
    user: User = Depends(require_permission("manage_entities", "bots.manage")),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, body.tenant_id)
    name = body.name.strip()
    _validate_entity(body)  # prohibited names/regex checked before anything else
    if db.scalar(select(EntityDef).where(
        EntityDef.tenant_id == tid, EntityDef.name == name, EntityDef.is_deleted.is_(False)
    )):
        raise ApiError("An entity with this name already exists.", 409)
    code = (body.code or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"))[:80]
    row = EntityDef(
        id=new_id("en"), tenant_id=tid, name=name, code=code,
        description=body.description, kind=body.kind, data_type=body.data_type,
        languages=body.languages, synonyms=body.synonyms,
        allowed_values=body.allowed_values, regex_pattern=body.regex_pattern,
        validation_rules=body.validation_rules, normalization_rules=body.normalization_rules,
        masking_enabled=body.masking_enabled or body.pii,
        require_confirmation=body.require_confirmation, retention_days=body.retention_days,
        example=body.example, pii=body.pii, status="active", used_by=[],
        created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created entity", entity_type="entity", entity_id=row.id,
        target_label=row.name, tenant_id=tid, new_value={"name": row.name, "pii": row.pii},
        request=request,
    )
    db.commit()
    return ok(serialize_entity(row))


class UpdateEntityRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    kind: str | None = Field(default=None, pattern=_ENTITY_KINDS)
    data_type: str | None = Field(default=None, alias="dataType", pattern=f"^({_DATA_TYPES})$")
    languages: list[str] | None = None
    synonyms: dict[str, list[str]] | None = None
    allowed_values: list[str] | None = Field(default=None, alias="allowedValues")
    regex_pattern: str | None = Field(default=None, alias="regexPattern", max_length=500)
    validation_rules: dict | None = Field(default=None, alias="validationRules")
    normalization_rules: dict | None = Field(default=None, alias="normalizationRules")
    masking_enabled: bool | None = Field(default=None, alias="maskingEnabled")
    require_confirmation: bool | None = Field(default=None, alias="requireConfirmation")
    retention_days: int | None = Field(default=None, alias="retentionDays", ge=0, le=3650)
    example: str | None = Field(default=None, max_length=300)
    pii: bool | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled|archived)$")

    model_config = {"populate_by_name": True}


def _entity_checked(db: Session, entity_id: str, user: User) -> EntityDef:
    row = db.get(EntityDef, entity_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Entity")
    assert_tenant_access(user, row.tenant_id)
    return row


@router.patch("/entities/{entity_id}")
def update_entity(
    entity_id: str,
    body: UpdateEntityRequest,
    request: Request,
    user: User = Depends(require_permission("manage_entities", "bots.manage")),
    db: Session = Depends(get_db),
):
    row = _entity_checked(db, entity_id, user)
    if body.name and body.name.strip() != row.name:
        if _FORBIDDEN_ENTITY_NAMES.search(body.name):
            raise ApiError("Authentication secrets must never be collected through entities.", 422)
        if db.scalar(select(EntityDef).where(
            EntityDef.tenant_id == row.tenant_id, EntityDef.name == body.name.strip(),
            EntityDef.is_deleted.is_(False), EntityDef.id != row.id,
        )):
            raise ApiError("An entity with this name already exists.", 409)
        row.name = body.name.strip()
    if body.regex_pattern:
        try:
            re.compile(body.regex_pattern)
        except re.error as exc:
            raise ApiError(f"Invalid regular expression: {exc.msg}.", 422)
    before = {"kind": row.kind, "dataType": row.data_type, "pii": row.pii, "status": row.status}
    changed = False
    for field in ("description", "kind", "data_type", "languages", "synonyms",
                  "allowed_values", "regex_pattern", "validation_rules",
                  "normalization_rules", "masking_enabled", "require_confirmation",
                  "retention_days", "example", "pii", "status"):
        val = getattr(body, field)
        if val is not None:
            setattr(row, field, val)
            changed = True
    if changed:
        row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated entity", entity_type="entity", entity_id=row.id,
        target_label=row.name, tenant_id=row.tenant_id, previous_value=before,
        new_value={"kind": row.kind, "dataType": row.data_type, "pii": row.pii,
                   "status": row.status},
        request=request,
    )
    db.commit()
    return ok(serialize_entity(row))


@router.post("/entities/{entity_id}/duplicate", status_code=201)
def duplicate_entity(
    entity_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_entities", "bots.manage")),
    db: Session = Depends(get_db),
):
    src = _entity_checked(db, entity_id, user)
    base, name, n = f"{src.name} (copy)", f"{src.name} (copy)", 2
    while db.scalar(select(EntityDef).where(
        EntityDef.tenant_id == src.tenant_id, EntityDef.name == name,
        EntityDef.is_deleted.is_(False),
    )):
        name, n = f"{base} {n}", n + 1
    clone = EntityDef(
        id=new_id("en"), tenant_id=src.tenant_id, name=name,
        code=f"{src.code}_copy" if src.code else None, description=src.description,
        kind=src.kind, data_type=src.data_type, languages=src.languages,
        synonyms=src.synonyms, allowed_values=src.allowed_values,
        regex_pattern=src.regex_pattern, validation_rules=src.validation_rules,
        normalization_rules=src.normalization_rules, masking_enabled=src.masking_enabled,
        require_confirmation=src.require_confirmation, retention_days=src.retention_days,
        example=src.example, pii=src.pii, status="disabled", used_by=[],
        created_by=user.id,
    )
    db.add(clone)
    record_audit(
        db, user=user, action="Duplicated entity", entity_type="entity",
        entity_id=clone.id, target_label=name, tenant_id=src.tenant_id,
        new_value={"from": src.id}, request=request,
    )
    db.commit()
    return ok(serialize_entity(clone))


@router.delete("/entities/{entity_id}")
def delete_entity(
    entity_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_permission("manage_entities", "bots.manage")),
    db: Session = Depends(get_db),
):
    row = _entity_checked(db, entity_id, user)
    if row.used_by:
        raise ApiError(
            f"This entity is used by {len(row.used_by)} intent(s): "
            f"{', '.join(row.used_by[:5])}. Remove it from those intents first, "
            "or disable it instead.",
            409,
        )
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="Archived entity", entity_type="entity", entity_id=row.id,
        target_label=row.name, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})


class EntityTestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.post("/entities/{entity_id}/test")
def test_entity(
    entity_id: str,
    body: EntityTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _entity_checked(db, entity_id, user)
    result = extract_entity(body.text, serialize_entity(row))
    return ok(result)
