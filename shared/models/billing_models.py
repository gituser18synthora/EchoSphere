"""Currencies, exchange rates, provider pricing and raw usage events.

Money conventions:
- The platform base currency is USD. Provider prices are stored in their
  native currency (normally USD); costs on usage events are snapshotted in
  USD at recording time so later price/rate changes never rewrite history.
- All monetary columns are DECIMAL — arithmetic happens in `decimal.Decimal`
  inside shared/billing, never floats.
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
)

BASE_CURRENCY = "USD"

# Capabilities that can be metered. Telephony is intentionally priced
# separately from AI providers (different components/units).
USAGE_CAPABILITIES = ("llm", "embedding", "stt", "tts", "telephony")

# What is being priced/measured within a capability.
PRICING_COMPONENTS = (
    "input_tokens",      # LLM prompt tokens
    "output_tokens",     # LLM completion tokens
    "cached_input_tokens",  # provider-discounted cached prompt tokens
    "tokens",            # blended/total tokens (embeddings, legacy blended LLM price)
    "characters",        # TTS characters
    "audio_seconds",     # STT / TTS audio duration
    "call_seconds",      # telephony call duration
    "requests",          # flat per-request pricing
)

# How a unit price is expressed. The divisor maps a raw quantity to the
# priced unit (e.g. tokens → per-1M-token price).
PRICING_UNITS = (
    "per_token",
    "per_1k_tokens",
    "per_1m_tokens",
    "per_character",
    "per_1k_characters",
    "per_second",
    "per_minute",
    "per_request",
)


class Currency(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """ISO 4217 display/pricing currencies managed by Super Admin."""

    __tablename__ = "currencies"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(3), unique=True, nullable=False)  # ISO 4217
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(8), nullable=False)
    decimal_places: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Exactly one active base currency (USD). Conversions always go
    # base -> target; chained cross-rates are never used.
    is_base: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ExchangeRate(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Manually managed base->target exchange rates with effective dating.

    Rate selection: active rows for the pair with effective_from <= now,
    newest effective_from wins. Historical rows are deactivated, never
    edited in place once superseded, so past conversions stay reproducible.
    """

    __tablename__ = "exchange_rates"
    __table_args__ = (
        UniqueConstraint(
            "base_code", "target_code", "effective_from", name="uq_fx_pair_effective"
        ),
        Index("ix_fx_pair_status", "base_code", "target_code", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    base_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.code", name="fk_fx_base_currency", onupdate="CASCADE"),
        nullable=False,
    )
    target_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.code", name="fk_fx_target_currency", onupdate="CASCADE"),
        nullable=False,
    )
    # 1 unit of base_code == `rate` units of target_code (e.g. USD->INR 86.50)
    rate: Mapped[float] = mapped_column(Numeric(18, 8), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    # manual today; a future automatic feed writes new rows with its own source.
    source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProviderPricing(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Database-driven unit prices per provider/model/component.

    One row prices one component of one provider model (e.g. openai gpt-4o
    llm input_tokens per_1m_tokens 2.50 USD). Price changes are made by
    deactivating the old row and adding a new one — usage events snapshot
    the price they were costed with, so history never shifts.
    """

    __tablename__ = "provider_pricing"
    __table_args__ = (
        UniqueConstraint(
            "provider_code",
            "capability",
            "model_code",
            "component",
            "effective_from",
            name="uq_pricing_key_effective",
        ),
        Index("ix_pricing_lookup", "provider_code", "capability", "model_code", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False)
    capability: Mapped[str] = mapped_column(String(20), nullable=False)  # USAGE_CAPABILITIES
    model_code: Mapped[str] = mapped_column(String(80), nullable=False)
    component: Mapped[str] = mapped_column(String(30), nullable=False)  # PRICING_COMPONENTS
    unit: Mapped[str] = mapped_column(String(20), nullable=False)  # PRICING_UNITS
    # Native provider price for one `unit` — small per-unit prices need depth.
    unit_price: Mapped[float] = mapped_column(Numeric(18, 10), nullable=False)
    currency_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.code", name="fk_pricing_currency", onupdate="CASCADE"),
        default=BASE_CURRENCY,
        nullable=False,
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class UsageEvent(Base, TimestampMixin):
    """One logical billable provider operation, attributed to a tenant.

    Streaming chunks/retries never create extra rows: callers aggregate one
    logical operation (a call's LLM turns, one TTS generation set, one
    embedding batch) and pass a deterministic request_id — the unique index
    makes re-submission a no-op.

    Cost snapshot: quantities + unit prices + USD cost are frozen at
    recording time (`pricing_snapshot`), so later pricing/exchange-rate
    changes only affect new events.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_usage_event_request"),
        Index("ix_usage_events_tenant_time", "tenant_id", "occurred_at"),
        Index("ix_usage_events_capability_time", "capability", "occurred_at"),
        Index("ix_usage_events_provider", "provider_code", "model_code"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False
    )
    bot_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=True
    )
    session_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    capability: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False)
    model_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    voice_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Deterministic idempotency key (e.g. "<session>:llm"); NULL allowed.
    request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    requests: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # Quantities — only the ones meaningful for the capability are set.
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    audio_seconds: Mapped[float] = mapped_column(Numeric(12, 3), default=0, nullable=False)
    # provider = provider-reported usage; estimated = documented fallback.
    usage_source: Mapped[str] = mapped_column(String(20), default="provider", nullable=False)
    usage_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # priced | missing_price (quantities recorded, cost unknown — surfaced to admin)
    pricing_status: Mapped[str] = mapped_column(String(20), default="priced", nullable=False)
    # {component: {unit, unit_price, currency}} used for this event.
    pricing_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Numeric(14, 6), default=0, nullable=False)
