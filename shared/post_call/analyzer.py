"""One bounded LLM analysis per completed call → validated PostCallAnalysis.

A single structured request covers summary, outcome, commitments, slots and
the proposed Next Best Action (the deterministic layer in
:mod:`shared.post_call.nba` then reconciles the proposal with recorded
platform state). The prompt is built entirely from the bot's configured goal
policy plus the call's own transcript/final state — no tenant wording lives
here.

Failure never raises into the processor's control flow:
``analyze_call`` returns None (the caller retries or falls back) and
``fallback_analysis`` builds an honest deterministic record from what the
platform itself observed, so the next call still gets safe context even when
the model is down.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from shared.orchestration.goal_engine import BotGoalPolicy
from shared.orchestration.time_context import resolve_zone
from shared.post_call.nba import allowed_next_actions, decide_next_best_action
from shared.post_call.schema import (
    NextBestAction,
    PostCallAnalysis,
    parse_analysis,
)
from shared.post_call.structured import (
    derive_structured_fields,
    summary_fields_prompt_block,
)

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)
_MAX_TURN_CHARS = 300
_MAX_TRANSCRIPT_TURNS = 60
_MAX_TOKENS = 900
_MAX_DETAIL_TURN_CHARS = 4000
_MAX_DETAIL_OUTPUT_TOKENS = 2600
_DEFAULT_TIMEOUT_SECONDS = 25.0

# Event kinds worth showing the analyst (verified facts and platform
# decisions); everything else in the event stream is diagnostics.
_FACT_EVENT_KINDS = (
    "tool_executed", "payment_verification", "call_control", "handoff",
    "intent_classified", "orchestration_decision", "language_detected",
)
_MAX_FACT_EVENTS = 12


def build_analysis_llm(config):
    """The LLM the post-call analysis runs on.

    Prefers the bot's configured orchestration engine (same override the
    Goal Engine uses); falls back to the conversation LLM. Deliberately free
    of any voice-runtime imports so the processor can run in any process.
    """
    from shared.providers.base import ProviderConfig
    from shared.providers.factory import get_llm_provider

    llm_conf = dict(config.llm or {})
    orchestration = llm_conf.get("orchestration") or {}
    reuse = bool(orchestration.get("provider")) and orchestration.get(
        "source", "bot",
    ) in ("bot", "reasoning_fallback")
    chosen = orchestration if reuse else llm_conf
    # The decision engine's service tier (e.g. "fast", billed at a premium)
    # buys per-turn latency; post-call analysis is not latency-bound.
    extra = {k: v for k, v in (chosen.get("extra") or {}).items() if k != "service_tier"}
    return get_llm_provider(ProviderConfig(
        provider=chosen.get("provider") or "openai",
        model=chosen.get("model") or "",
        api_key_reference=chosen.get("api_key_reference") or "",
        timeout_seconds=float(chosen.get("timeout_seconds")
                              or _DEFAULT_TIMEOUT_SECONDS),
        extra=extra,
    ))


def _transcript_block(turns: list[dict], *, detailed: bool = False) -> str:
    lines: list[str] = []
    # Opt-in extraction needs early corrections and full addresses. Keep the
    # existing compact context for all other bots.
    for index, turn in enumerate(turns if detailed else turns[-_MAX_TRANSCRIPT_TURNS:]):
        role = "Customer" if turn.get("role") == "user" else "Bot"
        text = " ".join(str(turn.get("text") or "").split())[
            :_MAX_DETAIL_TURN_CHARS if detailed else _MAX_TURN_CHARS
        ]
        if text:
            prefix = f"[turn {index + 1}] " if detailed else ""
            lines.append(f"{prefix}{role}: {text}")
    return "\n".join(lines) or "(no spoken turns)"


def _fact_events_block(events: list[dict]) -> str:
    lines: list[str] = []
    for event in events:
        kind = event.get("kind")
        if kind not in _FACT_EVENT_KINDS:
            continue
        detail = {
            k: v for k, v in event.items()
            if k not in ("kind", "at") and v not in (None, "", [], {})
        }
        lines.append(f"- {kind}: {json.dumps(detail, ensure_ascii=False, default=str)[:220]}")
        if len(lines) >= _MAX_FACT_EVENTS:
            break
    return "\n".join(lines)


def _policy_block(policy: BotGoalPolicy) -> str:
    lines = [f"- Role: {policy.role or 'voice assistant'}"]
    if policy.domain:
        lines.append(f"- Domain: {policy.domain}")
    for goal in policy.goals:
        if goal.description:
            lines.append(
                f"- Goal: {goal.description}"
                + (f" (complete when: {goal.completion})" if goal.completion else "")
            )
    if policy.slots:
        lines.append(
            "- Values the bot collects (use these EXACT names in "
            "collected_slots / missing_slots): "
            + ", ".join(spec.name for spec in policy.slots)
        )
    if policy.completion_criteria:
        lines.append("- Completion criteria: " + "; ".join(policy.completion_criteria[:6]))
    return "\n".join(lines)


def _build_system(policy: BotGoalPolicy, *, reference: datetime) -> str:
    # Opt-in date extraction uses the configured call locale, not the UTC
    # day of a delayed worker. Individual field references are also rendered.
    local_date_spec = next((
        spec for spec in policy.summary_fields
        if spec.evidence_required and spec.type == "date" and spec.timezone
    ), None)
    if local_date_spec is not None:
        reference = reference.astimezone(resolve_zone(local_date_spec.timezone))
    actions = ", ".join(allowed_next_actions(policy))
    detailed = any(spec.evidence_required for spec in policy.summary_fields)
    weekday = reference.strftime("%A")
    structured_block = summary_fields_prompt_block(policy, reference=reference)
    structured_json = (
        '  "structured_fields": {<field name>: <allowed value or null>},\n'
        if structured_block else ""
    )
    if detailed:
        field_keys = ", ".join(
            f'"{spec.name}": <allowed value or null>'
            for spec in policy.summary_fields if spec.allow_llm and not spec.derived_from
        )
        structured_json = '  "structured_fields": {' + field_keys + '},\n'
        evidence_keys = ", ".join(
            f'"{spec.name}": <Customer turn number as integer, or null>'
            for spec in policy.summary_fields if spec.evidence_required and spec.allow_llm
        )
        structured_json += (
            '  "structured_field_evidence": {' + evidence_keys + '},\n'
        )
    return (
        "You are the POST-CALL analyst for a phone voice agent. The call has "
        "ended. From the transcript and the recorded platform facts, produce "
        "ONE JSON object describing what happened — for the agent that will "
        "handle this customer's NEXT call. The customer may have spoken "
        "Hindi, English or mixed Hinglish.\n\n"
        "# The bot whose call you are analyzing\n"
        + _policy_block(policy)
        + (("\n\n" + structured_block) if structured_block and not detailed else "")
        + f"\n\n# Time reference\nThe call happened on {reference.date().isoformat()} "
        f"({weekday}). Resolve every relative date the customer used "
        "(tomorrow, Monday, कल, परसों, अगले हफ़्ते…) to an ABSOLUTE ISO date "
        "using this reference.\n\n"
        "# Output — ONLY this JSON object, no prose, no fences\n"
        "{\n"
        '  "call_outcome": <short snake_case label for how the call ended, '
        "e.g. promise_to_pay, refusal, callback_requested, wrong_person, "
        "goal_completed, incomplete — pick what fits THIS domain>,\n"
        '  "summary": <3-6 factual sentences: what the bot asked, what the '
        "customer said, what was agreed or refused, and what remains open. "
        "Written in English regardless of the call language.>,\n"
        '  "customer_intent": <short snake_case label>,\n'
        '  "customer_sentiment": <short snake_case label, e.g. cooperative, '
        "firm_refusal, frustrated, neutral>,\n"
        '  "customer_commitments": [{"type": <payment|callback|document|'
        "appointment|other>, \"description\": <what was committed, their "
        'words>, "amount": <number or null>, "currency": <"INR" etc or "">, '
        '"due_date": <ABSOLUTE "YYYY-MM-DD" or null>, "raw_due_expression": '
        '<the words the customer used for the date, verbatim>, "status": '
        '"promised"}],\n'
        '  "objections": [<customer objections, short>],\n'
        '  "important_facts": [<facts worth knowing next call, short>],\n'
        '  "resolved_items": [<items settled this call, snake_case>],\n'
        '  "unresolved_items": [<items still open, snake_case>],\n'
        '  "collected_slots": {<slot name>: <value the customer provided>},\n'
        '  "missing_slots": [<configured slots still missing>],\n'
        + structured_json
        + '  "next_best_action": {"action": <one of: ' + actions + ">, "
        '"reason": <short>, "priority": <low|medium|high|urgent>, '
        '"recommended_at": <ISO datetime or null>},\n'
        '  "follow_up_required": <bool>,\n'
        '  "confidence": <0..1>,\n'
        '  "dominant_language": <the language the CUSTOMER mostly spoke: '
        'locale like "hi-IN"/"en-IN">,\n'
        '  "last_customer_language": <language of their final turns>\n'
        "}\n\n"
        "# Rules (non-negotiable)\n"
        "- Facts only. Never invent amounts, dates, names or agreements the "
        "transcript does not contain.\n"
        "- A customer CLAIMING something happened (a payment, a submission) "
        "is a claim, not a verified fact — unless the platform facts below "
        "show a successful backend verification, keep verification in "
        "unresolved_items and never report the claim as completed.\n"
        + (
            "- For configured structured_fields, clear customer-reported claims "
            "MUST still be extracted as the field's rules define, with customer evidence. "
            "They report what the customer said, not backend verification. "
            "This caveat must not turn a clear customer statement into null. "
            "Emit EVERY listed structured_fields key, including nulls. Every "
            "non-null evidenceRequired field MUST have its OWN source Customer "
            "turn number in structured_field_evidence; repeat the number under "
            "multiple keys when one customer turn supports several fields. "
            "Never cite a Bot turn.\n"
            if detailed else ""
        )
        + "- Commitments: capture amount/date exactly as spoken; resolve the "
        "date to ISO using the time reference; status is always 'promised' "
        "for new commitments.\n"
        "- Choose next_best_action ONLY from the allowed list.\n"
        "- No sensitive values: never include OTPs, card numbers, or full "
        "account numbers anywhere in the output."
        + (("\n\n" + structured_block) if detailed else "")
    )


async def analyze_call(
    llm,
    *,
    policy: BotGoalPolicy,
    turns: list[dict],
    events: list[dict],
    final_state: dict,
    reference: datetime,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[PostCallAnalysis | None, tuple[int, int] | None]:
    """Run the single post-call analysis. Returns (analysis, token usage)."""
    state_lines = [
        f"- {key}: {json.dumps(value, ensure_ascii=False, default=str)[:200]}"
        for key, value in (final_state or {}).items()
        if value not in (None, "", [], {})
    ]
    facts = _fact_events_block(events or [])
    detailed = any(spec.evidence_required for spec in policy.summary_fields)
    user = (
        "# Transcript\n" + _transcript_block(turns, detailed=detailed)
        + "\n\n# Platform facts recorded during the call (authoritative)\n"
        + ("\n".join(state_lines) or "- (none)")
        + (("\n" + facts) if facts else "")
        + "\n\nProduce the JSON object now."
    )
    try:
        result = await asyncio.wait_for(
            llm.generate(
                [{"role": "user", "content": user}],
                system=_build_system(policy, reference=reference),
                temperature=0.0,
                max_tokens=_MAX_DETAIL_OUTPUT_TOKENS if detailed else _MAX_TOKENS,
            ),
            timeout=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — the processor retries/falls back
        logger.warning("post-call analysis LLM failed: %s", exc)
        return None, None
    usage = (
        int(getattr(result, "input_tokens", 0) or 0),
        int(getattr(result, "output_tokens", 0) or 0),
    )
    raw = (getattr(result, "text", "") or "").strip()
    match = _JSON_BLOCK.search(raw)
    if match is None:
        logger.warning("post-call analysis returned no JSON: %r", raw[:120])
        return None, usage
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        logger.warning("post-call analysis returned invalid JSON: %r", raw[:120])
        return None, usage
    # Turn references avoid asking the model to reproduce/translate quotes.
    # Expand only genuine customer references; persistence keeps transcript
    # text, not model-written evidence. Legacy quote output remains supported.
    raw_evidence = parsed.get("structured_field_evidence", parsed.get("structuredFieldEvidence")) if isinstance(parsed, dict) else None
    if isinstance(raw_evidence, dict):
        expanded = {}
        for name, value in raw_evidence.items():
            if type(value) is int:
                turn = turns[value - 1] if 1 <= value <= len(turns) else {}
                if turn.get("role") == "user":
                    expanded[name] = " ".join(str(turn.get("text") or "").split())[:_MAX_DETAIL_TURN_CHARS]
            elif isinstance(value, str):
                expanded[name] = value
        parsed["structured_field_evidence"] = expanded
    analysis = parse_analysis(parsed)
    if analysis is None:
        return None, usage
    analysis.resolve_dates(reference=reference)
    return analysis, usage


def fallback_analysis(
    *,
    final_state: dict,
    policy: BotGoalPolicy | None = None,
    reference: datetime | None = None,
) -> PostCallAnalysis:
    """Deterministic memory when analysis is unavailable.

    Built only from what the platform itself recorded (disposition, call
    state, language) — honest, low-confidence, never invented.
    """
    reference = reference or datetime.now(timezone.utc)
    disposition = str(final_state.get("disposition") or "").strip().lower()
    analysis = PostCallAnalysis(
        call_outcome=disposition or str(final_state.get("end_reason") or "incomplete"),
        summary="",
        confidence=0.0,
        source="fallback",
        dominant_language=str(final_state.get("language") or ""),
        last_customer_language=str(final_state.get("language") or ""),
    )
    if policy is not None and policy.summary_fields:
        # The workflow slots are platform-recorded facts — safe to report even
        # without an analysis; unknown fields stay None.
        derived = derive_structured_fields(
            policy, final_state.get("workflow_slots") or {}
        )
        analysis.structured_fields = derived
        analysis.structured_field_sources = {
            name: "workflow" for name, value in derived.items() if value is not None
        }
    analysis.next_best_action = decide_next_best_action(
        analysis,
        policy=policy,
        disposition=disposition or None,
        call_state=final_state.get("call_state") or {},
        escalated=bool(final_state.get("escalated")),
        workflow_active=bool(final_state.get("workflow_active")),
        now=reference,
    )
    if analysis.next_best_action.action in (
        "do_not_contact", "close_goal_completed", "no_action",
    ):
        analysis.follow_up_required = False
    return analysis


__all__ = [
    "analyze_call",
    "build_analysis_llm",
    "fallback_analysis",
    "NextBestAction",
]
