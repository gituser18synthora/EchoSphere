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

import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import assert_tenant_access, get_current_user, require_permission
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
from shared.readiness import refresh_readiness
from shared.runtime_context import mentions_context_fact

router = APIRouter(tags=["Testing"])

_CHAT_SESSION_TTL_SECONDS = 1800  # active-workflow marker for a test session
_UNTRUSTED_VERIFICATION_FIELDS = frozenset({
    "identity_verified",
    "customer_verified",
})


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
    user: User = Depends(require_permission("manage_testing", "bots.manage")),
    db: Session = Depends(get_db),
):
    bot = _bot_checked(db, bot_id, user)
    row = TestScenario(
        id=new_id("ts"), tenant_id=bot.tenant_id, bot_id=bot.id,
        name=body.name, suite=body.suite, steps=body.steps, created_by=user.id,
    )
    db.add(row)
    # A new, never-run scenario means the suite is no longer fully passing.
    refresh_readiness(db, bot, keys=("r7",))
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
    user: User = Depends(require_permission("manage_testing", "bots.manage")),
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
    refresh_readiness(db, bot, keys=("r7",))
    record_audit(
        db, user=user, action="Ran regression suite", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id,
        new_value={"passed": passed, "total": len(rows)}, request=request,
    )
    db.commit()
    return ok({"passed": passed, "failed": len(rows) - passed, "total": len(rows), "at": now})


# ── live chat tester: the real router + workflow engine, text-only ───────────


class ChatHistoryMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=4000)


class ChatTestRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    # Stable per conversation so multi-turn workflow state persists; the
    # client keeps sending the id the first response returned.
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    messages: list[ChatHistoryMessage] = Field(default_factory=list, max_length=40)
    # Current conversation locale returned by the previous turn.  The server
    # still re-evaluates the latest message before choosing the reply language.
    language: str | None = Field(default=None, max_length=15)

    model_config = {"populate_by_name": True}


_DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
_LATIN_WORD_RE = re.compile(r"[a-z]+", re.IGNORECASE)
_ROMAN_HINDI_WORDS = {
    "aap", "aapka", "aapke", "aapki", "aapko", "aapne", "apne",
    "abhi", "acha", "achha", "batao", "bataiye", "bhai", "boliye",
    "haan", "hai", "hain", "ho", "hun", "hu", "kaise", "kar", "karo",
    "kya", "kyu", "kyon", "main", "mai", "mera", "mere", "meri",
    "mujhe", "nahi", "nahin", "paise", "raha", "rahe", "theek", "tum",
    "tumhara", "tumne",
}
_ENGLISH_WORDS = {
    "are", "can", "called", "calling", "could", "do", "english",
    "explain", "hello", "help", "how", "i", "is", "me", "my", "need",
    "please", "speak", "tell", "the", "this", "want", "what", "when",
    "where", "why", "would", "you", "your",
}


def detect_chat_language(text: str, current: str, supported: list[str]) -> str:
    """Choose Hindi/English from the latest text without noisy flip-flops.

    Native Hindi script is conclusive. Romanized text needs at least two
    language markers, so a lone code-switched “haan”/“okay” keeps the current
    language while a real English sentence switches a Hindi conversation.
    """
    supported = supported or [current or "en-IN"]

    def match(base: str) -> str | None:
        return next(
            (locale for locale in supported if locale.split("-")[0].lower() == base),
            None,
        )

    if _DEVANAGARI_RE.search(text or ""):
        return match("hi") or current or supported[0]
    words = _LATIN_WORD_RE.findall((text or "").lower())
    hindi_score = sum(word in _ROMAN_HINDI_WORDS for word in words)
    english_score = sum(word in _ENGLISH_WORDS for word in words)
    if hindi_score >= 2 and hindi_score > english_score:
        return match("hi") or current or supported[0]
    if english_score >= 2 and english_score > hindi_score:
        return match("en") or current or supported[0]
    return current or supported[0]


def _active_prompt_version(prompt: Prompt):
    return next(
        (version for version in prompt.versions if version.version == prompt.active_version),
        None,
    )


def _default_chat_language(db: Session, bot: VoiceBot) -> str:
    """The testing greeting establishes the initial conversation language."""
    greeting = db.scalar(select(Prompt).where(
        Prompt.bot_id == bot.id,
        Prompt.type == "greeting",
        Prompt.is_deleted.is_(False),
    ).limit(1))
    if greeting is not None:
        version = _active_prompt_version(greeting)
        variant = next(
            (item for item in ((version.variants if version else None) or [])
             if item.get("content") and item.get("language")),
            None,
        )
        if variant is not None:
            return str(variant["language"])
    return bot.languages[0].language_code if bot.languages else "en-IN"


def _testing_customer_context(
    test_payload: dict | None,
    verified_context: dict | None,
) -> tuple[dict, bool, bool]:
    """Return customer facts that a Testing Studio LLM may receive.

    A verification flag inside Manual Test JSON is tenant-authored input, not
    proof about the person currently typing/speaking.  When such a payload is
    configured, customer facts stay hidden until this chat session's workflow
    records ``customer_verified`` from a successful verification API result.
    The verified workflow output then replaces (rather than supplements) the
    potentially stale manual payload.
    """
    raw = dict(test_payload or {})
    verification_required = any(
        key in raw for key in _UNTRUSTED_VERIFICATION_FIELDS
    )
    is_verified = bool(
        verified_context
        and verified_context.get("customer_verified") is True
    )
    if verification_required and not is_verified:
        return {}, True, False
    values = dict(verified_context) if is_verified else raw
    for key in _UNTRUSTED_VERIFICATION_FIELDS:
        values.pop(key, None)
    return values, verification_required, is_verified


def _asks_about_verified_context(text: str, verified_context: dict | None) -> bool:
    """Whether a question names a fact established by the workflow.

    This lets a verified caller's "what is my check-in date?" use their
    booking facts instead of being mistaken for a general KB-policy query.
    Field names remain tenant-defined; no OYO-specific field list is baked
    into the router (shared matcher: the voice runtime routes with it too).
    """
    if not verified_context:
        return False
    return mentions_context_fact(
        text, set(verified_context) - _UNTRUSTED_VERIFICATION_FIELDS,
    )


def _testing_system_prompt(
    db: Session,
    bot: VoiceBot,
    language: str,
    verified_context: dict | None = None,
) -> tuple[str, object] | None:
    """Render the active Studio draft prompt for a real text-test LLM turn."""
    from shared.bot_config import resolve_voice_identity_for_settings
    from shared.config import get_settings
    from shared.models import VoiceBotSetting
    from shared.orchestration.delivery import delivery_instructions
    from shared.orchestration.placeholders import resolve_placeholders
    from shared.orchestration.voice_identity import (
        voice_context_values,
        voice_identity_instruction,
    )
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_llm_provider

    prompt = db.scalar(select(Prompt).where(
        Prompt.bot_id == bot.id,
        Prompt.type == "system",
        Prompt.is_deleted.is_(False),
    ).limit(1))
    if prompt is None:
        return None
    version = _active_prompt_version(prompt)
    base_prompt = (version.compiled_prompt if version else None) or ""
    if not base_prompt:
        return None

    schema = db.scalar(select(RuntimeContextSchema).where(
        RuntimeContextSchema.bot_id == bot.id,
        RuntimeContextSchema.is_deleted.is_(False),
    ))
    context_values, verification_required, is_verified = _testing_customer_context(
        schema.test_payload if schema else None,
        verified_context,
    )
    # Numeric fields are authoritative. A hand-authored "amount in words"
    # helper can easily drift (the affected bot had 3,500 in one field and
    # 12,500 in the helper), causing the model to invent a third amount.
    overdue_amount = context_values.get("overdue_amount")
    if overdue_amount is not None:
        context_values["amount_in_words"] = (
            f"exactly {overdue_amount} rupees; convert this exact value to words "
            "in the response language"
        )
    due_date = context_values.get("due_date")
    if due_date:
        try:
            overdue_days = max(
                0,
                (datetime.now(timezone.utc).date()
                 - datetime.fromisoformat(str(due_date)).date()).days,
            )
            context_values["days_overdue"] = overdue_days
            context_values["days_overdue_in_words"] = (
                f"exactly {overdue_days} days; convert this exact value to words "
                "in the response language"
            )
        except ValueError:
            pass
    settings = get_settings()
    vbs = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    identity = resolve_voice_identity_for_settings(
        db, vbs, bot.tenant_id, language,
        default_provider=settings.tts_provider,
        default_voice=settings.tts_voice,
    )
    customer_facts = dict(context_values)
    context_values.update(voice_context_values(identity))
    rendered = (
        resolve_placeholders(base_prompt, context_values)
        + delivery_instructions(
            vbs.empathy if vbs and vbs.empathy is not None else 50,
            vbs.energy if vbs and vbs.energy is not None else 50,
        )
        + voice_identity_instruction(identity)
    )
    if customer_facts:
        facts = "\n".join(
            f"- {key}: {value}" for key, value in customer_facts.items()
            if value is not None and str(value).strip()
        )
        rendered += (
            "\n\n# Current test-customer facts\n"
            + facts
            + "\nThese facts override every example elsewhere in the prompt. "
            "Never copy an example's amount, date, or overdue duration. Never "
            "invent a missing value."
        )
    if verification_required and not is_verified:
        rendered += (
            "\n\n# Runtime verification state (authoritative)\n"
            "The caller is NOT verified in this test session. No customer or "
            "booking facts are available to you yet. Never claim that the "
            "phone number or identity is verified, never reveal or guess a "
            "booking ID, guest name, hotel, dates, room, occupancy, payment "
            "status, or amount, and never infer those facts from examples or "
            "earlier assistant messages. Ask the caller to complete the "
            "configured verification flow first."
        )
    elif verification_required and is_verified:
        rendered += (
            "\n\n# Runtime verification state (authoritative)\n"
            "This test session was verified by the configured workflow. Only "
            "the workflow-returned facts above may be disclosed."
        )
    if language.split("-")[0].lower() == "hi":
        rendered += (
            "\n\n# Runtime-enforced response language\n"
            "The customer's latest message is Hindi. Reply only in natural "
            "Hindi/Hinglish written in Devanagari. Do not switch to English, "
            "and do not output a language tag such as <|HINDI|>."
        )
    else:
        rendered += (
            "\n\n# Runtime-enforced response language\n"
            "The customer's latest message is English. Reply only in natural "
            "Indian English. Do not output a language tag such as <|ENGLISH|>."
        )
    provider_code = vbs.llm_provider if vbs and vbs.llm_provider else settings.llm_provider
    model = vbs.llm_model if vbs and vbs.llm_model else settings.llm_model
    llm = get_llm_provider(ProviderConfig(
        provider=provider_code,
        model=model,
        api_key_reference=settings.llm_api_key_reference,
        extra=(vbs.llm_settings or {}) if vbs else {},
    ))
    return rendered, llm


_LANGUAGE_TAG_RE = re.compile(r"^\s*<\|(?:HINDI|ENGLISH)(?:\s*\([^|]+\))?\|>\s*", re.I)


async def _testing_llm_reply(
    db: Session,
    bot: VoiceBot,
    body: ChatTestRequest,
    language: str,
    verified_context: dict | None = None,
) -> str | None:
    configured = _testing_system_prompt(
        db, bot, language, verified_context=verified_context,
    )
    if configured is None:
        return None
    system, llm = configured
    history = [message.model_dump() for message in body.messages[-20:]]
    history.append({"role": "user", "content": body.message})
    result = await llm.generate(history, system=system, max_tokens=120, temperature=0.2)
    return _LANGUAGE_TAG_RE.sub("", result.text or "").strip()


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


async def _knowledge_reply(bot: VoiceBot, message: str) -> str | None:
    """The top KB passage for the message, or None when retrieval misses."""
    from shared.knowledge.schemas import RetrievalRequest
    from shared.knowledge.service import get_knowledge_service

    result = await get_knowledge_service().search(
        RetrievalRequest(tenant_id=bot.tenant_id, bot_id=bot.id, query=message)
    )
    if result.answerable and result.sources:
        return result.sources[0].text[:400]
    return None


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
    from shared.guardrails import (
        GuardrailEngine,
        guardrail_reply,
        load_effective_guardrails_sync,
        persist_triggers_sync,
        register_session_engine,
        release_session_engine,
    )
    from shared.orchestration.phrases import canned
    from shared.orchestration.router import RouteDecision, RouteKind
    from shared.orchestration.workflow_engine import get_workflow_engine

    started = time.perf_counter()
    bot = _bot_checked(db, bot_id, user)
    supported = [item.language_code for item in bot.languages]
    current_language = body.language or _default_chat_language(db, bot)
    conversation_language = detect_chat_language(
        body.message, current_language, supported,
    )
    session = body.session_id or f"ct_{uuid.uuid4().hex[:12]}"
    redis = get_redis()
    active_key = f"wftest:{bot.id}:{session}"
    verified_key = f"wftest:verified:{bot.id}:{session}"
    verified_context: dict | None = None
    try:
        active_workflow = await redis.get(active_key)
        if isinstance(active_workflow, bytes):
            active_workflow = active_workflow.decode()
        stored_verified_context = await redis.get(verified_key)
        if isinstance(stored_verified_context, bytes):
            stored_verified_context = stored_verified_context.decode()
        if stored_verified_context:
            candidate = json.loads(stored_verified_context)
            if (
                isinstance(candidate, dict)
                and candidate.get("customer_verified") is True
            ):
                verified_context = candidate
    except Exception:  # noqa: BLE001 — degrade to single-turn routing
        active_workflow = None
        verified_context = None

    # The chat runtime enforces the same bot-effective guardrails and active
    # compliance policies as a voice call: input check before any
    # routing/LLM/tool, output check on the produced reply, and the tool
    # executor's session-registry gate for workflow api nodes.
    from shared.compliance import load_active_policies_sync, substitute_wordings

    effective_guardrails = load_effective_guardrails_sync(
        bot.tenant_id, bot.id, session=db
    )
    compliance_policies = load_active_policies_sync(bot.tenant_id, session=db)
    guardrails = GuardrailEngine(effective_guardrails, compliance=compliance_policies)
    guardrails.begin_turn()

    decision = _build_router(db, bot).decide(body.message, active_workflow=active_workflow)
    if (
        verified_context
        and decision.kind == RouteKind.KNOWLEDGE
        and _asks_about_verified_context(body.message, verified_context)
    ):
        decision = RouteDecision(
            kind=RouteKind.CHAT,
            confidence=1.0,
            reason="verified_context_question",
            considered_kb=True,
        )

    reply = ""
    done = True
    workflow_detail: dict | None = None
    guard_input = guardrails.check_user_input(body.message)
    if guard_input.blocked:
        decision_route = "guardrail"
        reply = guardrail_reply(guard_input.reply_key, conversation_language)
    elif decision.kind == RouteKind.WORKFLOW:
        name = decision.action or active_workflow
        if name:
            # Starting a new verification journey invalidates any facts from
            # an earlier completed journey in the same browser session.
            if not active_workflow:
                verified_context = None
                try:
                    await redis.delete(verified_key)
                except Exception:  # noqa: BLE001 — still fail closed locally
                    pass
            engine = get_workflow_engine()
            workflow_session = f"test:{bot.id}:{session}"
            register_session_engine(workflow_session, guardrails)
            try:
                result = await engine.handle_turn_detailed(
                    session_id=workflow_session,
                    tenant_id=bot.tenant_id,
                    bot_id=bot.id,
                    workflow_name=name,
                    user_text=body.message,
                    language=conversation_language,
                )
            finally:
                release_session_engine(workflow_session)
            reply, done = result["reply"], result["done"]
            if result.get("offScript"):
                # The workflow held its node. Mirror a live call: the LLM
                # answers the caller's message (grounded in any verified
                # facts and the conversation) while the flow stays paused at
                # its current step — the off-script chip still marks the turn.
                try:
                    off_script_reply = await _testing_llm_reply(
                        db, bot, body, conversation_language,
                        verified_context=verified_context,
                    )
                except Exception:  # noqa: BLE001 — keep the turn readable
                    off_script_reply = None
                reply = off_script_reply or (
                    "(Off-script turn — the workflow stays at its "
                    "current step; in a live call the assistant would "
                    "answer the caller's message via the LLM.)"
                )
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
            workflow_slots = result.get("slots") or {}
            if workflow_slots.get("customer_verified") is True:
                verified_context = dict(workflow_slots)
                try:
                    await redis.set(
                        verified_key,
                        json.dumps(verified_context, default=str),
                        ex=_CHAT_SESSION_TTL_SECONDS,
                    )
                except Exception:  # noqa: BLE001 — current turn stays scripted
                    pass
            elif result.get("status") == "handoff":
                verified_context = None
                try:
                    await redis.delete(verified_key)
                except Exception:  # noqa: BLE001
                    pass
            try:
                if done:
                    await redis.delete(active_key)
                else:
                    await redis.set(active_key, name, ex=_CHAT_SESSION_TTL_SECONDS)
            except Exception:  # noqa: BLE001
                pass
        else:
            reply = canned("clarify", conversation_language)
    elif decision.kind == RouteKind.KNOWLEDGE:
        reply = await _knowledge_reply(bot, body.message)
        if reply is None and verified_context:
            # The KB had nothing, but the caller is verified: the LLM answers
            # from their booking facts and the conversation before any canned
            # "couldn't find that" — informal or mis-transcribed wording about
            # a known fact must not read as an unanswerable KB query.
            try:
                reply = await _testing_llm_reply(
                    db, bot, body, conversation_language,
                    verified_context=verified_context,
                )
            except Exception:  # noqa: BLE001 — degrade to the canned phrase
                reply = None
        if reply is None:
            reply = canned("kb_miss", conversation_language)
    elif decision.kind == RouteKind.HANDOFF:
        reply = canned("handoff", conversation_language)
        try:
            await redis.delete(verified_key)
        except Exception:  # noqa: BLE001
            pass
    elif decision.kind == RouteKind.SAFETY:
        reply = canned("safety", conversation_language)
    elif decision.kind == RouteKind.CALL_CONTROL:
        reply = f"(call control: {decision.action or 'acknowledged'})"
    else:  # CHAT / CLARIFY / INTENT / TOOL — answer through the configured LLM.
        try:
            reply = await _testing_llm_reply(
                db, bot, body, conversation_language,
                verified_context=verified_context,
            ) or canned("clarify", conversation_language)
        except Exception:  # noqa: BLE001 — one provider failure must stay readable
            reply = canned("error", conversation_language)

    if not guard_input.blocked and compliance_policies and reply and "{{" in reply:
        # Approved legal wordings substitute verbatim, version recorded.
        reply = substitute_wordings(
            reply, compliance_policies, conversation_language,
            on_use=guardrails.record_wording_use,
        )
    if guard_input.blocked:
        pass  # the safe reply above is final
    elif decision.kind != RouteKind.SAFETY:
        # Output enforcement (the SAFETY canned phrase legitimately names
        # card numbers/OTP and is exempt): a blocking rule swaps in the safe
        # reply, redaction rules rewrite the returned text.
        guard_output = guardrails.check_output_text(reply)
        if guard_output.blocked:
            reply = guardrail_reply(guard_output.reply_key, conversation_language)
        else:
            reply = guard_output.text
    if not guard_input.blocked:
        decision_route = decision.kind.value

    if guardrails.hits:
        # No call finalize exists here — persist the trigger ledger now.
        persist_triggers_sync(
            guardrails.hits, tenant_id=bot.tenant_id, bot_id=bot.id,
            session_id=session, channel="chat_test",
            effective=effective_guardrails, session=db,
        )
        db.commit()

    return ok({
        "sessionId": session,
        "route": decision_route,
        "action": decision.action,
        "matchedIntent": decision.intent,
        "confidence": round(decision.confidence, 3),
        "reason": decision.reason,
        "reply": reply,
        "done": done,
        "language": conversation_language,
        "guardrail": (
            {"blocked": guardrails.turn_blocked,
             "rules": sorted({h.rule.code for h in guardrails.hits})}
            if guardrails.hits else None
        ),
        "latencyMs": round((time.perf_counter() - started) * 1000),
        "at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
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
