"""Voice bots, voice profiles, languages, channels and telephony."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.models.base import (
    ID_LEN,
    AuditByMixin,
    Base,
    SoftDeleteMixin,
    TenantOwnedMixin,
    TimestampMixin,
)


class VoiceBot(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin, TenantOwnedMixin):
    __tablename__ = "voice_bots"
    __table_args__ = (Index("ix_voice_bots_tenant_status", "tenant_id", "status"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    use_case: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="draft", nullable=False
    )  # draft | in_review | approved | published | rolled_back | archived
    version: Mapped[str] = mapped_column(String(20), default="v0.1.0", nullable=False)
    live_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    owner_user_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("users.id"), nullable=True
    )
    health: Mapped[str] = mapped_column(String(20), default="neutral", nullable=False)
    containment: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    avg_cost_per_call: Mapped[float] = mapped_column(Numeric(8, 4), default=0, nullable=False)
    csat: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    voice_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_profiles.id"), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    readiness_items: Mapped[list["VoiceBotReadiness"]] = relationship(
        lazy="selectin", order_by="VoiceBotReadiness.sort_order"
    )
    languages: Mapped[list["BotLanguage"]] = relationship(lazy="selectin")


class VoiceBotReadiness(Base, TimestampMixin):
    __tablename__ = "voice_bot_readiness"
    __table_args__ = (UniqueConstraint("bot_id", "item_key", name="uq_readiness_bot_item"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    item_key: Mapped[str] = mapped_column(String(30), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    studio_tab: Mapped[str] = mapped_column(String(50), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class VoiceProfile(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Platform voice catalog (shared across tenants)."""

    __tablename__ = "voice_profiles"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    gender: Mapped[str] = mapped_column(String(10), default="neutral", nullable=False)
    languages: Mapped[list | None] = mapped_column(JSON, nullable=True)
    locale: Mapped[str | None] = mapped_column(String(15), nullable=True)
    accent: Mapped[str | None] = mapped_column(String(100), nullable=True)
    styles: Mapped[list | None] = mapped_column(JSON, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    premium: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sample_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_voice_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    speaking_rate: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pitch: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)


class SupportedLanguage(Base, TimestampMixin, AuditByMixin):
    __tablename__ = "supported_languages"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    code: Mapped[str] = mapped_column(String(15), unique=True, nullable=False)  # e.g. en-US
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    native_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    iso_code: Mapped[str | None] = mapped_column(String(8), nullable=True)  # ISO 639-1/2, e.g. hi
    script: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. Devanagari
    direction: Mapped[str] = mapped_column(String(3), default="ltr", nullable=False)  # ltr | rtl
    # Per-capability provider support: {"stt": ["deepgram", ...], "tts": [...], "llm": [...]}
    # Platform listing does NOT imply every provider supports the language.
    provider_support: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BotLanguage(Base):
    __tablename__ = "bot_languages"

    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), primary_key=True
    )
    language_code: Mapped[str] = mapped_column(
        String(15), ForeignKey("supported_languages.code"), primary_key=True
    )


class VoiceBotSetting(Base, TimestampMixin, AuditByMixin):
    """Per-bot voice tuning (speed, pauses, per-language voice mapping)."""

    __tablename__ = "voice_bot_settings"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), unique=True, nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    voice_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_profiles.id"), nullable=True
    )
    speed: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    pause_ms: Mapped[int] = mapped_column(Integer, default=350, nullable=False)
    empathy: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    energy: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    language_voice_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Provider selection (NULL → platform default from environment settings).
    stt_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stt_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tts_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tts_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tts_voice: Mapped[str | None] = mapped_column(String(80), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ChannelConfig(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin, TenantOwnedMixin):
    __tablename__ = "channel_configs"
    __table_args__ = (UniqueConstraint("bot_id", "type", name="uq_channel_bot_type"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # voice | whatsapp | web | mobile
    status: Mapped[str] = mapped_column(
        String(20), default="not_configured", nullable=False
    )  # live | configured | testing | failed | not_configured
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    workflow_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_test: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PhoneNumber(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "phone_numbers"
    __table_args__ = (Index("ix_phone_numbers_tenant", "tenant_id"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    country: Mapped[str | None] = mapped_column(String(5), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=True
    )
    bot_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=True
    )
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="available", nullable=False
    )  # assigned | available | porting | error
    monthly_cost: Mapped[float] = mapped_column(Numeric(8, 2), default=0, nullable=False)


class SipTrunk(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    __tablename__ = "sip_trunks"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    region: Mapped[str | None] = mapped_column(String(50), nullable=True)
    capacity_lines: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_pct: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="healthy", nullable=False)
