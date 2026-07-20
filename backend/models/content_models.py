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
    )  # system | greeting | fallback | escalation | closing | reprompt | hold
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    variables: Mapped[list | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(
        String(30), default="draft", nullable=False
    )  # draft | pending_approval | approved | rejected | published | archived
    active_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

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
    # Structured prompt configuration (sectioned) + backend-compiled system prompt.
    structured_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    compiled_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_compatibility: Mapped[list | None] = mapped_column(JSON, nullable=True)


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
    code: Mapped[str | None] = mapped_column(String(80), nullable=True)  # unique per bot (app-enforced)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    samples: Mapped[list | None] = mapped_column(JSON, nullable=True)
    languages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence_threshold: Mapped[float] = mapped_column(Float, default=0.7, nullable=False)
    avg_confidence_30d: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    route: Mapped[str | None] = mapped_column(String(200), nullable=True)
    entities: Mapped[list | None] = mapped_column(JSON, nullable=True)  # required entity names
    optional_entities: Mapped[list | None] = mapped_column(JSON, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    api_connection_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    kb_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    fallback_behavior: Mapped[str | None] = mapped_column(String(30), nullable=True)  # clarify | handoff | llm
    handoff_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="active", nullable=False
    )  # active | needs_samples | disabled | archived
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
    code: Mapped[str | None] = mapped_column(String(80), nullable=True)  # unique per tenant (app-enforced)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    kind: Mapped[str] = mapped_column(String(20), default="custom", nullable=False)  # system | custom | regex | api
    # Value data type: text | number | integer | decimal | date | date_range | time |
    # duration | currency | percentage | phone | email | account_number | policy_number |
    # claim_number | card_last4 | person_name | location | product | list | regex | api
    data_type: Mapped[str] = mapped_column(String(30), default="text", nullable=False)
    languages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    synonyms: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {canonical: [synonyms]}
    allowed_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
    regex_pattern: Mapped[str | None] = mapped_column(String(500), nullable=True)
    validation_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    normalization_rules: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    masking_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    example: Mapped[str | None] = mapped_column(String(300), nullable=True)
    pii: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
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
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    method: Mapped[str] = mapped_column(String(10), default="GET", nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    auth_type: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    # Masked reference only (secret://…) — raw secrets are never stored here.
    secret_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # values may use {{vars}}
    query_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    path_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    body_template: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    success_condition: Mapped[str | None] = mapped_column(String(200), nullable=True)  # e.g. "status < 400"
    success_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_mapping: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sensitive_masks: Mapped[list | None] = mapped_column(JSON, nullable=True)  # header/field names to mask
    allowed_intents: Mapped[list | None] = mapped_column(JSON, nullable=True)
    allowed_workflows: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_state_changing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_confirmation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
