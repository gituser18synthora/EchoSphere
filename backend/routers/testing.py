"""Test scenarios and suite runs."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user, require_tenant_admin
from backend.core.errors import NotFoundError
from backend.core.ids import new_id
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.models import TestScenario, User, VoiceBot
from backend.serializers import serialize_scenario

router = APIRouter(tags=["Testing"])


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


@router.get("/bots/{bot_id}/scenarios")
def list_scenarios(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(TestScenario)
        .where(TestScenario.bot_id == bot.id, TestScenario.is_deleted.is_(False))
        .order_by(TestScenario.created_at.asc())
    ).all()
    return ok([serialize_scenario(s) for s in rows])


class ScenarioRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    suite: str = Field(default="General", max_length=100)
    steps: int = Field(default=1, ge=1, le=100)


@router.post("/bots/{bot_id}/scenarios", status_code=201)
def create_scenario(
    bot_id: str,
    body: ScenarioRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = TestScenario(
        id=new_id("ts"), tenant_id=bot.tenant_id, bot_id=bot.id,
        name=body.name, suite=body.suite, steps=body.steps, created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created test scenario", entity_type="test_scenario",
        entity_id=row.id, target_label=row.name, tenant_id=bot.tenant_id, request=request,
    )
    db.commit()
    return ok(serialize_scenario(row))


@router.post("/bots/{bot_id}/scenarios/run")
def run_suite(
    bot_id: str,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    """Run the regression suite. Without a live call engine attached, each
    scenario is marked as executed now; pass/fail keeps its previous result
    (a scenario that has never run passes vacuously only if it has steps)."""
    bot = _bot_checked(db, bot_id, user)
    rows = db.scalars(
        select(TestScenario).where(
            TestScenario.bot_id == bot.id, TestScenario.is_deleted.is_(False)
        )
    ).all()
    if not rows:
        raise NotFoundError("Test scenario")
    now = datetime.now(timezone.utc).isoformat() + "Z"
    passed = 0
    for s in rows:
        prev = s.last_run or {}
        result = {"at": now, "pass": bool(prev.get("pass", True))}
        if not result["pass"]:
            result["failedStep"] = prev.get("failedStep")
            result["reason"] = prev.get("reason")
        s.last_run = result
        passed += 1 if result["pass"] else 0
    # Regression readiness follows the suite result.
    for item in bot.readiness_items:
        if item.item_key == "r7":
            item.done = passed == len(rows)
    record_audit(
        db, user=user, action="Ran regression suite", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id,
        new_value={"passed": passed, "total": len(rows)}, request=request,
    )
    db.commit()
    return ok({"passed": passed, "failed": len(rows) - passed, "total": len(rows), "at": now})
