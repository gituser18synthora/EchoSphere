"""Releases: publish pipeline with approvals and rollback."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user, require_tenant_admin
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.models import Release, TestScenario, User, VoiceBot
from backend.serializers import serialize_release

router = APIRouter(tags=["Releases"])

# Allowed stage transitions
_TRANSITIONS = {
    "draft": {"review"},
    "review": {"approved", "draft"},
    "approved": {"published", "draft"},
    "published": {"rolled_back"},
    "rolled_back": set(),
}


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _build_checklist(db: Session, bot: VoiceBot) -> list[dict]:
    scenarios = db.scalars(
        select(TestScenario).where(
            TestScenario.bot_id == bot.id, TestScenario.is_deleted.is_(False)
        )
    ).all()
    failing = [s for s in scenarios if s.last_run and not s.last_run.get("pass")]
    never_run = [s for s in scenarios if not s.last_run]
    readiness = {r.item_key: r.done for r in bot.readiness_items}
    return [
        {
            "id": "c1", "label": "All regression tests passing",
            "ok": bool(scenarios) and not failing and not never_run,
            "detail": f"{len(failing) + len(never_run)} of {len(scenarios)} scenarios not passing"
            if scenarios else "No scenarios defined",
        },
        {"id": "c2", "label": "Prompts approved", "ok": readiness.get("r3", False)},
        {"id": "c3", "label": "Knowledge sources indexed", "ok": readiness.get("r1", False)},
        {"id": "c4", "label": "Workflow published", "ok": readiness.get("r5", False)},
        {"id": "c5", "label": "Channels tested", "ok": readiness.get("r6", False)},
        {"id": "c6", "label": "Voice selected & tuned", "ok": readiness.get("r2", False)},
    ]


@router.get("/bots/{bot_id}/releases")
def list_releases(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(Release)
        .where(Release.bot_id == bot.id, Release.is_deleted.is_(False))
        .order_by(Release.created_at.desc())
    ).all()
    return ok([serialize_release(r) for r in rows])


class CreateReleaseRequest(BaseModel):
    version: str = Field(min_length=1, max_length=20)
    notes: str = Field(default="", max_length=2000)
    diff: list[dict] = Field(default_factory=list)
    scheduled_for: datetime | None = Field(default=None, alias="scheduledFor")

    model_config = {"populate_by_name": True}


@router.post("/bots/{bot_id}/releases", status_code=201)
def create_release(
    bot_id: str,
    body: CreateReleaseRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = Release(
        id=new_id("rel"), tenant_id=bot.tenant_id, bot_id=bot.id, version=body.version,
        stage="review", notes=body.notes, requested_by=user.name,
        scheduled_for=body.scheduled_for, checklist=_build_checklist(db, bot),
        diff=body.diff, created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Submitted release for review", entity_type="release",
        entity_id=row.id, target_label=f"{bot.name} {body.version}",
        tenant_id=bot.tenant_id, new_value={"version": body.version}, request=request,
    )
    db.commit()
    return ok(serialize_release(row))


class ReleaseStageRequest(BaseModel):
    stage: str = Field(pattern="^(draft|review|approved|published|rolled_back)$")


@router.patch("/releases/{release_id}")
def update_release_stage(
    release_id: str,
    body: ReleaseStageRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(Release, release_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Release")
    assert_tenant_access(user, row.tenant_id)
    if body.stage not in _TRANSITIONS.get(row.stage, set()):
        raise ApiError(f"A release in stage '{row.stage}' cannot move to '{body.stage}'.", 422)

    bot = db.get(VoiceBot, row.bot_id)
    before = {"stage": row.stage}

    if body.stage == "approved":
        row.approved_by = user.name
    if body.stage == "published":
        checklist = _build_checklist(db, bot)
        row.checklist = checklist
        blocked = [c["label"] for c in checklist if not c["ok"]]
        if blocked:
            raise ApiError(
                "Publish blocked — checklist incomplete: " + "; ".join(blocked), 422
            )
        row.published_at = datetime.now(timezone.utc)
        bot.status = "published"
        bot.live_version = row.version
        bot.version = row.version
        bot.published_at = row.published_at
    if body.stage == "rolled_back":
        prev = db.scalar(
            select(Release)
            .where(
                Release.bot_id == row.bot_id, Release.stage == "published",
                Release.id != row.id, Release.is_deleted.is_(False),
            )
            .order_by(Release.published_at.desc())
        )
        bot.status = "rolled_back"
        bot.live_version = prev.version if prev else None

    row.stage = body.stage
    row.updated_by = user.id
    action = {
        "review": "Submitted release for review",
        "approved": "Approved release",
        "published": "Published release",
        "rolled_back": "Rolled back release",
        "draft": "Returned release to draft",
    }[body.stage]
    record_audit(
        db, user=user, action=action, entity_type="release", entity_id=row.id,
        target_label=f"{bot.name} {row.version}", tenant_id=row.tenant_id,
        previous_value=before, new_value={"stage": row.stage}, request=request,
    )
    db.commit()
    if body.stage in ("published", "rolled_back"):
        # Live calls pin their config at start; new calls must see this release.
        from backend.voice_runtime.bot_config import invalidate_bot_config_sync

        invalidate_bot_config_sync(row.tenant_id, row.bot_id)
    return ok(serialize_release(row))
