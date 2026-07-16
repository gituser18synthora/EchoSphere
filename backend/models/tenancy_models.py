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

from backend.models.base import (
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
    domain: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="provisioning", nullable=False, index=True
    )  # active | trial | suspended | provisioning
    health: Mapped[str] = mapped_column(String(20), default="neutral", nullable=False)
    admin_email: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Plan(Base, TimestampMixin, AuditByMixin):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)  # starter | growth | enterprise
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_monthly: Mapped[float] = mapped_column(Numeric(10, 2), default=0, nullable=False)
    bot_limit: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    minutes_included: Mapped[int] = mapped_column(Integer, default=10000, nullable=False)
    seats_included: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


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
