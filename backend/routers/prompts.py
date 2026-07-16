"""Prompts and versioned prompt content."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user, require_tenant_admin
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.db.mysql import get_db
from backend.models import Prompt, PromptVersion, User, VoiceBot
from backend.serializers import serialize_prompt

router = APIRouter(tags=["Prompts"])

PROMPT_TYPES = "^(greeting|fallback|escalation|closing|reprompt|hold)$"


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


class CreatePromptRequest(BaseModel):
    type: str = Field(pattern=PROMPT_TYPES)
    name: str = Field(min_length=1, max_length=200)
    variables: list[str] = Field(default_factory=list)
    variants: list[VariantPayload] = Field(default_factory=list)
    note: str = Field(default="Initial version", max_length=500)


@router.post("/bots/{bot_id}/prompts", status_code=201)
def create_prompt(
    bot_id: str,
    body: CreatePromptRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    prompt = Prompt(
        id=new_id("pr"), tenant_id=bot.tenant_id, bot_id=bot.id, type=body.type,
        name=body.name, variables=body.variables, state="draft", active_version=1,
        created_by=user.id,
    )
    db.add(prompt)
    db.add(
        PromptVersion(
            id=new_id("prv"), prompt_id=prompt.id, version=1, edited_by=user.name,
            edited_by_user_id=user.id, edited_at=datetime.now(timezone.utc),
            note=body.note, variants=[v.model_dump() for v in body.variants],
        )
    )
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
    variants: list[VariantPayload]


@router.post("/prompts/{prompt_id}/versions", status_code=201)
def add_prompt_version(
    prompt_id: str,
    body: NewVersionRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    prompt = _prompt_checked(db, prompt_id, user)
    latest = max((v.version for v in prompt.versions), default=0)
    db.add(
        PromptVersion(
            id=new_id("prv"), prompt_id=prompt.id, version=latest + 1,
            edited_by=user.name, edited_by_user_id=user.id,
            edited_at=datetime.now(timezone.utc), note=body.note,
            variants=[v.model_dump() for v in body.variants],
        )
    )
    prompt.state = "pending_approval"
    prompt.updated_by = user.id
    record_audit(
        db, user=user, action="Edited prompt (pending approval)", entity_type="prompt",
        entity_id=prompt.id, target_label=f"{prompt.name} v{latest + 1}",
        tenant_id=prompt.tenant_id, new_value={"version": latest + 1, "note": body.note},
        request=request,
    )
    db.commit()
    db.refresh(prompt)
    return ok(serialize_prompt(prompt))


class UpdatePromptRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    variables: list[str] | None = None
    state: str | None = Field(default=None, pattern="^(draft|pending_approval|approved)$")
    active_version: int | None = Field(default=None, alias="activeVersion", ge=1)

    model_config = {"populate_by_name": True}


@router.patch("/prompts/{prompt_id}")
def update_prompt(
    prompt_id: str,
    body: UpdatePromptRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    prompt = _prompt_checked(db, prompt_id, user)
    before = {"state": prompt.state, "activeVersion": prompt.active_version}
    if body.name:
        prompt.name = body.name
    if body.variables is not None:
        prompt.variables = body.variables
    if body.active_version is not None:
        if body.active_version not in {v.version for v in prompt.versions}:
            raise ApiError("Unknown prompt version.", 422)
        prompt.active_version = body.active_version
    if body.state is not None:
        # Approval is a tenant-admin/super-admin action (already enforced by guard).
        prompt.state = body.state
        if body.state == "approved":
            record_audit(
                db, user=user, action="Approved prompt", entity_type="prompt",
                entity_id=prompt.id, target_label=prompt.name, tenant_id=prompt.tenant_id,
                previous_value=before,
                new_value={"state": "approved", "activeVersion": prompt.active_version},
                request=request,
            )
    prompt.updated_by = user.id
    record_audit(
        db, user=user, action="Updated prompt", entity_type="prompt", entity_id=prompt.id,
        target_label=prompt.name, tenant_id=prompt.tenant_id, previous_value=before,
        new_value={"state": prompt.state, "activeVersion": prompt.active_version},
        request=request,
    )
    db.commit()
    db.refresh(prompt)
    return ok(serialize_prompt(prompt))


@router.delete("/prompts/{prompt_id}")
def delete_prompt(
    prompt_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    prompt = _prompt_checked(db, prompt_id, user)
    if hard:
        guard_hard_delete()
    soft_delete(prompt, user)
    record_audit(
        db, user=user, action="Archived prompt", entity_type="prompt", entity_id=prompt.id,
        target_label=prompt.name, tenant_id=prompt.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": prompt.id})
