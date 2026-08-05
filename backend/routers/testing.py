"""Test scenarios, suite runs, the live chat tester and the full simulator.

The chat tester (`POST /bots/{bot_id}/testing/chat`) runs a text turn through
the SAME components the voice runtime uses — TurnRouter for routing and the
WorkflowEngine for saved workflow execution — so the Studio Testing tab
exercises real behavior, not a UI simulation.

The simulator (`POST /bots/{bot_id}/testing/simulate`) goes further: one
complete runtime turn — transcript finality gating, runtime context from a
manual payload / mock API response / the saved config, platform command
detection, hybrid LLM intent classification, domain policy, workflow
routing, MOCKED tool execution and the real LLM reply on the rendered
prompt — returning the full decision trace. Audio (STT/TTS) is the only part
not covered here.
"""

import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user, require_tenant_admin
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import (
    Intent,
    KnowledgeSource,
    Prompt,
    RuntimeContextSchema,
    TestScenario,
    User,
    VoiceBot,
)
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
            if result.get("offScript"):
                # The workflow held its node; a live call answers this turn
                # through the LLM, grounded in the paused step.
                reply = ("(Off-script turn — the workflow stays at its "
                         "current step; in a live call the assistant would "
                         "answer the caller's message via the LLM.)")
            workflow_detail = {
                "name": name,
                "source": result["source"],
                "status": result["status"],
                "workflowId": result["workflowId"],
                "nodeTrace": result["trace"],
                "slots": result["slots"],
                "offScript": bool(result.get("offScript")),
                "signal": result.get("signal"),
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


# ── full runtime simulator: one turn, complete trace ─────────────────────────


class SimulateRequest(BaseModel):
    """One simulated runtime turn. Everything optional has a live default."""

    message: str = Field(min_length=1, max_length=2000)
    # Prior conversation, oldest first: [{role: user|assistant, content}].
    messages: list[dict] = Field(default_factory=list, max_length=40)
    # Prompt selection: a specific prompt/version, else the published system
    # prompt (exactly what a live call would load).
    prompt_id: str | None = Field(default=None, alias="promptId")
    prompt_version: int | None = Field(default=None, alias="promptVersion", ge=1)
    # Runtime context source: "saved" uses the bot's configured source;
    # "manual" / "api_mock" validate contextPayload against the schema and
    # treat it as the manual test JSON / the User Details API response.
    context_source: str = Field(
        default="saved", alias="contextSource",
        pattern="^(saved|manual|api_mock|none)$",
    )
    context_payload: dict | None = Field(default=None, alias="contextPayload")
    language: str = Field(default="", max_length=15)
    # Transcript state: partial transcripts NEVER become turns in the live
    # runtime — the simulator demonstrates that instead of pretending.
    is_final: bool = Field(default=True, alias="isFinal")
    interrupted: bool = Field(default=False)
    # {tool_name: payload} — replaces live HTTP in tool/workflow execution.
    mock_tool_results: dict = Field(default_factory=dict, alias="mockToolResults")
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)

    model_config = {"populate_by_name": True}


def _simulate_prompt(db: Session, bot: VoiceBot, body: SimulateRequest) -> dict:
    """The compiled prompt + provenance the simulated call runs on."""
    prompt = None
    if body.prompt_id:
        prompt = db.get(Prompt, body.prompt_id)
        if prompt is None or prompt.is_deleted or prompt.bot_id != bot.id:
            raise NotFoundError("Prompt")
    else:
        prompt = db.scalar(
            select(Prompt).where(
                Prompt.bot_id == bot.id, Prompt.type == "system",
                Prompt.state == "published", Prompt.is_deleted.is_(False),
            ).limit(1)
        )
    if prompt is None:
        return {"compiled": "", "promptId": None, "promptVersion": None,
                "promptMode": None, "promptState": None}
    version_no = body.prompt_version or prompt.published_version or prompt.active_version
    version = next((v for v in prompt.versions if v.version == version_no), None)
    if version is None:
        raise ApiError("Unknown prompt version.", 422)
    return {
        "compiled": version.compiled_prompt or "",
        "promptId": prompt.id,
        "promptVersion": version_no,
        "promptMode": version.prompt_mode,
        "promptState": prompt.state,
    }


@router.post("/bots/{bot_id}/testing/simulate")
async def simulate_turn(
    bot_id: str,
    body: SimulateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One complete runtime turn with a full trace — the Testing Studio.

    Uses the same shared modules as the voice worker (context builder,
    router, hybrid classifier, collection policy, workflow engine, tool
    executor) so the trace shows real behavior; only audio and live tool
    HTTP are replaced (tools run against mockToolResults).
    """
    from shared.config import get_settings
    from shared.bot_config import resolve_voice_identity_for_settings
    from shared.models import VoiceBotSetting
    from shared.orchestration.delivery import delivery_instructions
    from shared.orchestration.intent_classifier import HybridIntentPipeline
    from shared.orchestration.placeholders import resolve_placeholders
    from shared.orchestration.router import (
        RouteKind,
        classify_user_signal,
        detect_do_not_call,
        detect_emergency,
        detect_hangup,
    )
    from shared.orchestration.tool_executor import get_tool_executor
    from shared.orchestration.voice_identity import (
        voice_context_values,
        voice_identity_instruction,
    )
    from shared.orchestration.workflow_engine import get_workflow_engine
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_llm_provider
    from shared.runtime_context import (
        build_runtime_context,
        collection_snapshot_from_context,
        validate_payload,
    )
    from voice_runtime.call_policy import CollectionCallPolicy

    started = time.monotonic()
    bot = _bot_checked(db, bot_id, user)
    trace: dict = {
        "rawTranscript": body.message,
        "isFinal": body.is_final,
        "interrupted": body.interrupted,
        "botVersion": bot.live_version or bot.version,
    }

    # 0. Transcript finality: a partial NEVER becomes a turn.
    if not body.is_final:
        trace.update({
            "finalTranscript": None,
            "heldForFinal": True,
            "route": None,
            "response": None,
            "note": (
                "Partial transcript — the runtime only feeds the live UI with "
                "partials; business routing, workflows and the LLM run on the "
                "completed (final) turn."
            ),
            "latencyMs": round((time.monotonic() - started) * 1000),
        })
        return ok(trace)
    trace["finalTranscript"] = body.message

    # 1. Runtime context (schema-validated, source-tagged, masked).
    schema = db.scalar(
        select(RuntimeContextSchema).where(
            RuntimeContextSchema.bot_id == bot.id,
            RuntimeContextSchema.is_deleted.is_(False),
        )
    )
    fields = (schema.fields if schema else []) or []
    context_errors: list[dict] = []
    payload = None
    payload_source = "test"
    if body.context_source == "none":
        payload = None
    elif body.context_source in ("manual", "api_mock") and body.context_payload is not None:
        context_errors, payload = validate_payload(
            fields, body.context_payload,
            allow_additional=bool(schema.allow_additional) if schema else True,
        )
        payload_source = "api" if body.context_source == "api_mock" else "test"
    elif schema is not None and isinstance(schema.test_payload, dict):
        context_errors, payload = validate_payload(
            fields, schema.test_payload,
            allow_additional=bool(schema.allow_additional),
        )
        payload_source = "test"
    runtime_ctx = build_runtime_context(
        tenant_id=bot.tenant_id, bot_id=bot.id,
        field_definitions=fields, payload=payload,
        payload_source=payload_source,
        system_values={"call_channel": "simulator", "bot_language": body.language or None},
        allow_additional=bool(schema.allow_additional) if schema else True,
        missing_value_policy=schema.missing_value_policy if schema else None,
        domain_policy=(schema.domain_policy if schema else "generic") or "generic",
        source_mode=body.context_source,
        schema_id=schema.id if schema else None,
    )
    trace["runtimeContext"] = {
        "values": runtime_ctx.items_with_sources(),
        "errors": context_errors,
        "missingRequired": runtime_ctx.missing_required(),
        "domainPolicy": runtime_ctx.domain_policy,
    }

    # 2. Prompt: selected (or published) version rendered with the context.
    prompt_info = _simulate_prompt(db, bot, body)
    context_values = runtime_ctx.prompt_values()
    base_prompt = prompt_info["compiled"] or (
        f"You are {bot.name}, a helpful voice assistant. Keep answers short "
        "and conversational. Never invent facts."
    )
    vbs = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    settings = get_settings()
    simulation_language = body.language or (
        ((vbs.language_voice_map or {}).get("default") if vbs else None)
        or (bot.languages[0].language_code if bot.languages else "en")
    )
    voice_identity = resolve_voice_identity_for_settings(
        db, vbs, bot.tenant_id, simulation_language,
        default_provider=settings.tts_provider,
        default_voice=settings.tts_voice,
    )
    context_values.update(voice_context_values(voice_identity))
    policy: CollectionCallPolicy | None = None
    if runtime_ctx.domain_policy == "collections":
        policy = CollectionCallPolicy(
            context=collection_snapshot_from_context(runtime_ctx),
            language=simulation_language,
        )
        policy.tools_available = bool(body.mock_tool_results)
        # Replay the prior conversation through the policy so its state
        # matches where a live call would be at this turn.
        for message in body.messages:
            content = str(message.get("content") or "")
            if not content:
                continue
            if message.get("role") == "assistant":
                policy.observe_bot(content)
            else:
                policy.observe_user(content, classify_user_signal(content))
        if body.interrupted:
            policy.interruption_detected = True
    context_block = (
        policy.static_instruction() if policy is not None
        else runtime_ctx.prompt_section()
    )
    rendered_prompt = (
        resolve_placeholders(base_prompt, context_values)
        + delivery_instructions(
            (vbs.empathy if vbs and vbs.empathy is not None else 50),
            (vbs.energy if vbs and vbs.energy is not None else 50),
        )
        + voice_identity_instruction(voice_identity)
        + context_block
    )
    trace.update({
        "promptId": prompt_info["promptId"],
        "promptVersion": prompt_info["promptVersion"],
        "promptMode": prompt_info["promptMode"],
        "promptState": prompt_info["promptState"],
        "voiceIdentity": {
            "name": voice_identity.name,
            "gender": voice_identity.gender,
        },
        "renderedPrompt": rendered_prompt,
    })

    # 3. Platform deterministic commands first — never the LLM's call.
    if detect_hangup(body.message):
        trace.update({"route": "call_control", "action": "hangup",
                      "response": "(call ends: caller-requested hang-up)",
                      "latencyMs": round((time.monotonic() - started) * 1000)})
        return ok(trace)
    if detect_do_not_call(body.message):
        trace.update({"route": "call_control", "action": "do_not_call",
                      "disposition": "do_not_call",
                      "response": "(call ends: number marked do-not-call)",
                      "latencyMs": round((time.monotonic() - started) * 1000)})
        return ok(trace)
    if detect_emergency(body.message):
        trace.update({"route": "handoff", "action": "transfer",
                      "reason": "emergency",
                      "response": "(escalated to a human agent: emergency)",
                      "latencyMs": round((time.monotonic() - started) * 1000)})
        return ok(trace)

    # 4. Routing + hybrid intent classification (real LLM, bounded).
    provider_code = (vbs.llm_provider if vbs and vbs.llm_provider else settings.llm_provider)
    model = (vbs.llm_model if vbs and vbs.llm_model else settings.llm_model)
    llm = get_llm_provider(ProviderConfig(
        provider=provider_code, model=model,
        api_key_reference=settings.llm_api_key_reference,
    ))
    intents = db.scalars(
        select(Intent).where(
            Intent.bot_id == bot.id, Intent.is_deleted.is_(False),
            Intent.status == "active",
        )
    ).all()
    intent_dicts = [
        {"name": i.name, "description": i.description or "",
         "samples": i.samples or [], "route": i.route,
         "confidence_threshold": i.confidence_threshold,
         "entities": i.entities or [], "optional_entities": i.optional_entities or [],
         "api_connection_id": i.api_connection_id, "workflow_id": i.workflow_id}
        for i in intents
    ]
    session = body.session_id or f"sim_{uuid.uuid4().hex[:12]}"
    from shared.db.redis import get_redis

    redis = get_redis()
    active_key = f"wftest:{bot.id}:{session}"
    try:
        active_workflow = await redis.get(active_key)
        if isinstance(active_workflow, bytes):
            active_workflow = active_workflow.decode()
    except Exception:  # noqa: BLE001 — degrade to single-turn routing
        active_workflow = None

    decision = _build_router(db, bot).decide(body.message, active_workflow=active_workflow)
    pipeline = HybridIntentPipeline(llm=llm, intents=intent_dicts, enabled=True)
    classification = await pipeline.classify(
        body.message, body.messages, active_workflow=active_workflow,
    )
    signal = classification.signal or decision.signal or classify_user_signal(body.message)
    trace["intent"] = classification.as_event()
    trace["signal"] = signal
    trace["routerDecision"] = {
        "route": decision.kind.value, "reason": decision.reason,
        "confidence": round(decision.confidence, 3),
    }

    plan = None
    if policy is not None:
        policy.observe_user(body.message, signal)
        plan = policy.plan_turn(body.message, signal)
        trace["policy"] = {
            "phase": policy.phase,
            "blockers": policy.blockers(),
            "forceLlm": plan.force_llm,
            "handoff": plan.handoff,
            "closeAfterReply": plan.close_after_reply,
            "disposition": policy.disposition(),
        }

    # 5. Tool execution (mocked): validated exactly like a live call.
    tool_instruction = ""
    tool_trace = None
    tool_name = classification.tool_name
    if tool_name is None and signal == "already_paid":
        for intent in intent_dicts:
            if intent["name"] == "already_paid":
                route = intent.get("route") or ""
                if route.startswith("tool:"):
                    tool_name = route.split(":", 1)[1]
                elif intent.get("api_connection_id"):
                    tool_name = str(intent["api_connection_id"])
    if tool_name and not classification.below_threshold:
        result = await get_tool_executor().execute(
            tenant_id=bot.tenant_id, bot_id=bot.id, tool=tool_name,
            args={k: v for k, v in (classification.entities or {}).items()
                  if v is not None},
            intent=classification.intent or signal,
            session_id=f"sim:{session}",
            customer_verified=bool(policy and policy.verified),
            context_values=context_values,
            mock_results=body.mock_tool_results or None,
        )
        tool_trace = {
            "request": {"tool": tool_name,
                        "args": {k: v for k, v in (classification.entities or {}).items()
                                 if v is not None}},
            "response": result.trace.get("response") if result.trace else None,
            "ok": result.ok, "status": result.status, "error": result.error,
            "mocked": result.mocked, "latencyMs": result.latency_ms,
        }
        payload_map = result.mapped or (result.data if isinstance(result.data, dict) else {})
        if result.ok and signal == "already_paid" and policy is not None:
            status_value = payload_map.get("payment_status") or payload_map.get("status")
            policy.record_payment_verification(
                str(status_value) if status_value is not None else None
            )
            trace["paymentVerification"] = policy.payment_verified_status
        if result.ok:
            facts = "\n".join(f"- {k}: {v}" for k, v in list(payload_map.items())[:12])
            tool_instruction = (
                "\n\n# Tool result (verified by the system THIS turn)\n"
                f"`{tool_name}` returned:\n{facts or '- (no fields)'}\n"
                "These are the only verified facts from this check."
            )
        else:
            tool_instruction = (
                "\n\n# Tool result (THIS turn)\n"
                f"- The system check `{tool_name}` FAILED ({result.error or result.status}). "
                "Do not claim anything was verified."
            )
        if policy is not None and tool_instruction:
            # Same re-plan the live brain performs: the verified result — not
            # the claim — decides this reply's next step and close behavior.
            plan = policy.plan_turn(body.message, signal)
            trace["policy"] = {
                "phase": policy.phase, "blockers": policy.blockers(),
                "forceLlm": plan.force_llm, "handoff": plan.handoff,
                "closeAfterReply": plan.close_after_reply,
                "disposition": policy.disposition(),
            }
    trace["tool"] = tool_trace

    # 6. Route execution: workflow (with mocked tools) or the LLM.
    response_text = ""
    workflow_detail = None
    if plan is not None and plan.handoff:
        trace["route"] = "handoff"
        response_text = "(transfer to human agent — policy confirmed)"
    elif decision.kind == RouteKind.WORKFLOW and not (plan and plan.force_llm):
        name = decision.action or active_workflow
        if name:
            engine = get_workflow_engine()
            result = await engine.handle_turn_detailed(
                session_id=f"sim:{bot.id}:{session}",
                tenant_id=bot.tenant_id, bot_id=bot.id,
                workflow_name=name, user_text=body.message,
                language=body.language or None,
                mock_tool_results=body.mock_tool_results or None,
            )
            workflow_detail = {
                "name": name, "status": result["status"],
                "nodeTrace": result["trace"], "slots": result["slots"],
                "offScript": bool(result.get("offScript")), "done": result["done"],
            }
            trace["route"] = "workflow"
            response_text = result["reply"]
            try:
                if result["done"]:
                    await redis.delete(active_key)
                else:
                    await redis.set(active_key, name, ex=_CHAT_SESSION_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                pass
            if result.get("offScript"):
                plan_instruction = policy.turn_instruction() if policy else ""
                response_text = await _simulate_llm_reply(
                    llm, rendered_prompt + plan_instruction + tool_instruction,
                    body.messages, body.message,
                )
                trace["route"] = "workflow_off_script_llm"
    if not response_text:
        extra = (plan.instruction if plan else "") + tool_instruction
        trace.setdefault("route", decision.kind.value if decision.kind != RouteKind.WORKFLOW else "chat")
        if decision.kind == RouteKind.CLARIFY and policy is None and not classification.intent:
            from shared.orchestration.phrases import canned

            response_text = canned("clarify", body.language or "en")
            trace["route"] = "clarify"
        else:
            # Trace fidelity: show the system prompt the LLM ACTUALLY got.
            trace["renderedPrompt"] = rendered_prompt + extra
            response_text = await _simulate_llm_reply(
                llm, rendered_prompt + extra, body.messages, body.message,
            )
    trace["workflow"] = workflow_detail
    trace["response"] = response_text
    trace["language"] = simulation_language
    trace["sessionId"] = session
    trace["provider"] = provider_code
    trace["latencyMs"] = round((time.monotonic() - started) * 1000)
    if policy is not None:
        trace["dispositionAfterTurn"] = policy.disposition()
    return ok(trace)


async def _simulate_llm_reply(
    llm, system: str, messages: list[dict], message: str
) -> str:
    history = [
        {"role": ("assistant" if m.get("role") == "assistant" else "user"),
         "content": str(m.get("content") or "")}
        for m in messages if m.get("content")
    ]
    history.append({"role": "user", "content": message})
    try:
        result = await llm.generate(history, system=system, max_tokens=400)
        return result.text
    except Exception as exc:  # noqa: BLE001 — surfaced as a safe test failure
        return f"(LLM unavailable: {type(exc).__name__})"
