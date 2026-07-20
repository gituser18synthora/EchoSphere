"""Platform master data: industries, data regions, AI configuration profiles
and provider definitions (STT / TTS / LLM / embedding / voice).

These tables drive tenant onboarding options and platform configuration.
Records are never hard-deleted while referenced — deactivate or archive instead.
"""

from sqlalchemy import JSON, Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TimestampMixin,
)

MASTER_STATUSES = ("active", "inactive", "archived")


class Industry(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "industries"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Recommended defaults only — never lock the tenant into these.
    default_prompt_template_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    default_guardrail_profile_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    default_workflow_template_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)


class DataRegion(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "data_regions"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cloud_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    storage_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    database_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recording_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    transcript_region: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # A region row is a *configured* operational region. Only rows with
    # infrastructure_ready=True correspond to actually deployed infrastructure.
    infrastructure_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class AiConfigProfile(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Reusable AI configuration preset offered during tenant onboarding.

    A profile is a starting template only; tenants customize afterwards within
    plan limits. API keys are NEVER stored here — only secret references live
    in tenant-specific configuration.
    """

    __tablename__ = "ai_config_profiles"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    stt_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stt_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tts_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tts_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    default_voice: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reranking_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    retrieval_top_k: Mapped[int] = mapped_column(Integer, default=6, nullable=False)
    retrieval_threshold: Mapped[float] = mapped_column(default=0.35, nullable=False)
    temperature: Mapped[float] = mapped_column(default=0.4, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    response_timeout_ms: Mapped[int] = mapped_column(Integer, default=8000, nullable=False)
    fallback_providers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cost_category: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)  # low | medium | high
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ProviderDef(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Provider master (one table, `kind` discriminates: voice/stt/tts/llm/embedding)."""

    __tablename__ = "provider_defs"
    __table_args__ = (UniqueConstraint("kind", "code", name="uq_provider_kind_code"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # voice | stt | tts | llm | embedding
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(300), nullable=True)
    requires_api_key: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Secret *reference* (env:VAR / secret://…) — raw keys are never stored.
    secret_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # models, locales, capabilities
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
