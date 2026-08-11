"""Versioned, configurable collections-compliance policies.

A :class:`CompliancePolicy` captures WHAT a jurisdiction/regulator (or an
internal company policy) requires — calling windows, contact limits,
prohibited conduct, waiver authorization rules — as data, so enforcement is
deterministic and auditable instead of prompt-only. Policies move through
``draft → approved → active → retired``; ONLY ``active`` policies whose
``effective_date`` has arrived are enforced at runtime. Regulatory values must
carry their primary-source references in ``sources`` and are activated only
after a compliance owner signs off (``approved_by``/``approved_at``) — the
platform never turns an unverified legal rule into production enforcement.

:class:`ComplianceWording` stores legally-exact spoken text as IMMUTABLE
versioned templates: the API creates new versions, never edits one, and the
runtime records which version a call actually spoke.
"""

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
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


class CompliancePolicy(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "compliance_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", "version", name="uq_policy_code_version"),
        Index("ix_compliance_policies_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    # NULL tenant_id → platform-wide policy template (not enforced until a
    # tenant-scoped copy is activated).
    tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    jurisdiction: Mapped[str | None] = mapped_column(String(10), nullable=True)  # ISO country
    # RBI | TRAI | internal | … — internal company policy is explicitly
    # distinguished from regulator requirements.
    regulator: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False
    )  # draft | approved | active | retired
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # {"purposes": ["collections"], "channels": ["phone"],
    #  "directions": ["outbound"], "assume_direction": "outbound"}
    applies_to: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # IANA zone the calling windows are evaluated in (e.g. Asia/Kolkata).
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    # [{"days": [0..6 Monday-first], "start": "08:00", "end": "19:00"}]
    calling_windows: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # {"per_day": 3} — accepted-call attempts per caller per policy-tz day.
    contact_limits: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # [{"code": "threat_of_harm", "description": "...", "action": "block",
    #   "patterns": ["..."]}] — deterministic output rules, data-driven.
    prohibited_conduct: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # {"require_authorization": true, "patterns": ["..."],
    #  "escalate_when_unauthorized": true}
    waiver_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    escalation_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # [{"url", "title", "publisher", "document", "published", "effective",
    #   "accessed", "note"}] — primary-source evidence for every regulatory value.
    sources: Mapped[list | None] = mapped_column(JSON, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approval_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    wordings: Mapped[list["ComplianceWording"]] = relationship(
        back_populates="policy", lazy="selectin"
    )


class ComplianceWording(Base, TimestampMixin, AuditByMixin):
    """Immutable, versioned legally-exact spoken text. No update path exists —
    corrections create a new version; the runtime speaks the highest version
    for the caller's language and records exactly which one it used."""

    __tablename__ = "compliance_wordings"
    __table_args__ = (
        UniqueConstraint(
            "policy_id", "code", "language", "version", name="uq_wording_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    policy_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("compliance_policies.id"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    language: Mapped[str] = mapped_column(String(15), default="en", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # Marked exact → the LLM must never paraphrase it; it is spoken verbatim.
    exact: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    policy: Mapped[CompliancePolicy] = relationship(back_populates="wordings")
