"""Customer collection context — per-customer account/collection data for a bot.

One row is the server-trusted "customer context" for a collection call: who the
customer is, what is overdue, what payment options apply, and the mutable
call-state flags (verification, dispute, complaint, payment status, callback)
that the voice runtime records back after each call.

Typing rules (deliberate, part of the API contract):
- unknown values are NULL — never empty strings or zeros;
- money is Numeric(12, 2); day counts are Integer; dates are Date;
- tri-state facts (partial payment allowed, payment link available) are
  nullable Booleans so "unknown" is distinguishable from "no";
- call-state flags the runtime owns are non-null Booleans with defaults.

Sensitive columns (`phone`, `loan_account_number`) are stored in full but are
NEVER serialized raw — the API and the runtime expose masked derivations only
(see backend/serializers.py and shared/customer_context.py).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TenantOwnedMixin,
    TimestampMixin,
)

# The runtime and API validate payment_status against this closed set.
PAYMENT_STATUSES = ("pending", "partial", "completed", "disputed", "unknown")


class CustomerCollectionContext(
    Base, TimestampMixin, AuditByMixin, SoftDeleteMixin, TenantOwnedMixin
):
    __tablename__ = "customer_contexts"
    __table_args__ = (
        Index("ix_customer_contexts_bot_phone", "bot_id", "phone"),
        Index("ix_customer_contexts_tenant_bot", "tenant_id", "bot_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    # External CRM/LMS customer reference (stable identifier for the tenant).
    customer_ref: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # E.164-ish dialable number; calls are matched on the trailing 10 digits.
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # ── identity / parties ────────────────────────────────────────────────
    customer_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    dcs_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    lender_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    # Stored in full; exposed only as a masked tail (never serialized raw).
    loan_account_number: Mapped[str | None] = mapped_column(String(40), nullable=True)
    preferred_language: Mapped[str | None] = mapped_column(String(15), nullable=True)

    # ── account / collection facts ────────────────────────────────────────
    overdue_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    total_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    minimum_payable: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    penal_charges: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    days_overdue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    previous_promise_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    partial_payment_allowed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    payment_methods: Mapped[list | None] = mapped_column(JSON, nullable=True)
    secure_payment_link_available: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    active_offers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    offer_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    credit_reporting_status: Mapped[str | None] = mapped_column(String(120), nullable=True)
    callback_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    grievance_contact: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # ── mutable call state (owned by the runtime, updatable via the API) ──
    payment_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )
    customer_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recording_notice_required: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    complaint_pending: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    account_disputed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    callback_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    callback_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Telemetry of the most recent call that touched this context.
    last_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_disposition: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_final_transcript: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interruption_detected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
