"""Next Best Action — deterministic, configuration-driven reconciliation.

The analysis LLM *proposes* a next action; this layer decides the one that is
stored. Rules here are domain-neutral and keyed on platform facts the runtime
itself recorded (disposition vocabulary, verified tool outcomes, escalation,
workflow position, commitments with absolute dates) — never on tenant wording.
What each action operationally means is the tenant's campaign tooling's
business; a bot's goal policy may extend the allowed vocabulary
(``goal_policy.next_actions``) but shared code never invents domain logic for
those extensions.

Precedence: verified/recorded platform state > commitments the analysis
captured > the LLM's own proposal (accepted only when it names an allowed
action).
"""

from datetime import date, datetime, time, timezone

from shared.post_call.schema import (
    PLATFORM_NEXT_ACTIONS,
    NextBestAction,
    PostCallAnalysis,
)

# Default follow-up moment on a commitment's promised day. Stored in UTC;
# 04:30 UTC = 10:00 IST, mid-morning for the platform's calling base.
_FOLLOW_UP_UTC_TIME = time(4, 30)

# Platform disposition values the runtime itself produces (see
# SessionRecorder.disposition / CollectionCallPolicy.disposition and the
# brain's DNC fast path). Grouped by what they mean for follow-up.
_DNC_DISPOSITIONS = frozenset({"do_not_call", "do_not_contact"})
_COMPLETED_DISPOSITIONS = frozenset({
    "completed", "resolved", "payment_verified", "goal_completed",
})
_CALLBACK_DISPOSITIONS = frozenset({"callback_requested"})
_CLAIM_DISPOSITIONS = frozenset({"payment_claimed"})


def allowed_next_actions(policy=None) -> tuple[str, ...]:
    """Platform vocabulary plus the bot's configured extensions."""
    extra: list[str] = []
    for name in getattr(policy, "next_actions", None) or []:
        cleaned = str(name).strip().lower().replace(" ", "_")[:60]
        if cleaned and cleaned not in PLATFORM_NEXT_ACTIONS and cleaned not in extra:
            extra.append(cleaned)
    return PLATFORM_NEXT_ACTIONS + tuple(extra)


def _commitment_follow_up(analysis: PostCallAnalysis, now: datetime):
    """(commitment, recommended_at, overdue) for the earliest open commitment."""
    open_dated = [
        c for c in analysis.customer_commitments if c.is_open() and c.due_date
    ]
    if not open_dated:
        return None, None, False
    earliest = min(open_dated, key=lambda c: c.due_date)
    due: date = earliest.due_date
    recommended = datetime.combine(due, _FOLLOW_UP_UTC_TIME, tzinfo=timezone.utc)
    overdue = due < now.date()
    return earliest, (now if overdue else recommended), overdue


def decide_next_best_action(
    analysis: PostCallAnalysis,
    *,
    policy=None,
    disposition: str | None = None,
    call_state: dict | None = None,
    escalated: bool = False,
    workflow_active: bool = False,
    now: datetime | None = None,
) -> NextBestAction:
    """The stored Next Best Action for one completed call.

    ``analysis.next_best_action`` is the LLM's proposal; recorded platform
    facts override it, and an unknown proposed action degrades to a safe
    follow-up instead of inventing vocabulary.
    """
    now = now or datetime.now(timezone.utc)
    call_state = call_state or {}
    disposition = (disposition or analysis.call_outcome or "").strip().lower()
    allowed = allowed_next_actions(policy)
    proposal = analysis.next_best_action

    def rules(action: str, reason: str, *, priority: str = "medium",
              recommended_at: datetime | None = None) -> NextBestAction:
        return NextBestAction(
            action=action, reason=reason, priority=priority,
            recommended_at=recommended_at, source="rules",
        )

    # 1. Consent revoked — absolute, nothing may schedule another contact.
    if disposition in _DNC_DISPOSITIONS or (
        str(call_state.get("last_disposition", "")).lower() in _DNC_DISPOSITIONS
    ):
        return rules(
            "do_not_contact",
            "The customer revoked contact consent on this call.",
            priority="urgent",
        )

    # 2. The call was handed to a human — the human owns the next step.
    if escalated or disposition in ("escalated", "handoff"):
        return rules(
            "escalate_to_human",
            "The call was escalated to a human agent; ownership moved there.",
            priority="high",
        )

    # 3. A claim was recorded but never verified by a backend check. The
    # verification outcome is a runtime fact (tool result / policy record) —
    # a summary can never upgrade it.
    verification = call_state.get("payment_verification")
    verification_outcome = str(
        (verification or {}).get("outcome")
        if isinstance(verification, dict)
        else call_state.get("verification_outcome") or ""
    ).lower()
    claim_unverified = (
        disposition in _CLAIM_DISPOSITIONS
        or verification_outcome in ("pending", "unverified", "claim_recorded",
                                    "not_found", "failed")
    ) and verification_outcome not in ("verified", "completed")
    if claim_unverified:
        return rules(
            "verify_previous_payment",
            "The customer claimed a prior payment that no backend check "
            "verified; verify before any further ask.",
            priority="high",
            recommended_at=now,
        )

    # 4. Verified completion / goal met: close, no follow-up.
    if disposition in _COMPLETED_DISPOSITIONS or (
        verification_outcome in ("verified", "completed")
        and not analysis.unresolved_items
    ):
        return rules(
            "close_goal_completed",
            "The call's goal completed against verified state.",
            priority="low",
        )

    # 5. An open commitment with an absolute date drives timed follow-up.
    commitment, recommended_at, overdue = _commitment_follow_up(analysis, now)
    if commitment is not None:
        if overdue:
            return rules(
                "retry_commitment",
                f"The committed date ({commitment.due_date.isoformat()}) has "
                "already passed without confirmation.",
                priority="high",
                recommended_at=recommended_at,
            )
        return rules(
            "follow_up_on_commitment",
            "Follow up on the commitment the customer made "
            f"({commitment.description or commitment.type}).",
            priority="medium",
            recommended_at=recommended_at,
        )

    # 6. An explicitly requested callback is honored at the asked time.
    if disposition in _CALLBACK_DISPOSITIONS or bool(
        call_state.get("callback_requested")
    ):
        return rules(
            "schedule_callback",
            "The customer asked to be called back.",
            priority="medium",
            recommended_at=proposal.recommended_at,
        )

    # 7. The call ended mid-workflow with the goal still open: resume it.
    if workflow_active and analysis.unresolved_items:
        return rules(
            "continue_pending_workflow",
            "The call ended before the active flow finished; resume from "
            "the pending step.",
            priority="medium",
        )

    # 8. Otherwise the LLM's proposal stands — if it names an allowed action.
    if proposal.action in allowed:
        return NextBestAction(
            action=proposal.action,
            reason=proposal.reason
            or "Proposed by post-call analysis within the configured actions.",
            priority=proposal.priority,
            recommended_at=proposal.recommended_at,
            source="llm",
        )
    return rules(
        "follow_up_later",
        f"Analysis proposed an unconfigured action ('{proposal.action}'); "
        "defaulting to a standard follow-up.",
    )
