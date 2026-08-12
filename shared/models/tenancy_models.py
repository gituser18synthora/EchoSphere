"""Tenants, plans, subscriptions, invoices and settings."""

from datetime import date, datetime

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
    TimestampMixin,
)


class Tenant(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Short tenant code — Super Admin controlled, unique when set.
    code: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True)
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ai_profile_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="provisioning", nullable=False, index=True
    )  # active | trial | suspended | provisioning
    health: Mapped[str] = mapped_column(String(20), default="neutral", nullable=False)
    admin_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Assigned guardrail profile (Super Admin controlled). Validated
    # app-side like industry/region: only ACTIVE profiles may be newly
    # assigned, but an existing assignment stays readable after the profile
    # is deactivated. NULL → platform-mandatory guardrails only.
    guardrail_profile_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), nullable=True
    )
    # Tenant profile (tenant-admin editable, within permissions)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Post-call intelligence switches (Super Admin controlled, both off by
    # default): generate the AI call summary / outcome / NBA after each call,
    # and inject the customer's previous call summary into new calls.
    call_summary_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    use_previous_call_summary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )


class Plan(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # starter | growth | enterprise
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_monthly: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    price_annual: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    bot_limit: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    minutes_included: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    seats_included: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    kb_limit: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    storage_gb_included: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    languages_included: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    concurrent_call_limit: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    monthly_call_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = unlimited
    monthly_token_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_embedding_limit: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recording_retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    transcript_retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    analytics_retention_days: Mapped[int] = mapped_column(Integer, default=365, nullable=False)
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # feature flags
    overage_rates: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_recommended: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Subscription(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    plan_id: Mapped[str] = mapped_column(String(ID_LEN), ForeignKey("plans.id"), nullable=False)
    seats: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    bot_limit: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    minutes_included: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    renews_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )  # active | past_due | cancelled | trial
    mrr: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)


class Invoice(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "invoices"
    __table_args__ = (Index("ix_invoices_tenant_issued", "tenant_id", "issued_at"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    period: Mapped[str] = mapped_column(String(20), nullable=False)  # e.g. "Jun 2026"
    amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="open", nullable=False
    )  # paid | open | past_due | void
    issued_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class SystemSetting(Base, TimestampMixin, AuditByMixin):
    __tablename__ = "system_settings"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    value: Mapped[dict | list | str | int | bool | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class TenantSetting(Base, TimestampMixin, AuditByMixin):
    __tablename__ = "tenant_settings"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), unique=True, nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    default_languages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    branding: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    business_hours: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    holidays: Mapped[list | None] = mapped_column(JSON, nullable=True)
    notifications: Mapped[list | None] = mapped_column(JSON, nullable=True)
    security: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    # Tenant-wide human speech naturalness overrides (sparse; see
    # shared.orchestration.naturalness). Bots may override per key on top.
    human_speech: Mapped[dict | None] = mapped_column(JSON, nullable=True)
