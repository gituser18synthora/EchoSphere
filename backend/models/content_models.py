"""Knowledge, prompts, intents, entities, API connections, workflows, testing, releases."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
)


class KnowledgeSource(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "knowledge_sources"
    __table_args__ = (Index("ix_knowledge_tenant_scope", "tenant_id", "scope"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    # NULL tenant_id + scope="global" → platform-wide source
    tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=True, index=True
    )
    bot_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=True, index=True
    )
    scope: Mapped[str] = mapped_column(String(10), default="bot", nullable=False)  # bot | tenant | global
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # document | url | faq | connector
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )  # indexed | indexing | failed | pending | stale
    chunks: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    size_kb: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quality: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_30d: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class KnowledgeGap(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "knowledge_gaps"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=True
    )
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    frequency: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    suggested_source: Mapped[str | None] = mapped_column(String(300), nullable=True)


class Prompt(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "prompts"
    __table_args__ = (Index("ix_prompts_bot", "bot_id"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # greeting | fallback | escalation | closing | reprompt | hold
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(
        String(30), default="draft", nullable=False
    )  # draft | pending_approval | approved
    active_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    versions: Mapped[list["PromptVersion"]] = relationship(
        lazy="selectin", order_by="PromptVersion.version.desc()"
    )


class PromptVersion(Base, TimestampMixin):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("prompt_id", "version", name="uq_prompt_version"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    prompt_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("prompts.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    edited_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    edited_by_user_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    variants: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{language, content}]


class Intent(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "intents"
    __table_args__ = (
        Index("ix_intents_bot", "bot_id"),
        UniqueConstraint("bot_id", "name", name="uq_intent_bot_name"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    samples: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    avg_confidence_30d: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    route: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entities: Mapped[list | None] = mapped_column(JSON, nullable=True)  # entity names
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )  # active | needs_samples | disabled
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    test_pass: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    test_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class EntityDef(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "entity_defs"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_entity_tenant_name"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), default="custom", nullable=False)  # system | custom | regex
    example: Mapped[str | None] = mapped_column(String(300), nullable=True)
    pii: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    used_by: Mapped[list | None] = mapped_column(JSON, nullable=True)  # intent names


class ApiConnection(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "api_connections"
    __table_args__ = (Index("ix_api_connections_bot", "bot_id"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    method: Mapped[str] = mapped_column(String(10), default="GET", nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    # Masked reference only (secret://…) — raw secrets are never stored here.
    secret_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, default=4000, nullable=False)
    retries: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    response_mapping: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="untested", nullable=False
    )  # healthy | degraded | failing | untested
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Workflow(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_bot", "bot_id"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    nodes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    edges: Mapped[list | None] = mapped_column(JSON, nullable=True)
    issues: Mapped[list | None] = mapped_column(JSON, nullable=True)


class TestScenario(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "test_scenarios"
    __table_args__ = (Index("ix_test_scenarios_bot", "bot_id"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    suite: Mapped[str | None] = mapped_column(String(100), nullable=True)
    steps: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_run: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Release(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "releases"
    __table_args__ = (Index("ix_releases_bot", "bot_id"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    stage: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False
    )  # draft | review | approved | published | rolled_back
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checklist: Mapped[list | None] = mapped_column(JSON, nullable=True)
    diff: Mapped[list | None] = mapped_column(JSON, nullable=True)


class PlatformTemplate(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Governance reference libraries: prompt library, prompt version registry,
    knowledge templates, journey templates, action blocks."""

    __tablename__ = "platform_templates"
    __table_args__ = (Index("ix_platform_templates_kind", "kind"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
