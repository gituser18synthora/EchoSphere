"""Persistent post-call conversation memory (summary, outcome, Next Best Action).

One row per completed conversation, written in two phases:

1. **Enqueue** (call finalize, cheap and synchronous): the row is created in
   status ``queued`` carrying everything the processor needs that is not in
   the stored transcript — customer linkage, final platform state, language.
   The unique ``conversation_id`` makes a duplicate hangup/finalize a no-op.
2. **Processing** (background worker): one bounded LLM analysis fills the
   structured memory, outcome and Next Best Action; retried on failure and
   terminally marked ``failed`` with a deterministic fallback memory so the
   next call still gets safe context.

Customer linkage mirrors how calls resolve customers today: the runtime
context record / legacy customer context row when one was attached, with the
caller's trailing digits as the tenant+bot-scoped fallback — never a global
phone lookup.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import (
    ID_LEN,
    Base,
    SoftDeleteMixin,
    TenantOwnedMixin,
    TimestampMixin,
)

# Processing lifecycle (same shape as knowledge ingestion jobs).
MEMORY_STATUSES = ("queued", "processing", "completed", "failed")


class ConversationMemory(Base, TimestampMixin, SoftDeleteMixin, TenantOwnedMixin):
    __tablename__ = "conversation_memories"
    __table_args__ = (
        # Idempotency boundary: one memory per conversation, ever.
        UniqueConstraint("conversation_id", name="uq_conversation_memory"),
        # Latest-memory lookups, one per customer-resolution path, all
        # tenant+bot scoped (memory must never leak across tenants or bots).
        Index(
            "ix_conv_memory_record",
            "tenant_id", "bot_id", "runtime_context_record_id", "created_at",
        ),
        Index(
            "ix_conv_memory_cctx",
            "tenant_id", "bot_id", "customer_context_id", "created_at",
        ),
        Index(
            "ix_conv_memory_phone",
            "tenant_id", "bot_id", "phone_tail", "created_at",
        ),
        # Worker claim scan.
        Index("ix_conv_memory_status", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    # conversation_sessions.id (the control-plane conversation row).
    conversation_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("conversation_sessions.id"), nullable=False
    )
    # The runtime voice session id (vs_…) — joins the Mongo transcript doc.
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(20), default="voice", nullable=False)

    # Customer linkage (any of these may be NULL; lookups always add tenant
    # + bot scope on top).
    runtime_context_record_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), nullable=True
    )
    customer_context_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), nullable=True
    )
    # Trailing digits of the caller number (the same tail the runtime uses to
    # resolve context rows) — the fallback key when no context row exists.
    phone_tail: Mapped[str | None] = mapped_column(String(15), nullable=True)

    # Processing lifecycle.
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_processing_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Inputs snapshotted at enqueue (final platform state the transcript does
    # not carry): disposition, call_state (masked/derived values only),
    # end_reason, escalated, language, workflow stage.
    final_state: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Results.
    call_outcome: Mapped[str | None] = mapped_column(String(60), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The full validated PostCallAnalysis payload (structured memory).
    memory: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Denormalized for filtering/campaign queries; the JSON carries the rest.
    next_action: Mapped[str | None] = mapped_column(String(60), nullable=True)
    next_best_action: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    follow_up_required: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    follow_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    # Language continuity: the call's final conversation language and the
    # analysis' view of the caller's dominant language.
    language: Mapped[str | None] = mapped_column(String(15), nullable=True)
    dominant_language: Mapped[str | None] = mapped_column(String(15), nullable=True)
