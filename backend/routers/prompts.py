"""Prompts: versioned content, structured prompt builder, approval/publish
lifecycle, deterministic backend compilation and text-only prompt testing.

Lifecycle: draft → pending_approval → approved → published (rollback allowed).
State transitions are permission-enforced server-side:
  edit/save draft . manage_prompts
  approve/reject . approve_prompts
  publish/rollback . publish_prompts
"""

import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    has_permission,
    require_permission,
)
from shared.errors import ApiError, ForbiddenError, NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.db.mysql import get_db
from shared.models import (
    KnowledgeSource, Prompt, PromptVersion, SupportedLanguage, User, VoiceBot,
)
from shared.tenant_languages import tenant_allowed_language_codes
from shared.orchestration.prompt_compiler import (
    compile_prompt,
    compile_source,
    estimate_tokens,
    extract_variables,
    render_preview,
    validate_config,
)
from backend.serializers import serialize_prompt
from shared.readiness import refresh_readiness


def _refresh_prompt_readiness(db: Session, prompt: Prompt) -> None:
    bot = db.get(VoiceBot, prompt.bot_id)
    if bot is not None and not bot.is_deleted:
        refresh_readiness(db, bot, keys=("r3",))

router = APIRouter(tags=["Prompts"])

PROMPT_TYPES = "^(system|greeting|fallback|escalation|closing|reprompt|hold)$"
PROMPT_MODES_PATTERN = "^(structured|full)$"


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _prompt_checked(db: Session, prompt_id: str, user: User) -> Prompt:
    row = db.get(Prompt, prompt_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Prompt")
    assert_tenant_access(user, row.tenant_id)
    return row


def _compile_version_content(
    mode: str, config: dict | None, full_prompt: str | None
) -> tuple[dict | None, str | None, str | None]:
    """Validate + compile one authoring mode; raises ApiError on invalid.

    Returns (structured_config, full_prompt, compiled_prompt) as stored on the
    version row. Non-system prompt types pass (None, None) through untouched —
    their content lives in per-language variants.
    """
    if mode == "full":
        errors, compiled = compile_source("full", full_prompt=full_prompt)
        if errors:
            raise ApiError("The full prompt is invalid.", 422, errors=errors)
        return None, (full_prompt or "").strip(), compiled
    if config is None:
        return None, None, None
    errors, compiled = compile_source("structured", structured_config=config)
    if errors:
        raise ApiError("The prompt configuration is incomplete.", 422, errors=errors)
    return config, None, compiled


@router.get("/bots/{bot_id}/prompts")
def list_prompts(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(Prompt)
        .where(Prompt.bot_id == bot.id, Prompt.is_deleted.is_(False))
        .order_by(Prompt.created_at.asc())
    ).all()
    return ok([serialize_prompt(p) for p in rows])


class VariantPayload(BaseModel):
    language: str = Field(max_length=15)
    content: str = Field(max_length=4000)


def _validate_variant_languages(
    db: Session,
    tenant_id: str,
    variants: list[VariantPayload],
    *,
    previous_languages: set[str] | None = None,
) -> None:
    """Allow retaining/removing historical variants, but scope every new one.

    This keeps an old variant editable after its language is unassigned while
    ensuring “Add language” cannot bypass the tenant assignment via the API.
    """
    codes = [variant.language.strip() for variant in variants]
    if not codes:
        raise ApiError("At least one prompt language is required.", 422)
    if len(codes) != len(set(codes)):
        raise ApiError("A prompt can contain only one variant per language.", 422)

    allowed = tenant_allowed_language_codes(db, tenant_id)
    if allowed is None:
        allowed = set(db.scalars(
            select(SupportedLanguage.code)
            .where(SupportedLanguage.enabled.is_(True))
        ).all())
    newly_added = set(codes) - (previous_languages or set())
    rejected = sorted(newly_added - allowed)
    if rejected:
        raise ApiError(
            "Language(s) not assigned to this tenant or inactive: "
            + ", ".join(rejected),
            422,
        )
    for variant, code in zip(variants, codes):
        variant.language = code


class CreatePromptRequest(BaseModel):
    type: str = Field(pattern=PROMPT_TYPES)
    prompt_mode: str = Field(
        default="structured", alias="promptMode", pattern=PROMPT_MODES_PATTERN
    )
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    variables: list[str] = Field(default_factory=list)
    variants: list[VariantPayload] = Field(default_factory=list)
    structured_config: dict | None = Field(default=None, alias="structuredConfig")
    full_prompt: str | None = Field(default=None, alias="fullPrompt")
    note: str = Field(default="Initial version", max_length=500)

    model_config = {"populate_by_name": True}


@router.post("/bots/{bot_id}/prompts", status_code=201)
def create_prompt(
    bot_id: str,
    body: CreatePromptRequest,
    request: Request,
    user: User = Depends(require_permission("manage_prompts", "prompts.manage")),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    name = body.name.strip()
    if not name:
        raise ApiError("Prompt name is required.", 422,
                       errors=[{"field": "name", "message": "Name is required."}])
    duplicate = db.scalar(
        select(Prompt).where(
            Prompt.bot_id == bot.id, Prompt.name == name, Prompt.is_deleted.is_(False)
        )
    )
    if duplicate is not None:
        raise ApiError(f"A prompt named '{name}' already exists on this bot.", 409)

    if body.type != "system":
        _validate_variant_languages(db, bot.tenant_id, body.variants)

    config, full, compiled = _compile_version_content(
        body.prompt_mode, body.structured_config, body.full_prompt
    )
    # Variables the author listed plus every variable the prompt text uses —
    # the runtime-context screens read this to know what a prompt expects.
    variables = list(dict.fromkeys([*body.variables, *extract_variables(compiled)]))
    prompt = Prompt(
        id=new_id("pr"), tenant_id=bot.tenant_id, bot_id=bot.id, type=body.type,
        name=name, description=body.description, variables=variables,
        state="draft", active_version=1, created_by=user.id,
    )
    db.add(prompt)
    db.add(
        PromptVersion(
            id=new_id("prv"), prompt_id=prompt.id, version=1, edited_by=user.name,
            edited_by_user_id=user.id, edited_at=datetime.now(timezone.utc),
            note=body.note, variants=[v.model_dump() for v in body.variants],
            prompt_mode=body.prompt_mode, structured_config=config,
            full_prompt=full, compiled_prompt=compiled,
        )
    )
    refresh_readiness(db, bot, keys=("r3",))
    record_audit(
        db, user=user, action="Created prompt", entity_type="prompt", entity_id=prompt.id,
        target_label=f"{prompt.name} ({bot.name})", tenant_id=bot.tenant_id,
        new_value={"name": prompt.name, "type": prompt.type}, request=request,
    )
    db.commit()
    db.refresh(prompt)
    return ok(serialize_prompt(prompt))


class NewVersionRequest(BaseModel):
    note: str = Field(default="", max_length=500)
    prompt_mode: str | None = Field(
        default=None, alias="promptMode", pattern=PROMPT_MODES_PATTERN
    )
    variants: list[VariantPayload] = Field(default_factory=list)
    structured_config: dict | None = Field(default=None, alias="structuredConfig")
    full_prompt: str | None = Field(default=None, alias="fullPrompt")
    submit_for_approval: bool = Field(default=True, alias="submitForApproval")

    model_config = {"populate_by_name": True}


@router.post("/prompts/{prompt_id}/versions", status_code=201)
def add_prompt_version(
    prompt_id: str,
    body: NewVersionRequest,
    request: Request,
    user: User = Depends(require_permission("manage_prompts", "prompts.manage")),
    db: Session = Depends(get_db),
):
    prompt = _prompt_checked(db, prompt_id, user)
    if prompt.type != "system":
        previous = {
            str(variant.get("language") or "").strip()
            for variant in ((prompt.versions[0].variants or []) if prompt.versions else [])
            if variant.get("language")
        }
        _validate_variant_languages(
            db,
            prompt.tenant_id,
            body.variants,
            previous_languages=previous,
        )
    # Mode defaults to the payload shape, then the previous version's mode —
    # so existing clients that only ever send structuredConfig keep working.
    mode = body.prompt_mode or (
        "full" if body.full_prompt is not None
        else ("structured" if body.structured_config is not None
              else (prompt.versions[0].prompt_mode if prompt.versions else "structured"))
    )
    config, full, compiled = _compile_version_content(
        mode, body.structured_config, body.full_prompt
    )
    if compiled:
        prompt.variables = list(dict.fromkeys(
            [*(prompt.variables or []), *extract_variables(compiled)]
        ))
    latest = max((v.version for v in prompt.versions), default=0)
    db.add(
        PromptVersion(
            id=new_id("prv"), prompt_id=prompt.id, version=latest + 1,
            edited_by=user.name, edited_by_user_id=user.id,
            edited_at=datetime.now(timezone.utc), note=body.note,
            variants=[v.model_dump() for v in body.variants],
            prompt_mode=mode, structured_config=config,
            full_prompt=full, compiled_prompt=compiled,
        )
    )
    prompt.active_version = latest + 1
    prompt.state = "pending_approval" if body.submit_for_approval else "draft"
    prompt.updated_by = user.id
    _refresh_prompt_readiness(db, prompt)
    record_audit(
        db, user=user,
        action="Edited prompt (pending approval)" if body.submit_for_approval else "Saved prompt draft",
        entity_type="prompt", entity_id=prompt.id,
        target_label=f"{prompt.name} v{latest + 1}",
        tenant_id=prompt.tenant_id, new_value={"version": latest + 1, "note": body.note},
        request=request,
    )
    db.commit()
    db.refresh(prompt)
    return ok(serialize_prompt(prompt))


class CompilePreviewRequest(BaseModel):
    prompt_mode: str = Field(
        default="structured", alias="promptMode", pattern=PROMPT_MODES_PATTERN
    )
    structured_config: dict | None = Field(default=None, alias="structuredConfig")
    full_prompt: str | None = Field(default=None, alias="fullPrompt")
    # Optional sample runtime-context values: when present the response also
    # carries the rendered prompt + missing-variable warnings.
    test_context: dict | None = Field(default=None, alias="testContext")

    model_config = {"populate_by_name": True}


@router.post("/prompts/compile-preview")
def compile_preview(
    body: CompilePreviewRequest,
    user: User = Depends(get_current_user),
):
    """Stateless compile: preview + validation for the builder UI. The backend
    is the single compiler — the UI never assembles the runtime prompt."""
    errors, compiled = compile_source(
        body.prompt_mode,
        structured_config=body.structured_config or {},
        full_prompt=body.full_prompt,
    )
    result = {
        "compiled": compiled,
        "valid": not errors,
        "errors": errors,
        "characterCount": len(compiled),
        "tokenEstimate": estimate_tokens(compiled) if compiled else 0,
        "variables": extract_variables(compiled),
    }
    if body.test_context is not None and compiled:
        result["render"] = render_preview(compiled, body.test_context)
    return ok(result)


class RenderPreviewRequest(BaseModel):
    """Render a SAVED version against sample context (Prompt Studio preview)."""

    version: int | None = Field(default=None, ge=1)
    test_context: dict = Field(default_factory=dict, alias="testContext")

    model_config = {"populate_by_name": True}


@router.post("/prompts/{prompt_id}/render-preview")
def render_prompt_preview(
    prompt_id: str,
    body: RenderPreviewRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The prompt a live call would run, rendered with the supplied test data.

    Uses the runtime resolver, so 'missing' here is exactly what a call with
    this context would leave unresolved (and therefore handle per the
    prompt's missing-information instructions — never by inventing values).
    """
    prompt = _prompt_checked(db, prompt_id, user)
    version_no = body.version or prompt.active_version
    version = next((v for v in prompt.versions if v.version == version_no), None)
    if version is None:
        raise ApiError("Unknown prompt version.", 422)
    compiled = version.compiled_prompt or ""
    if not compiled:
        variant = next((v for v in (version.variants or []) if v.get("content")), None)
        compiled = variant["content"] if variant else ""
    if not compiled:
        raise ApiError("This version has no content to render.", 422)
    return ok({
        "promptVersion": version_no,
        "promptMode": version.prompt_mode,
        **render_preview(compiled, body.test_context),
    })


@router.post("/prompts/{prompt_id}/duplicate", status_code=201)
def duplicate_prompt(
    prompt_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_prompts", "prompts.manage")),
    db: Session = Depends(get_db),
):
    src = _prompt_checked(db, prompt_id, user)
    base = f"{src.name} (copy)"
    name, n = base, 2
    while db.scalar(select(Prompt).where(
        Prompt.bot_id == src.bot_id, Prompt.name == name, Prompt.is_deleted.is_(False)
    )) is not None:
        name, n = f"{base} {n}", n + 1
    clone = Prompt(
        id=new_id("pr"), tenant_id=src.tenant_id, bot_id=src.bot_id, type=src.type,
        name=name, description=src.description, variables=src.variables,
        state="draft", active_version=1, created_by=user.id,
    )
    db.add(clone)
    active = next((v for v in src.versions if v.version == src.active_version), None)
    db.add(
        PromptVersion(
            id=new_id("prv"), prompt_id=clone.id, version=1, edited_by=user.name,
            edited_by_user_id=user.id, edited_at=datetime.now(timezone.utc),
            note=f"Duplicated from {src.name} v{src.active_version}",
            variants=(active.variants if active else []) or [],
            prompt_mode=active.prompt_mode if active else "structured",
            structured_config=active.structured_config if active else None,
            full_prompt=active.full_prompt if active else None,
            compiled_prompt=active.compiled_prompt if active else None,
        )
    )
    _refresh_prompt_readiness(db, clone)
    record_audit(
        db, user=user, action="Duplicated prompt", entity_type="prompt",
        entity_id=clone.id, target_label=name, tenant_id=src.tenant_id,
        new_value={"from": src.id}, request=request,
    )
    db.commit()
    db.refresh(clone)
    return ok(serialize_prompt(clone))


class UpdatePromptRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    variables: list[str] | None = None
    state: str | None = Field(
        default=None,
        pattern="^(draft|pending_approval|approved|rejected|published|archived)$",
    )
    active_version: int | None = Field(default=None, alias="activeVersion", ge=1)

    model_config = {"populate_by_name": True}


_STATE_PERMISSIONS = {
    "approved": ("approve_prompts", "prompts.manage"),
    "rejected": ("approve_prompts", "prompts.manage"),
    "published": ("publish_prompts", "prompts.manage"),
}


@router.patch("/prompts/{prompt_id}")
def update_prompt(
    prompt_id: str,
    body: UpdatePromptRequest,
    request: Request,
    user: User = Depends(require_permission("manage_prompts", "approve_prompts",
                                            "publish_prompts", "prompts.manage")),
    db: Session = Depends(get_db),
):
    prompt = _prompt_checked(db, prompt_id, user)
    before = {"state": prompt.state, "activeVersion": prompt.active_version}
    if body.name:
        prompt.name = body.name
    if body.description is not None:
        prompt.description = body.description
    if body.variables is not None:
        prompt.variables = body.variables
    if body.active_version is not None:
        if body.active_version not in {v.version for v in prompt.versions}:
            raise ApiError("Unknown prompt version.", 422)
        prompt.active_version = body.active_version
        if prompt.state == "published" and prompt.published_version != body.active_version:
            # Rollback / roll-forward of the published pointer.
            if not any(has_permission(user, c) for c in _STATE_PERMISSIONS["published"]):
                raise ForbiddenError("Publishing permissions are required to change the live version.")
            prompt.published_version = body.active_version
            record_audit(
                db, user=user, action="Rolled back prompt", entity_type="prompt",
                entity_id=prompt.id, target_label=f"{prompt.name} → v{body.active_version}",
                tenant_id=prompt.tenant_id, previous_value=before,
                new_value={"publishedVersion": body.active_version}, request=request,
            )
    if body.state is not None and body.state != prompt.state:
        needed = _STATE_PERMISSIONS.get(body.state)
        if needed and not any(has_permission(user, c) for c in needed):
            raise ForbiddenError("You do not have permission for this approval action.")
        prompt.state = body.state
        if body.state == "approved":
            prompt.approved_by = user.name
            prompt.approved_at = datetime.now(timezone.utc)
            record_audit(
                db, user=user, action="Approved prompt", entity_type="prompt",
                entity_id=prompt.id, target_label=prompt.name, tenant_id=prompt.tenant_id,
                previous_value=before,
                new_value={"state": "approved", "activeVersion": prompt.active_version},
                request=request,
            )
        elif body.state == "published":
            prompt.published_version = prompt.active_version
            prompt.published_at = datetime.now(timezone.utc)
            record_audit(
                db, user=user, action="Published prompt", entity_type="prompt",
                entity_id=prompt.id, target_label=f"{prompt.name} v{prompt.active_version}",
                tenant_id=prompt.tenant_id, previous_value=before,
                new_value={"state": "published", "publishedVersion": prompt.published_version},
                request=request,
            )
        elif body.state == "rejected":
            record_audit(
                db, user=user, action="Rejected prompt", entity_type="prompt",
                entity_id=prompt.id, target_label=prompt.name, tenant_id=prompt.tenant_id,
                previous_value=before, new_value={"state": "rejected"}, request=request,
            )
    prompt.updated_by = user.id
    _refresh_prompt_readiness(db, prompt)
    record_audit(
        db, user=user, action="Updated prompt", entity_type="prompt", entity_id=prompt.id,
        target_label=prompt.name, tenant_id=prompt.tenant_id, previous_value=before,
        new_value={"state": prompt.state, "activeVersion": prompt.active_version},
        request=request,
    )
    db.commit()
    db.refresh(prompt)
    if prompt.state == "published" and (
        before["state"] != "published"
        or before["activeVersion"] != prompt.active_version
    ):
        # A publish or a live-version move must reach calls NOW, not after the
        # bot-config cache TTL: the conversation record traces the version it
        # ran on, so serving a stale one would falsify that trace.
        from shared.bot_config import invalidate_bot_config_sync

        invalidate_bot_config_sync(prompt.tenant_id, prompt.bot_id)
    return ok(serialize_prompt(prompt))


@router.delete("/prompts/{prompt_id}")
def delete_prompt(
    prompt_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_permission("manage_prompts", "prompts.manage")),
    db: Session = Depends(get_db),
):
    prompt = _prompt_checked(db, prompt_id, user)
    if hard:
        guard_hard_delete()
    soft_delete(prompt, user)
    prompt.state = "archived"
    _refresh_prompt_readiness(db, prompt)
    record_audit(
        db, user=user, action="Archived prompt", entity_type="prompt", entity_id=prompt.id,
        target_label=prompt.name, tenant_id=prompt.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": prompt.id})


# ── Prompt testing (text-only, no state-changing tools) ──────────────────────


class PromptTestRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    language: str = Field(default="en-US", max_length=15)
    version: int | None = Field(default=None, ge=1)
    use_knowledge: bool = Field(default=True, alias="useKnowledge")

    model_config = {"populate_by_name": True}


@router.post("/prompts/{prompt_id}/test")
async def test_prompt(
    prompt_id: str,
    body: PromptTestRequest,
    user: User = Depends(require_permission("manage_prompts", "prompts.manage")),
    db: Session = Depends(get_db),
):
    """Run a sample caller message against a prompt version: routing decision,
    optional KB retrieval, and an LLM response using the bot's configured
    provider. Text-only — no tools are executed, nothing is state-changing."""
    from shared.bot_config import resolve_voice_identity_for_settings
    from shared.config import get_settings
    from shared.models import Intent, VoiceBotSetting
    from shared.orchestration.placeholders import resolve_placeholders
    from shared.orchestration.router import RouteKind, TurnRouter
    from shared.orchestration.voice_identity import (
        voice_context_values,
        voice_identity_instruction,
    )
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_llm_provider

    prompt = _prompt_checked(db, prompt_id, user)
    bot = _bot_checked(db, prompt.bot_id, user)

    version_no = body.version or prompt.active_version
    version = next((v for v in prompt.versions if v.version == version_no), None)
    if version is None:
        raise ApiError("Unknown prompt version.", 422)

    system = version.compiled_prompt
    if not system:
        variant = next((v for v in (version.variants or []) if v.get("content")), None)
        system = variant["content"] if variant else ""
    if not system:
        raise ApiError("This version has no compiled prompt or content to test.", 422)

    # 1. Routing decision using the bot's live intents.
    intents = db.scalars(
        select(Intent).where(
            Intent.bot_id == bot.id, Intent.is_deleted.is_(False), Intent.status == "active"
        )
    ).all()
    kb_ids = db.scalars(
        select(KnowledgeSource.id).where(
            KnowledgeSource.is_deleted.is_(False),
            KnowledgeSource.status.in_(("indexed", "stale")),
            ((KnowledgeSource.bot_id == bot.id)
             | ((KnowledgeSource.tenant_id == bot.tenant_id) & (KnowledgeSource.scope == "tenant"))
             | (KnowledgeSource.scope == "global")),
        )
    ).all()
    router_ = TurnRouter(
        intents=[{"name": i.name, "samples": i.samples or [], "route": i.route,
                  "confidence_threshold": i.confidence_threshold} for i in intents],
        has_knowledge_bases=bool(kb_ids),
    )
    decision = router_.decide(body.message)

    # 2. Optional retrieval (same service the voice bot uses).
    sources, used_kb = [], False
    if body.use_knowledge and kb_ids and decision.kind in (RouteKind.KNOWLEDGE, RouteKind.CHAT):
        from shared.knowledge.schemas import RetrievalRequest
        from shared.knowledge.service import get_knowledge_service

        result = await get_knowledge_service().search(
            RetrievalRequest(
                tenant_id=bot.tenant_id, kb_ids=[str(k) for k in kb_ids],
                query=body.message, top_k=4,
            )
        )
        used_kb = result.used_knowledge_base
        sources = [
            {"documentName": s.document_name, "score": round(s.score, 4),
             "text": s.text[:400]}
            for s in result.sources
        ]

    # 3. LLM response with the bot's configured provider (mock works keyless).
    settings = get_settings()
    vbs = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    voice_identity = resolve_voice_identity_for_settings(
        db, vbs, bot.tenant_id, body.language,
        default_provider=settings.tts_provider,
        default_voice=settings.tts_voice,
    )
    system = (
        resolve_placeholders(system, voice_context_values(voice_identity))
        + voice_identity_instruction(voice_identity)
    )
    provider_code = (vbs.llm_provider if vbs and vbs.llm_provider else settings.llm_provider)
    model = (vbs.llm_model if vbs and vbs.llm_model else settings.llm_model)
    llm = get_llm_provider(ProviderConfig(
        provider=provider_code, model=model,
        api_key_reference=settings.llm_api_key_reference,
    ))
    context = ""
    if sources:
        context = "\n\nContext from the knowledge base:\n" + "\n---\n".join(
            s["text"] for s in sources
        )
    started = time.monotonic()
    error = None
    response_text = ""
    tokens_in = tokens_out = 0
    try:
        result = await llm.generate(
            [{"role": "user", "content": body.message + context}],
            system=system, max_tokens=400,
        )
        response_text = result.text
        tokens_in, tokens_out = result.input_tokens, result.output_tokens
        if provider_code != "mock":
            # A real provider call was billed — record it for the bot's tenant.
            from shared.billing.metering import record_usage_event

            record_usage_event(
                db,
                tenant_id=bot.tenant_id,
                bot_id=bot.id,
                capability="llm",
                provider_code=provider_code,
                model_code=model or "",
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                usage_source="provider",
                usage_metadata={"kind": "prompt_test"},
                commit=False,
            )
            db.commit()
    except Exception as exc:  # noqa: BLE001 — surfaced as a safe test failure
        error = f"LLM provider '{provider_code}' failed: {type(exc).__name__}"
    latency_ms = round((time.monotonic() - started) * 1000)

    return ok({
        "promptVersion": version_no,
        "language": body.language,
        "route": decision.kind.value,
        "matchedIntent": decision.intent,
        "intentConfidence": round(decision.confidence, 3),
        "usedKnowledgeBase": used_kb,
        "sources": sources,
        "response": response_text,
        "latencyMs": latency_ms,
        "tokens": {"input": tokens_in, "output": tokens_out},
        "provider": provider_code,
        "error": error,
    })
