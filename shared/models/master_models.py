"""Platform master data: industries, data regions, AI configuration profiles
and provider definitions (STT / TTS / LLM / embedding / voice).

These tables drive tenant onboarding options and platform configuration.
Records are never hard-deleted while referenced — deactivate or archive instead.
"""

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import (
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


class Country(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Country catalog used by regional platform configuration.

    The first rollout intentionally contains Asia only. ``code`` is the
    lowercase ISO 3166-1 alpha-2 code and is stable after creation.
    """

    __tablename__ = "countries"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(2), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    region: Mapped[str] = mapped_column(String(50), default="Asia", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class DataRegion(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "data_regions"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    country_code: Mapped[str | None] = mapped_column(
        String(2), ForeignKey("countries.code"), nullable=True, index=True
    )
    # Denormalized display name retained for existing API/data compatibility;
    # writes are canonicalized from ``countries`` by the master-data service.
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


class ProviderModel(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Per-provider model catalog: capabilities, languages, formats and parameter schema.

    Everything the configuration UI and the validators need to offer/check a
    provider+model combination lives here (seeded, editable via master data).
    """

    __tablename__ = "provider_models"
    __table_args__ = (
        UniqueConstraint("provider_code", "capability", "code", name="uq_provider_model"),
    )

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    capability: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # stt | tts | llm
    code: Mapped[str] = mapped_column(String(80), nullable=False)  # e.g. bulbul:v3, eleven_flash_v2_5
    display_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Provider-native language codes ("hi-IN" for Sarvam, "hi" for ElevenLabs).
    # Empty list => language-agnostic (LLMs).
    languages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    codecs: Mapped[list | None] = mapped_column(JSON, nullable=True)  # e.g. ["linear16","mulaw","alaw"]
    sample_rates: Mapped[list | None] = mapped_column(JSON, nullable=True)  # e.g. [8000,16000,22050,24000]
    streaming: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # JSON-schema-ish parameter descriptors driving the dynamic UI and backend
    # range validation: {"field": {"type","min","max","default","enum","label","help"}}
    params_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
