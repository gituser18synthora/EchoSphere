"""Conversations, alerts, audit, integrations, governance, usage and health."""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
)


class ConversationSession(Base, TimestampMixin, SoftDeleteMixin):
    """Structured conversation metadata. The full transcript (nested, variable
    per-turn structure) lives in MongoDB `conversation_transcripts`, keyed by id."""

    __tablename__ = "conversation_sessions"
    __table_args__ = (
        Index("ix_conversations_tenant_started", "tenant_id", "started_at"),
        Index("ix_conversations_bot_started", "bot_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False
    )
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(20), default="voice", nullable=False)
    caller_masked: Mapped[str | None] = mapped_column(String(50), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_sec: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sentiment: Mapped[str] = mapped_column(String(10), default="neutral", nullable=False)
    intents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    contained: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    csat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float] = mapped_column(Numeric(8, 4), default=0, nullable=False)
    language: Mapped[str | None] = mapped_column(String(15), nullable=True)
    qa_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="completed", nullable=False)


class PlatformAlert(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "platform_alerts"
    __table_args__ = (Index("ix_alerts_status_time", "status", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    severity: Mapped[str] = mapped_column(String(20), default="warning", nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source: Mapped[str | None] = mapped_column(String(200), nullable=True)
    scope: Mapped[str] = mapped_column(String(20), default="platform", nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="open", nullable=False
    )  # open | acknowledged | resolved
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    target_label: Mapped[str | None] = mapped_column(String(300), nullable=True)
    previous_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class Integration(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Platform integration catalog (what CAN be connected)."""

    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="available", nullable=False)


class TenantIntegration(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Per-tenant connection state for a catalog integration."""

    __tablename__ = "tenant_integrations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "integration_id", name="uq_tenant_integration"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    integration_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("integrations.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="available", nullable=False
    )  # connected | available | error
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ApprovedModel(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "approved_models"
    __table_args__ = (UniqueConstraint("name", "purpose", name="uq_model_name_purpose"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    purpose: Mapped[str] = mapped_column(
        String(30), default="conversation", nullable=False
    )  # conversation | embedding | classification | summarization
    status: Mapped[str] = mapped_column(
        String(20), default="testing", nullable=False
    )  # approved | testing | deprecated
    tenants_using: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_per_1k: Mapped[float] = mapped_column(Numeric(10, 6), default=0, nullable=False)
    latency_p50: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Guardrail(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "guardrails"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enforcement: Mapped[str] = mapped_column(
        String(20), default="flag", nullable=False
    )  # block | flag | redact
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    triggers_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class UsageRecord(Base, TimestampMixin):
    """Daily usage rollup per tenant/bot — powers dashboards, analytics, billing."""

    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("tenant_id", "bot_id", "date", name="uq_usage_tenant_bot_date"),
        Index("ix_usage_tenant_date", "tenant_id", "date"),
        Index("ix_usage_date", "date"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False
    )
    # NULL bot_id → tenant-level rollup row
    bot_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    contained_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    escalations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    minutes: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    csat_avg: Mapped[float | None] = mapped_column(Float, nullable=True)
    cost_llm: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    cost_tts: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    cost_stt: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)
    cost_telephony: Mapped[float] = mapped_column(Numeric(10, 4), default=0, nullable=False)


class HealthMetric(Base, TimestampMixin):
    """Latest platform component health snapshot (Monitoring page)."""

    __tablename__ = "health_metrics"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="neutral", nullable=False)
    value: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    spark: Mapped[list | None] = mapped_column(JSON, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
