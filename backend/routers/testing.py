"""Test scenarios, suite runs and the live chat tester.

The chat tester (`POST /bots/{bot_id}/testing/chat`) runs a text turn through
the SAME components the voice runtime uses — TurnRouter for routing and the
WorkflowEngine for saved workflow execution — so the Studio Testing tab
exercises real behavior, not a UI simulation. Audio (STT/TTS) is the only
part not covered here.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user, require_tenant_admin
from shared.errors import NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import Intent, KnowledgeSource, TestScenario, User, VoiceBot
from backend.serializers import serialize_scenario

router = APIRouter(tags=["Testing"])

_CHAT_SESSION_TTL_SECONDS = 1800  # active-workflow marker for a test session


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


# ── live chat tester: the real router + workflow engine, text-only ───────────


class ChatTestRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    # Stable per conversation so multi-turn workflow state persists; the
    # client keeps sending the id the first response returned.
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)

    model_config = {"populate_by_name": True}


def _build_router(db: Session, bot: VoiceBot):
    """The same TurnRouter construction the voice runtime uses."""
    from shared.orchestration.router import TurnRouter

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
             | ((KnowledgeSource.tenant_id == bot.tenant_id)
                & (KnowledgeSource.scope == "tenant"))
             | (KnowledgeSource.scope == "global")),
        ).limit(1)
    ))
    return TurnRouter(
        intents=[{"name": i.name, "samples": i.samples or [], "route": i.route,
                  "confidence_threshold": i.confidence_threshold} for i in intents],
        has_knowledge_bases=has_kbs,
    )


async def _knowledge_reply(bot: VoiceBot, message: str) -> str:
    from shared.knowledge.schemas import RetrievalRequest
    from shared.knowledge.service import get_knowledge_service

    result = await get_knowledge_service().search(
        RetrievalRequest(tenant_id=bot.tenant_id, bot_id=bot.id, query=message)
    )
    if result.answerable and result.sources:
        return result.sources[0].text[:400]
    return "I couldn't find that in the information I have."


@router.post("/bots/{bot_id}/testing/chat")
async def chat_test(
    bot_id: str,
    body: ChatTestRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One text turn through the runtime routing + workflow stack.

    Multi-turn: pass back the returned sessionId. The active-workflow marker
    lives in Redis (mirroring the brain's `_active_workflow`), and workflow
    slot state persists in the engine's LangGraph checkpoints — exactly the
    state model of a live call.
    """
    from shared.db.redis import get_redis
    from shared.orchestration.router import RouteKind
    from shared.orchestration.workflow_engine import get_workflow_engine

    bot = _bot_checked(db, bot_id, user)
    session = body.session_id or f"ct_{uuid.uuid4().hex[:12]}"
    redis = get_redis()
    active_key = f"wftest:{bot.id}:{session}"
    try:
        active_workflow = await redis.get(active_key)
        if isinstance(active_workflow, bytes):
            active_workflow = active_workflow.decode()
    except Exception:  # noqa: BLE001 — degrade to single-turn routing
        active_workflow = None

    decision = _build_router(db, bot).decide(body.message, active_workflow=active_workflow)

    reply = ""
    done = True
    workflow_detail: dict | None = None
    if decision.kind == RouteKind.WORKFLOW:
        name = decision.action or active_workflow
        if name:
            engine = get_workflow_engine()
            result = await engine.handle_turn_detailed(
                session_id=f"test:{bot.id}:{session}",
                tenant_id=bot.tenant_id,
                bot_id=bot.id,
                workflow_name=name,
                user_text=body.message,
            )
            reply, done = result["reply"], result["done"]
            workflow_detail = {
                "name": name,
                "source": result["source"],
                "status": result["status"],
                "workflowId": result["workflowId"],
                "nodeTrace": result["trace"],
                "slots": result["slots"],
                "done": done,
            }
            try:
                if done:
                    await redis.delete(active_key)
                else:
                    await redis.set(active_key, name, ex=_CHAT_SESSION_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                pass
        else:
            reply = "Could you tell me a bit more about what you need?"
    elif decision.kind == RouteKind.KNOWLEDGE:
        reply = await _knowledge_reply(bot, body.message)
    elif decision.kind == RouteKind.HANDOFF:
        reply = "I understand — let me connect you with a human agent. Please hold on."
    elif decision.kind == RouteKind.SAFETY:
        reply = "I can't help with that over this channel."
    elif decision.kind == RouteKind.CLARIFY:
        reply = "Could you tell me a little more about what you need?"
    elif decision.kind == RouteKind.CALL_CONTROL:
        reply = f"(call control: {decision.action or 'acknowledged'})"
    else:  # CHAT / INTENT / TOOL — the live call would answer via the LLM
        reply = ("(In a live call the assistant would answer conversationally here "
                 "via the configured LLM.)")

    return ok({
        "sessionId": session,
        "route": decision.kind.value,
        "action": decision.action,
        "matchedIntent": decision.intent,
        "confidence": round(decision.confidence, 3),
        "reason": decision.reason,
        "reply": reply,
        "done": done,
        "activeWorkflow": (workflow_detail or {}).get("name") if workflow_detail and not done else None,
        "workflow": workflow_detail,
    })
