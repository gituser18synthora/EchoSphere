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

from shared.models.base import (
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
    # Optional bot-specific guardrail profile. NULL → the bot inherits the
    # tenant's default profile; an explicit assignment stays unchanged when
    # the tenant default changes. Mandatory platform guardrails apply either
    # way and can never be weakened by this override.
    guardrail_profile_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), nullable=True
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
    """Voice catalog. tenant_id NULL = platform voice visible to every tenant;
    set = private to the owning tenant (e.g. an ElevenLabs voice clone)."""

    __tablename__ = "voice_profiles"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=True, index=True
    )
    # platform (curated catalog) | cloned (tenant-created via a provider
    # voice-cloning API — provider_voice_id is the provider clone id).
    source: Mapped[str] = mapped_column(String(20), default="platform", nullable=False)
    # Clone provenance: sample file names/sizes/durations, source types,
    # requires_verification, the provider options used. The audio itself is
    # retained in voice_clone_audio rows (files under VOICE_CLONE_AUDIO_DIR).
    clone_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
    # Provider models this voice can be used with (e.g. ["bulbul:v3"]). Empty/NULL
    # means "any model of the provider".
    model_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Per-voice default provider parameters (e.g. ElevenLabs VoiceSettings).
    provider_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class PronunciationDictionary(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Tenant-scoped metadata for provider pronunciation dictionaries.

    The provider account (Sarvam) stores the actual pronunciations and issues
    provider_dict_id; its list API returns bare ids with no names, so this
    table owns the tenant-facing name plus a cached per-language word-count
    summary. TTS (preview and live calls) passes provider_dict_id as dict_id.
    """

    __tablename__ = "pronunciation_dictionaries"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="sarvam", nullable=False)
    provider_dict_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # {"hi-IN": 12, "en-IN": 3} — display summary only, provider is authoritative.
    language_word_counts: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class VoiceCloneAudio(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin):
    """Source audio a cloned voice was built from — retained after cloning so
    tenants can replay exactly what was sent to the provider. One row per
    sample; files live under VOICE_CLONE_AUDIO_DIR at storage_path (relative,
    server-generated — user filenames never form the on-disk path)."""

    __tablename__ = "voice_clone_audio"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("tenants.id"), nullable=False, index=True
    )
    voice_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_profiles.id"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    # live_recording (browser microphone) | file_upload
    source_type: Mapped[str] = mapped_column(
        String(20), default="file_upload", nullable=False
    )
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_voice_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="stored", nullable=False)


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
    # Per-language voice assignment. Values are either a voice_profiles id
    # (legacy) or an object {"provider","model","voice","params"?}. The special
    # key "default" holds the bot's default conversation locale.
    language_voice_map: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Provider selection (NULL → platform default from environment settings).
    stt_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    stt_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tts_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tts_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tts_voice: Mapped[str | None] = mapped_column(String(80), nullable=True)
    llm_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Provider-specific configuration (validated against provider_models.params_schema).
    stt_language: Mapped[str | None] = mapped_column(String(15), nullable=True)
    stt_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    tts_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    llm_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Fallback TTS engine used only for configured transient failures.
    fallback_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    fallback_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    fallback_voice: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Transport audio configuration: {"browser": {"codec","sampleRate"},
    #                                 "telephony": {"codec","sampleRate"}}
    audio_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Goal Engine configuration (shared.orchestration.goal_engine
    # .BotGoalPolicy shape: role, domain, goals, allowedTopics,
    # restrictedTopics, identity, slots, toolRules, escalation,
    # completionCriteria, tone, outOfScope, safety). NULL → a safe default is
    # derived at runtime from the published prompt, intents and domain policy.
    goal_policy: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Human speech naturalness overrides (shared.orchestration.naturalness
    # HUMAN_SPEECH_DEFAULTS keys). Sparse: only keys the bot overrides are
    # stored. NULL → tenant override / platform defaults apply.
    human_speech: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ChannelConfig(Base, TimestampMixin, AuditByMixin, SoftDeleteMixin, TenantOwnedMixin):
    __tablename__ = "channel_configs"
    __table_args__ = (UniqueConstraint("bot_id", "type", name="uq_channel_bot_type"),)

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    bot_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("voice_bots.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # voice | whatsapp | web | mobile | sms
    status: Mapped[str] = mapped_column(
        String(20), default="not_configured", nullable=False
    )  # live | configured | testing | failed | not_configured
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
    workflow_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_test: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Provider-specific non-secret fields; secrets are env: references only.
    config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Traffic gate — webhooks/media streams reject a disabled channel.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


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
    # Admin gate: an inactive number keeps its current assignment/routing but
    # cannot be claimed for NEW bot/channel assignments.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


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
