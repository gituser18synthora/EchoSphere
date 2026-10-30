"""Guardrail profiles: named bundles of guardrails assignable to tenants.

The flat ``guardrails`` table stays the platform-wide rule registry; a
``GuardrailProfile`` selects which of those rules apply to a tenant, via the
normalized ``guardrail_profile_rules`` association. Mandatory platform rules
(``Guardrail.is_mandatory``) apply to every tenant regardless of profile and
can never be disabled by a profile, tenant or bot.

``GuardrailTrigger`` is the tenant-scoped enforcement ledger: one row per
runtime guardrail hit (block / redact / flag / escalate), carrying the rule,
stage and profile version — never the raw sensitive value that tripped it.
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
)


class GuardrailProfile(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "guardrail_profiles"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False, index=True
    )  # active | inactive | archived
    # Bumped on every rule-membership or metadata change so triggers and
    # audits can pin the exact profile revision a call ran under.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    rules: Mapped[list["GuardrailProfileRule"]] = relationship(
        back_populates="profile", lazy="selectin", cascade="all, delete-orphan"
    )


class GuardrailProfileRule(Base, TimestampMixin, AuditByMixin):
    """Normalized profile ↔ guardrail association."""

    __tablename__ = "guardrail_profile_rules"
    __table_args__ = (
        UniqueConstraint("profile_id", "guardrail_id", name="uq_profile_guardrail"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    profile_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("guardrail_profiles.id"), nullable=False, index=True
    )
    guardrail_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("guardrails.id"), nullable=False, index=True
    )

    profile: Mapped[GuardrailProfile] = relationship(back_populates="rules")


class GuardrailTrigger(Base):
    """Append-only runtime enforcement record. ``detail`` is a short
    non-sensitive summary (pattern kind, tool name) — raw matched values are
    never stored here."""

    __tablename__ = "guardrail_triggers"
    __table_args__ = (
        Index("ix_guardrail_triggers_tenant_time", "tenant_id", "created_at"),
        Index("ix_guardrail_triggers_code", "guardrail_code"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    bot_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    guardrail_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    guardrail_code: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    action: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # block | redact | flag | escalate
    stage: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # input | output | tool | transcript | log
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    profile_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Compliance-policy context when the hit came from a policy rule rather
    # than (or in addition to) a guardrail row.
    policy_code: Mapped[str | None] = mapped_column(String(60), nullable=True)
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # What actually happened: blocked | redacted | flagged | emitted |
    # rescheduled | escalated.
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    channel: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
