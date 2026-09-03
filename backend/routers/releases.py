"""Releases: publish pipeline with approvals and rollback."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    SUPER_ADMIN,
    assert_tenant_access,
    require_permission,
    require_tenant_admin,
)
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import Release, TestScenario, User, VoiceBot
from backend.serializers import serialize_release
from shared.readiness import refresh_readiness

router = APIRouter(tags=["Releases"])

# Allowed stage transitions
_TRANSITIONS = {
    "draft": {"review"},
    "review": {"approved", "draft"},
    "approved": {"published", "draft"},
    "published": {"rolled_back"},
    "rolled_back": set(),
}


# A release that is still moving through the pipeline. Only one may exist per
# bot at a time — the Publish tab drives exactly one "current" release.
OPEN_STAGES = ("draft", "review", "approved")


def _open_release(db: Session, bot_id: str) -> Release | None:
    return db.scalar(
        select(Release)
        .where(
            Release.bot_id == bot_id, Release.is_deleted.is_(False),
            Release.stage.in_(OPEN_STAGES),
        )
        .order_by(Release.created_at.desc())
        .limit(1)
    )


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _build_checklist(db: Session, bot: VoiceBot) -> list[dict]:
    """Evaluate the publish gate from CURRENT platform state.

    Readiness flags are recomputed here (not read as stored) so a checklist
    never blocks on a stale r-flag — e.g. a channel that was archived and
    re-created before the flush fix landed."""
    refresh_readiness(db, bot)
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
    bot_id: str,
    user: User = Depends(require_permission("bots.publish", "bots.manage")),
    db: Session = Depends(get_db),
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
    existing = _open_release(db, bot.id)
    if existing is not None:
        raise ApiError(
            f"Release {existing.version} is already {existing.stage}. "
            "Publish or return it to draft before creating another.", 409,
        )
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
    # Free-text note for review / approval decisions (audit only).
    note: str | None = Field(default=None, max_length=2000)
    # Super-admin only: publish although checklist items fail. Recorded in the
    # audit log together with the failing items. Ignored for other roles.
    override_reason: str | None = Field(
        default=None, alias="overrideReason", max_length=2000
    )

    model_config = {"populate_by_name": True}


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

    # Every forward move re-evaluates the gate so the UI always shows the
    # current state, not the snapshot taken when the release was requested.
    if body.stage in ("review", "approved", "published"):
        row.checklist = _build_checklist(db, bot)
    override_used = False
    if body.stage == "approved":
        row.approved_by = user.name
    if body.stage == "published":
        blocked = [c["label"] for c in row.checklist if not c["ok"]]
        if blocked:
            reason = (body.override_reason or "").strip()
            if user.role.code == SUPER_ADMIN and len(reason) >= 10:
                override_used = True
            elif user.role.code == SUPER_ADMIN:
                raise ApiError(
                    "Publish blocked — checklist incomplete: " + "; ".join(blocked)
                    + ". A super admin may override by supplying a justification "
                    "(overrideReason, at least 10 characters).", 422,
                )
            else:
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
    if override_used:
        action = "Published release with checklist override"
    new_value: dict = {"stage": row.stage}
    if body.note:
        new_value["note"] = body.note
    if override_used:
        new_value["override"] = {
            "reason": (body.override_reason or "").strip(),
            "failedChecks": [c["label"] for c in row.checklist if not c["ok"]],
        }
    record_audit(
        db, user=user, action=action, entity_type="release", entity_id=row.id,
        target_label=f"{bot.name} {row.version}", tenant_id=row.tenant_id,
        previous_value=before, new_value=new_value, request=request,
    )
    db.commit()
    if body.stage in ("published", "rolled_back"):
        # Live calls pin their config at start; new calls must see this release.
        from shared.bot_config import invalidate_bot_config_sync

        invalidate_bot_config_sync(row.tenant_id, row.bot_id)
    return ok(serialize_release(row))
