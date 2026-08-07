"""Load the latest relevant conversation memory at the start of a new call.

Scope is always tenant + bot (the same scoping the platform's customer
context rows use — memory must never leak across tenants, and a phone number
alone is never a lookup key: the tail only matters INSIDE the tenant+bot
scope). Resolution mirrors how the call itself resolved the customer:

1. the runtime context record the call is running against,
2. the legacy customer context row,
3. the caller's trailing digits (fallback, tenant+bot scoped).

Race safety: only rows with a produced memory are returned — an immediately
recalled customer whose previous call is still ``processing`` simply gets the
memory of the call before it (or none), never a crash and never a wait.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import or_, select

from shared.customer_context import phone_tail
from shared.db.mysql import get_sessionmaker
from shared.models import ConversationMemory
from shared.post_call.schema import PostCallAnalysis

logger = logging.getLogger(__name__)

_LOAD_TIMEOUT_SECONDS = 3.0
_MAX_PROMPT_ITEMS = 6


@dataclass
class PreviousCallMemory:
    """The structured memory of the customer's most recent analyzed call."""

    conversation_id: str
    session_id: str
    started_at: datetime | None
    status: str  # completed | failed (failed = deterministic fallback memory)
    call_outcome: str
    summary: str
    analysis: PostCallAnalysis
    next_action: str
    next_action_reason: str
    follow_up_at: datetime | None
    language: str
    dominant_language: str
    matched_by: str = "record"
    open_commitments: list[dict] = field(default_factory=list)

    def preferred_language(self) -> str:
        """Locale to START the next call in (per-turn switching still owns
        the conversation after the first customer turn)."""
        return self.analysis.last_customer_language or self.dominant_language or ""

    def prompt_section(self) -> str:
        """System-prompt block for the next call.

        Context, not truth: the wording makes precedence explicit so the
        current conversation, current workflow state and current verified
        tool data always outrank this block.
        """
        analysis = self.analysis
        lines = ["\n\n# Previous conversation memory (context — NOT current truth)"]
        when = self.started_at.strftime("%Y-%m-%d %H:%M UTC") if self.started_at else "recently"
        lines.append(f"- This caller spoke with this service before ({when}).")
        if self.call_outcome:
            lines.append(f"- Last call outcome: {self.call_outcome}")
        if self.summary:
            lines.append(f"- What happened: {self.summary}")
        for commitment in self.open_commitments[:3]:
            piece = commitment.get("description") or commitment.get("type") or "commitment"
            amount = commitment.get("amount")
            due = commitment.get("due_date")
            extras = []
            if amount:
                extras.append(f"amount {amount}")
            if due:
                extras.append(f"due {due}")
            lines.append(
                "- Open commitment from last call: "
                + piece + (f" ({', '.join(extras)})" if extras else "")
            )
        if analysis.important_facts:
            lines.append(
                "- Important context: "
                + "; ".join(analysis.important_facts[:_MAX_PROMPT_ITEMS])
            )
        if analysis.unresolved_items:
            lines.append(
                "- Still pending from last call: "
                + ", ".join(analysis.unresolved_items[:_MAX_PROMPT_ITEMS])
            )
        if analysis.objections:
            lines.append(
                "- Objections raised last time: "
                + "; ".join(analysis.objections[:3])
            )
        if self.next_action:
            lines.append(
                f"- Recommended continuation: {self.next_action}"
                + (f" — {self.next_action_reason}" if self.next_action_reason else "")
            )
        lines.append(
            "- How to use this: continue naturally from where the last call "
            "left off — briefly acknowledge the relevant previous context "
            "instead of restarting the script, and never re-ask what is "
            "already resolved above unless verification requires it."
        )
        lines.append(
            "- Precedence: what the caller says NOW, the current workflow "
            "state, and anything verified by a tool THIS call always "
            "override this memory. If they contradict it, follow the "
            "current information and update your understanding."
        )
        return "\n".join(lines)

    def live_state_entry(self) -> str:
        """Compact one-line form for the Goal Engine's live-state block."""
        parts = []
        if self.call_outcome:
            parts.append(f"outcome={self.call_outcome}")
        for commitment in self.open_commitments[:2]:
            bit = commitment.get("type") or "commitment"
            if commitment.get("amount"):
                bit += f" {commitment['amount']}"
            if commitment.get("due_date"):
                bit += f" due {commitment['due_date']}"
            parts.append(f"open commitment: {bit}")
        if self.analysis.unresolved_items:
            parts.append("pending: " + ", ".join(self.analysis.unresolved_items[:4]))
        return "; ".join(parts) or "previous call on record"


def _row_to_memory(row: ConversationMemory, matched_by: str) -> PreviousCallMemory | None:
    analysis = PostCallAnalysis.model_validate(row.memory or {})
    open_commitments = [
        c.model_dump(mode="json")
        for c in analysis.customer_commitments
        if c.is_open()
    ]
    return PreviousCallMemory(
        conversation_id=row.conversation_id,
        session_id=row.session_id,
        started_at=row.generated_at or row.created_at,
        status=row.status,
        call_outcome=row.call_outcome or analysis.call_outcome or "",
        summary=row.summary or analysis.summary or "",
        analysis=analysis,
        next_action=row.next_action or analysis.next_best_action.action or "",
        next_action_reason=analysis.next_best_action.reason or "",
        follow_up_at=row.follow_up_at,
        language=row.language or "",
        dominant_language=row.dominant_language or "",
        matched_by=matched_by,
        open_commitments=open_commitments,
    )


def _load_sync(
    tenant_id: str,
    bot_id: str,
    *,
    runtime_context_record_id: str | None,
    customer_context_id: str | None,
    phone: str | None,
    exclude_session_id: str | None,
) -> PreviousCallMemory | None:
    tail = phone_tail(phone or "")
    matchers = []
    if runtime_context_record_id:
        matchers.append(
            ConversationMemory.runtime_context_record_id == runtime_context_record_id
        )
    if customer_context_id:
        matchers.append(ConversationMemory.customer_context_id == customer_context_id)
    if tail:
        matchers.append(ConversationMemory.phone_tail == tail)
    if not matchers:
        return None
    session = get_sessionmaker()()
    try:
        query = (
            select(ConversationMemory)
            .where(
                ConversationMemory.tenant_id == tenant_id,
                ConversationMemory.bot_id == bot_id,
                ConversationMemory.is_deleted.is_(False),
                # Only produced memories: completed analyses and terminal
                # fallbacks. A still-processing previous call is skipped.
                ConversationMemory.status.in_(("completed", "failed")),
                ConversationMemory.memory.is_not(None),
                or_(*matchers),
            )
            .order_by(ConversationMemory.created_at.desc())
            .limit(1)
        )
        if exclude_session_id:
            query = query.where(ConversationMemory.session_id != exclude_session_id)
        row = session.execute(query).scalar_one_or_none()
        if row is None:
            return None
        matched_by = (
            "record" if runtime_context_record_id
            and row.runtime_context_record_id == runtime_context_record_id
            else "customer_context" if customer_context_id
            and row.customer_context_id == customer_context_id
            else "phone_tail"
        )
        return _row_to_memory(row, matched_by)
    finally:
        session.close()


async def load_previous_memory(
    tenant_id: str,
    bot_id: str,
    *,
    runtime_context_record_id: str | None = None,
    customer_context_id: str | None = None,
    phone: str | None = None,
    exclude_session_id: str | None = None,
    timeout_seconds: float = _LOAD_TIMEOUT_SECONDS,
) -> PreviousCallMemory | None:
    """Latest analyzed memory for this customer, tenant+bot scoped.

    Bounded and fail-open: a slow or failing lookup returns None — a new
    call must never wait on (or crash because of) memory retrieval.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _load_sync,
                tenant_id,
                bot_id,
                runtime_context_record_id=runtime_context_record_id,
                customer_context_id=customer_context_id,
                phone=phone,
                exclude_session_id=exclude_session_id,
            ),
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — memory is an enhancement, never a blocker
        logger.warning("previous-memory load failed for bot %s", bot_id, exc_info=True)
        return None
