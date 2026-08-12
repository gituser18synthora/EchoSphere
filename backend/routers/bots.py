"""VoiceBots: CRUD, readiness, voice settings."""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_super_admin,
    require_tenant_admin,
    resolve_tenant_id,
)
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.providers.tts.delivery import strip_speed_params
from shared.orchestration.naturalness import resolve_human_speech_with_sources
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.db.mysql import get_db
from shared.models import (
    BotLanguage,
    ChannelConfig,
    SupportedLanguage,
    Tenant,
    TenantSetting,
    UsageRecord,
    User,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    VoiceProfile,
    Workflow,
)
from backend.serializers import serialize_bot

router = APIRouter(tags=["VoiceBots"])

DEFAULT_READINESS = [
    ("r1", "Knowledge sources indexed", "knowledge"),
    ("r2", "Voice selected & tuned", "voice"),
    ("r3", "Core prompts approved", "prompts"),
    ("r4", "Intents validated", "intents"),
    ("r5", "Workflow published", "workflows"),
    ("r6", "Channel connected", "channels"),
    ("r7", "Regression suite passing", "testing"),
]


def _bot_extras(db: Session, bots: list[VoiceBot]) -> dict[str, dict]:
    ids = [b.id for b in bots]
    if not ids:
        return {}
    today = date.today()
    channels = {}
    for bot_id, ctype in db.execute(
        select(ChannelConfig.bot_id, ChannelConfig.type).where(
            ChannelConfig.bot_id.in_(ids),
            ChannelConfig.is_deleted.is_(False),
            ChannelConfig.status.in_(["live", "configured", "testing"]),
        )
    ).all():
        channels.setdefault(bot_id, []).append(ctype)

    today_calls = dict(
        db.execute(
            select(UsageRecord.bot_id, func.coalesce(func.sum(UsageRecord.calls), 0))
            .where(UsageRecord.bot_id.in_(ids), UsageRecord.date == today)
            .group_by(UsageRecord.bot_id)
        ).all()
    )
    # Calls and AI cost for the current calendar month, from the metering
    # rollup (usage_records is written per call at finalize) — never from the
    # static voice_bots columns, which only demo seeds ever populated.
    month_cost = (
        func.coalesce(func.sum(UsageRecord.cost_llm), 0)
        + func.coalesce(func.sum(UsageRecord.cost_tts), 0)
        + func.coalesce(func.sum(UsageRecord.cost_stt), 0)
        + func.coalesce(func.sum(UsageRecord.cost_telephony), 0)
        + func.coalesce(func.sum(UsageRecord.cost_embedding), 0)
    )
    month_usage = {
        bot_id: (int(calls or 0), float(cost or 0))
        for bot_id, calls, cost in db.execute(
            select(
                UsageRecord.bot_id,
                func.coalesce(func.sum(UsageRecord.calls), 0),
                month_cost,
            )
            .where(
                UsageRecord.bot_id.in_(ids),
                func.extract("year", UsageRecord.date) == today.year,
                func.extract("month", UsageRecord.date) == today.month,
            )
            .group_by(UsageRecord.bot_id)
        ).all()
    }
    owner_ids = [b.owner_user_id for b in bots if b.owner_user_id]
    owners = {}
    if owner_ids:
        owners = dict(
            db.execute(select(User.id, User.name).where(User.id.in_(owner_ids))).all()
        )
    extras = {}
    for b in bots:
        calls_month, cost_month = month_usage.get(b.id, (0, 0.0))
        extras[b.id] = {
            "channels": channels.get(b.id, []),
            "calls_today": int(today_calls.get(b.id, 0)),
            "calls_month": calls_month,
            # Cost per answered call this month, from actual metered usage.
            "avg_cost_per_call": (cost_month / calls_month) if calls_month else 0.0,
            "owner": owners.get(b.owner_user_id, "—"),
        }
    return extras


def _serialize_many(db: Session, bots: list[VoiceBot]) -> list[dict]:
    extras = _bot_extras(db, bots)
    return [
        serialize_bot(
            b,
            owner_name=extras[b.id]["owner"],
            channels=extras[b.id]["channels"],
            calls_today=extras[b.id]["calls_today"],
            calls_month=extras[b.id]["calls_month"],
            avg_cost_per_call=extras[b.id]["avg_cost_per_call"],
        )
        for b in bots
    ]


def _get_bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


@router.get("/bots")
def list_bots(
    params: PageParams = Depends(page_params),
    tenant_id: str | None = Query(None, alias="tenantId"),
    status: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from backend.core.deps import is_super_admin

    if is_super_admin(user) and tenant_id is None:
        # Platform view: all bots across tenants (VoicePlatform admin page).
        stmt = select(VoiceBot).where(VoiceBot.is_deleted.is_(False))
    else:
        tid = resolve_tenant_id(user, tenant_id)
        stmt = select(VoiceBot).where(VoiceBot.tenant_id == tid, VoiceBot.is_deleted.is_(False))
    if status:
        stmt = stmt.where(VoiceBot.status == status)
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(or_(VoiceBot.name.like(like), VoiceBot.use_case.like(like)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(VoiceBot.created_at.asc()).offset(params.offset).limit(params.page_size)
    ).all()
    return paginated(_serialize_many(db, rows), page=params.page, page_size=params.page_size, total=total)


@router.get("/bots/{bot_id}")
def get_bot(bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    bot = _get_bot_checked(db, bot_id, user)
    return ok(_serialize_many(db, [bot])[0])


class CreateBotRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    use_case: str = Field(default="", alias="useCase", max_length=200)
    description: str = Field(default="", max_length=2000)
    # Omitted means "use the current enabled platform default".  A literal
    # locale here becomes stale as soon as an administrator disables it.
    languages: list[str] | None = None
    tenant_id: str | None = Field(default=None, alias="tenantId")

    model_config = {"populate_by_name": True}


def _validated_languages(db: Session, codes: list[str]) -> list[str]:
    """Require every code to be an enabled platform language."""
    deduped = list(dict.fromkeys(c.strip() for c in codes if c and c.strip()))
    if not deduped:
        raise ApiError("At least one supported language is required.", 422)
    enabled = set(
        db.scalars(
            select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))
        ).all()
    )
    rejected = [c for c in deduped if c not in enabled]
    if rejected:
        raise ApiError(f"Unknown or disabled language(s): {', '.join(rejected)}", 422)
    return deduped


def _default_bot_languages(db: Session) -> list[str]:
    """One enabled catalog language: flagged default first, then display order."""
    code = db.scalar(
        select(SupportedLanguage.code)
        .where(SupportedLanguage.enabled.is_(True))
        .order_by(
            SupportedLanguage.is_default.desc(),
            SupportedLanguage.sort_order,
            SupportedLanguage.code,
        )
        .limit(1)
    )
    return [code] if code else []


@router.post("/bots", status_code=201)
def create_bot(
    body: CreateBotRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, body.tenant_id)
    if db.get(Tenant, tid) is None:
        raise NotFoundError("Tenant")

    requested_languages = (
        body.languages if body.languages is not None else _default_bot_languages(db)
    )
    langs = _validated_languages(db, requested_languages)

    bot = VoiceBot(
        id=new_id("bot"),
        tenant_id=tid,
        name=body.name,
        use_case=body.use_case,
        description=body.description,
        status="draft",
        version="v0.1.0",
        owner_user_id=user.id,
        health="neutral",
        created_by=user.id,
    )
    db.add(bot)
    db.flush()  # bot row must exist before workflow/readiness FKs
    for i, (key, label, tab) in enumerate(DEFAULT_READINESS):
        db.add(
            VoiceBotReadiness(
                id=new_id("rd"), bot_id=bot.id, item_key=key, label=label,
                done=False, studio_tab=tab, sort_order=i,
            )
        )
    for code in langs:
        db.add(BotLanguage(bot_id=bot.id, language_code=code))
    db.add(
        Workflow(
            id=new_id("wf"), tenant_id=tid, bot_id=bot.id,
            name=f"{body.use_case or body.name} journey", version=1, status="draft",
            nodes=[
                {"id": "n1", "kind": "start", "label": "Call starts", "x": 40, "y": 40},
                {"id": "n2", "kind": "message", "label": "Greeting", "x": 40, "y": 150},
                {"id": "n3", "kind": "end", "label": "End call", "x": 40, "y": 260},
            ],
            edges=[
                {"id": "e1", "from": "n1", "to": "n2"},
                {"id": "e2", "from": "n2", "to": "n3"},
            ],
            issues=[],
            created_by=user.id,
        )
    )
    record_audit(
        db, user=user, action="Created VoiceBot", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=tid,
        new_value={"name": bot.name, "useCase": bot.use_case, "languages": langs},
        request=request,
    )
    db.commit()
    db.refresh(bot)
    return ok(_serialize_many(db, [bot])[0])


class UpdateBotRequest(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    use_case: str | None = Field(default=None, alias="useCase", max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: str | None = Field(
        default=None,
        pattern="^(draft|in_review|approved|published|rolled_back|archived)$",
    )
    languages: list[str] | None = None
    voice_id: str | None = Field(default=None, alias="voiceId")
    owner_user_id: str | None = Field(default=None, alias="ownerUserId")
    readiness: dict[str, bool] | None = None  # item_key -> done

    model_config = {"populate_by_name": True}


@router.patch("/bots/{bot_id}")
def update_bot(
    bot_id: str,
    body: UpdateBotRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _get_bot_checked(db, bot_id, user)
    before = {"name": bot.name, "status": bot.status, "voiceId": bot.voice_id}

    for field in ("name", "use_case", "description"):
        val = getattr(body, field)
        if val is not None:
            setattr(bot, field, val)
    if body.status is not None and body.status != bot.status:
        bot.status = body.status
        if body.status == "published":
            bot.published_at = datetime.now(timezone.utc)
            bot.live_version = bot.version
    if body.voice_id is not None:
        if body.voice_id and db.get(VoiceProfile, body.voice_id) is None:
            raise ApiError("Unknown voice profile.", 422)
        bot.voice_id = body.voice_id or None
    if body.owner_user_id is not None:
        owner = db.get(User, body.owner_user_id)
        if owner is None or (owner.tenant_id not in (None, bot.tenant_id)):
            raise ApiError("Owner must be a member of the same tenant.", 422)
        bot.owner_user_id = body.owner_user_id
    if body.languages is not None:
        langs = _validated_languages(db, body.languages)
        existing = db.scalars(select(BotLanguage).where(BotLanguage.bot_id == bot.id)).all()
        for row in existing:
            if row.language_code not in langs:
                db.delete(row)  # association row, not domain data
        have = {r.language_code for r in existing}
        for code in langs:
            if code not in have:
                db.add(BotLanguage(bot_id=bot.id, language_code=code))
    if body.readiness:
        for item in bot.readiness_items:
            if item.item_key in body.readiness:
                item.done = bool(body.readiness[item.item_key])
    bot.updated_by = user.id
    record_audit(
        db, user=user, action="Updated VoiceBot", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id,
        previous_value=before,
        new_value={"name": bot.name, "status": bot.status, "voiceId": bot.voice_id},
        request=request,
    )
    db.commit()
    db.refresh(bot)
    return ok(_serialize_many(db, [bot])[0])


@router.delete("/bots/{bot_id}")
def delete_bot(
    bot_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _get_bot_checked(db, bot_id, user)
    if hard:
        guard_hard_delete()
    soft_delete(bot, user)
    record_audit(
        db, user=user, action="Archived VoiceBot", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": bot.id})


# ── Voice settings (tuning) ──────────────────────────────────────────────────


class VoiceSettingsRequest(BaseModel):
    voice_id: str | None = Field(default=None, alias="voiceId")
    speed: float | None = Field(default=None, ge=0.5, le=2.0)
    pause_ms: int | None = Field(default=None, alias="pauseMs", ge=0, le=5000)
    empathy: int | None = Field(default=None, ge=0, le=100)
    energy: int | None = Field(default=None, ge=0, le=100)
    # Values: legacy voice-profile id strings or {"provider","model","voice","params"?}.
    language_voice_map: dict[str, object] | None = Field(default=None, alias="languageVoiceMap")
    stt_provider: str | None = Field(default=None, alias="sttProvider", max_length=40)
    stt_model: str | None = Field(default=None, alias="sttModel", max_length=80)
    stt_language: str | None = Field(default=None, alias="sttLanguage", max_length=15)
    stt_settings: dict | None = Field(default=None, alias="sttSettings")
    tts_provider: str | None = Field(default=None, alias="ttsProvider", max_length=40)
    tts_model: str | None = Field(default=None, alias="ttsModel", max_length=80)
    tts_voice: str | None = Field(default=None, alias="ttsVoice", max_length=80)
    tts_settings: dict | None = Field(default=None, alias="ttsSettings")
    llm_provider: str | None = Field(default=None, alias="llmProvider", max_length=40)
    llm_model: str | None = Field(default=None, alias="llmModel", max_length=80)
    llm_settings: dict | None = Field(default=None, alias="llmSettings")
    fallback_provider: str | None = Field(default=None, alias="fallbackProvider", max_length=40)
    fallback_model: str | None = Field(default=None, alias="fallbackModel", max_length=80)
    fallback_voice: str | None = Field(default=None, alias="fallbackVoice", max_length=80)
    audio_settings: dict | None = Field(default=None, alias="audioSettings")
    # Goal Engine configuration ({} clears it back to the derived default).
    goal_policy: dict | None = Field(default=None, alias="goalPolicy")
    # Human speech naturalness overrides ({} clears back to tenant/platform).
    human_speech: dict | None = Field(default=None, alias="humanSpeech")

    model_config = {"populate_by_name": True}


_VOICE_SETTINGS_FIELDS = (
    "speed", "pause_ms", "empathy", "energy", "language_voice_map",
    "stt_provider", "stt_model", "stt_language", "stt_settings",
    "tts_provider", "tts_model", "tts_voice", "tts_settings",
    "llm_provider", "llm_model", "llm_settings",
    "fallback_provider", "fallback_model", "fallback_voice", "audio_settings",
    "goal_policy", "human_speech",
)

# Delivery tuning's speaking speed is the single canonical speed control:
# per-provider duplicates in stored TTS parameters are stripped on save and
# hidden on read (the runtime additionally overrides any value that survives
# in old rows). shared/providers/tts/delivery.py owns the key list so the save
# path and the preview path strip exactly the same parameters.

def _sanitize_language_voice_map(lang_map: dict | None) -> dict | None:
    if lang_map is None:
        return None
    sanitized: dict = {}
    for locale, entry in lang_map.items():
        if isinstance(entry, dict) and entry.get("params"):
            entry = {**entry, "params": strip_speed_params(entry["params"])}
        sanitized[locale] = entry
    return sanitized


def _serialize_voice_settings(
    s: VoiceBotSetting, tenant_human_speech: dict | None = None
) -> dict:
    effective, sources = resolve_human_speech_with_sources(
        tenant_human_speech, s.human_speech
    )
    inherited, inherited_sources = resolve_human_speech_with_sources(
        tenant_human_speech
    )
    return {
        "botId": s.bot_id,
        "voiceId": s.voice_id,
        "speed": s.speed,
        "pauseMs": s.pause_ms,
        "empathy": s.empathy,
        "energy": s.energy,
        "languageVoiceMap": _sanitize_language_voice_map(s.language_voice_map) or {},
        "sttProvider": s.stt_provider,
        "sttModel": s.stt_model,
        "sttLanguage": s.stt_language,
        "sttSettings": s.stt_settings or {},
        "ttsProvider": s.tts_provider,
        "ttsModel": s.tts_model,
        "ttsVoice": s.tts_voice,
        "ttsSettings": strip_speed_params(s.tts_settings),
        "llmProvider": s.llm_provider,
        "llmModel": s.llm_model,
        "llmSettings": s.llm_settings or {},
        "fallbackProvider": s.fallback_provider,
        "fallbackModel": s.fallback_model,
        "fallbackVoice": s.fallback_voice,
        "audioSettings": s.audio_settings or {},
        "goalPolicy": s.goal_policy or {},
        "humanSpeech": s.human_speech or {},
        "humanSpeechEffective": effective,
        "humanSpeechSources": sources,
        "humanSpeechInherited": inherited,
        "humanSpeechInheritedSources": inherited_sources,
    }


@router.get("/bots/{bot_id}/voice-settings")
def get_voice_settings(
    bot_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    bot = _get_bot_checked(db, bot_id, user)
    s = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    if s is None:
        s = VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot.id, tenant_id=bot.tenant_id,
            voice_id=bot.voice_id, created_by=user.id,
        )
        db.add(s)
        db.commit()
    tenant_human_speech = db.scalar(
        select(TenantSetting.human_speech).where(
            TenantSetting.tenant_id == bot.tenant_id
        )
    )
    return ok(_serialize_voice_settings(s, tenant_human_speech))


@router.put("/bots/{bot_id}/voice-settings")
def update_voice_settings(
    bot_id: str,
    body: VoiceSettingsRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _get_bot_checked(db, bot_id, user)
    s = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    if s is None:
        s = VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot.id, tenant_id=bot.tenant_id, created_by=user.id
        )
        db.add(s)
    tenant_human_speech = db.scalar(
        select(TenantSetting.human_speech).where(
            TenantSetting.tenant_id == bot.tenant_id
        )
    )
    before = _serialize_voice_settings(s, tenant_human_speech)
    if body.voice_id is not None:
        if body.voice_id:
            profile = db.get(VoiceProfile, body.voice_id)
            # Another tenant's cloned voice is indistinguishable from a
            # nonexistent one — never selectable, never acknowledged.
            if (profile is None or profile.is_deleted
                    or profile.tenant_id not in (None, bot.tenant_id)):
                raise ApiError("Unknown voice profile.", 422)
            if profile.status != "active":
                raise ApiError(
                    f"Voice '{profile.name}' is inactive and cannot be selected.", 422,
                    errors=[{"field": "voiceId",
                             "message": f"Voice '{profile.name}' is inactive and cannot be selected."}],
                )
        s.voice_id = body.voice_id or None
        bot.voice_id = s.voice_id

    # Goal Engine configuration must at least parse into the policy schema —
    # a config that cannot compile would silently fall back to the derived
    # default at runtime, which is exactly the confusion to reject here.
    if body.goal_policy:
        from shared.orchestration.goal_engine import BotGoalPolicy

        try:
            BotGoalPolicy.model_validate(body.goal_policy)
        except Exception as exc:  # noqa: BLE001 — surfaced as a field error
            raise ApiError(
                "Goal policy configuration is invalid.", 422,
                errors=[{"field": "goalPolicy", "message": str(exc)[:300]}],
            )

    # Human speech overrides are sparse per-key; junk keys/values are
    # rejected here rather than silently dropped at runtime resolution.
    if body.human_speech:
        from shared.orchestration.naturalness import validate_human_speech

        problems = validate_human_speech(body.human_speech)
        if problems:
            raise ApiError(
                "Human speech configuration is invalid.", 422,
                errors=[{"field": "humanSpeech", "message": p} for p in problems],
            )

    # Sanitize legacy per-provider speed duplicates before validation and
    # persistence — Delivery tuning's speed is the only speed control.
    if body.tts_settings is not None:
        body.tts_settings = strip_speed_params(body.tts_settings)
    if body.language_voice_map is not None:
        body.language_voice_map = _sanitize_language_voice_map(body.language_voice_map)

    # The provider registry gates which adapters exist; the DB catalog gates
    # which provider/model/language/voice/parameter combinations are valid.
    from shared.providers.factory import _REGISTRY as _provider_registry

    for kind, value in (("stt", body.stt_provider), ("tts", body.tts_provider),
                        ("llm", body.llm_provider), ("tts", body.fallback_provider)):
        if value and (kind, value.lower()) not in _provider_registry:
            raise ApiError(f"Unknown {kind} provider '{value}'.", 422)

    # Validate the EFFECTIVE configuration (current row overlaid with updates)
    # against the database catalog — frontend hiding alone is never trusted.
    from backend.core.provider_catalog import validate_voice_settings

    effective = {
        field: getattr(body, field) if getattr(body, field) is not None
        else getattr(s, field)
        for field in _VOICE_SETTINGS_FIELDS
    }
    errors, warnings = validate_voice_settings(db, bot, effective)
    if errors:
        raise ApiError("Voice settings are invalid.", 422, errors=errors)

    for field in _VOICE_SETTINGS_FIELDS:
        val = getattr(body, field)
        if val is not None:
            setattr(s, field, val)
    s.updated_by = user.id
    # Voice readiness follows having a voice selected.
    for item in bot.readiness_items:
        if item.item_key == "r2" and s.voice_id:
            item.done = True
    record_audit(
        db, user=user, action="Updated voice settings", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id,
        previous_value=before,
        new_value=_serialize_voice_settings(s, tenant_human_speech), request=request,
    )
    db.commit()
    from shared.bot_config import invalidate_bot_config_sync

    invalidate_bot_config_sync(bot.tenant_id, bot.id)
    return ok(
        _serialize_voice_settings(s, tenant_human_speech),
        meta={"warnings": warnings} if warnings else None,
    )


# ── Bot-level guardrail profile ───────────────────────────────────────────────
#
# Hierarchy: mandatory platform guardrails (always) → the bot's explicit
# profile when set → else the tenant's default profile. Assignment is a
# governance action (Super Admin); tenant members can VIEW the effective
# result for their own bots.


class BotGuardrailProfileRequest(BaseModel):
    # "" clears the explicit assignment → the bot inherits the tenant default.
    guardrail_profile_id: str = Field(alias="guardrailProfileId", max_length=50)

    model_config = {"populate_by_name": True}


@router.patch("/bots/{bot_id}/guardrail-profile")
def set_bot_guardrail_profile(
    bot_id: str,
    body: BotGuardrailProfileRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from backend.routers.tenants import _validate_guardrail_profile

    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("Bot")
    before = {"guardrailProfileId": bot.guardrail_profile_id}
    value = body.guardrail_profile_id.strip()
    if not value:
        bot.guardrail_profile_id = None
    elif value != bot.guardrail_profile_id:
        # New assignments require an ACTIVE profile; keeping the current
        # (possibly deactivated) one untouched is always allowed.
        bot.guardrail_profile_id = _validate_guardrail_profile(db, value).id
    bot.updated_by = user.id
    record_audit(
        db, user=user,
        action="Assigned bot guardrail profile" if bot.guardrail_profile_id
        else "Cleared bot guardrail profile (inherits tenant default)",
        entity_type="bot", entity_id=bot.id, target_label=bot.name,
        tenant_id=bot.tenant_id, previous_value=before,
        new_value={"guardrailProfileId": bot.guardrail_profile_id},
        request=request,
    )
    db.commit()
    from shared.bot_config import invalidate_bot_config_sync

    invalidate_bot_config_sync(bot.tenant_id, bot.id)
    return ok(_serialize_many(db, [bot])[0])


@router.get("/bots/{bot_id}/effective-guardrails")
def bot_effective_guardrails(
    bot_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The guardrails and compliance policies actually enforced for this bot
    at call start: mandatory rules ∪ (explicit bot profile ∥ tenant default),
    plus every ACTIVE compliance policy version."""
    from shared.compliance import load_active_policies_sync
    from shared.guardrails import load_effective_guardrails_sync
    from shared.models import GuardrailProfile

    bot = _get_bot_checked(db, bot_id, user)
    effective = load_effective_guardrails_sync(bot.tenant_id, bot.id, session=db)
    tenant = db.get(Tenant, bot.tenant_id)
    tenant_profile = db.get(GuardrailProfile, tenant.guardrail_profile_id) \
        if tenant is not None and tenant.guardrail_profile_id else None
    explicit_profile = db.get(GuardrailProfile, bot.guardrail_profile_id) \
        if bot.guardrail_profile_id else None
    policies = load_active_policies_sync(bot.tenant_id, session=db)

    def _summary(p):
        if p is None or p.is_deleted:
            return None
        return {"id": p.id, "code": p.code, "name": p.name,
                "status": p.status, "version": p.version}

    rules = sorted(effective.rules, key=lambda r: (not r.mandatory, r.name))
    return ok({
        "botId": bot.id,
        "tenantId": bot.tenant_id,
        "inherited": bot.guardrail_profile_id is None,
        "profile": _summary(explicit_profile) if bot.guardrail_profile_id
        else _summary(tenant_profile),
        "tenantDefaultProfile": _summary(tenant_profile),
        "rules": [
            {"guardrailId": r.guardrail_id or "", "code": r.code, "name": r.name,
             "category": r.category, "action": r.action, "mandatory": r.mandatory}
            for r in rules
        ],
        "compliancePolicies": [
            {"code": p.code, "version": p.version, "name": p.name,
             "regulator": p.regulator, "jurisdiction": p.jurisdiction,
             "timezone": p.timezone,
             "callingWindows": list(p.calling_windows)}
            for p in policies
        ],
        "degraded": effective.degraded,
    })
