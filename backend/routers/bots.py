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
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.db.mysql import get_db
from backend.models import (
    BotLanguage,
    ChannelConfig,
    SupportedLanguage,
    Tenant,
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
    month_calls = dict(
        db.execute(
            select(UsageRecord.bot_id, func.coalesce(func.sum(UsageRecord.calls), 0))
            .where(
                UsageRecord.bot_id.in_(ids),
                func.extract("year", UsageRecord.date) == today.year,
                func.extract("month", UsageRecord.date) == today.month,
            )
            .group_by(UsageRecord.bot_id)
        ).all()
    )
    owner_ids = [b.owner_user_id for b in bots if b.owner_user_id]
    owners = {}
    if owner_ids:
        owners = dict(
            db.execute(select(User.id, User.name).where(User.id.in_(owner_ids))).all()
        )
    return {
        b.id: {
            "channels": channels.get(b.id, []),
            "calls_today": int(today_calls.get(b.id, 0)),
            "calls_month": int(month_calls.get(b.id, 0)),
            "owner": owners.get(b.owner_user_id, "—"),
        }
        for b in bots
    }


def _serialize_many(db: Session, bots: list[VoiceBot]) -> list[dict]:
    extras = _bot_extras(db, bots)
    return [
        serialize_bot(
            b,
            owner_name=extras[b.id]["owner"],
            channels=extras[b.id]["channels"],
            calls_today=extras[b.id]["calls_today"],
            calls_month=extras[b.id]["calls_month"],
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
    languages: list[str] = Field(default_factory=lambda: ["en-US"])
    tenant_id: str | None = Field(default=None, alias="tenantId")

    model_config = {"populate_by_name": True}


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

    valid_langs = set(
        db.scalars(select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))).all()
    )
    langs = [l for l in body.languages if l in valid_langs] or ["en-US"]

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
        valid = set(
            db.scalars(
                select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))
            ).all()
        )
        langs = [l for l in body.languages if l in valid]
        if not langs:
            raise ApiError("At least one supported language is required.", 422)
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
    language_voice_map: dict[str, str] | None = Field(default=None, alias="languageVoiceMap")
    stt_provider: str | None = Field(default=None, alias="sttProvider", max_length=40)
    stt_model: str | None = Field(default=None, alias="sttModel", max_length=80)
    tts_provider: str | None = Field(default=None, alias="ttsProvider", max_length=40)
    tts_model: str | None = Field(default=None, alias="ttsModel", max_length=80)
    tts_voice: str | None = Field(default=None, alias="ttsVoice", max_length=80)
    llm_provider: str | None = Field(default=None, alias="llmProvider", max_length=40)
    llm_model: str | None = Field(default=None, alias="llmModel", max_length=80)

    model_config = {"populate_by_name": True}


def _serialize_voice_settings(s: VoiceBotSetting) -> dict:
    return {
        "botId": s.bot_id,
        "voiceId": s.voice_id,
        "speed": s.speed,
        "pauseMs": s.pause_ms,
        "empathy": s.empathy,
        "energy": s.energy,
        "languageVoiceMap": s.language_voice_map or {},
        "sttProvider": s.stt_provider,
        "sttModel": s.stt_model,
        "ttsProvider": s.tts_provider,
        "ttsModel": s.tts_model,
        "ttsVoice": s.tts_voice,
        "llmProvider": s.llm_provider,
        "llmModel": s.llm_model,
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
    return ok(_serialize_voice_settings(s))


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
    before = _serialize_voice_settings(s)
    if body.voice_id is not None:
        if body.voice_id and db.get(VoiceProfile, body.voice_id) is None:
            raise ApiError("Unknown voice profile.", 422)
        s.voice_id = body.voice_id or None
        bot.voice_id = s.voice_id
    from backend.providers.factory import _REGISTRY as _provider_registry

    for kind, value in (("stt", body.stt_provider), ("tts", body.tts_provider),
                        ("llm", body.llm_provider)):
        if value and (kind, value.lower()) not in _provider_registry:
            raise ApiError(f"Unknown {kind} provider '{value}'.", 422)
    for field in (
        "speed", "pause_ms", "empathy", "energy", "language_voice_map",
        "stt_provider", "stt_model", "tts_provider", "tts_model", "tts_voice",
        "llm_provider", "llm_model",
    ):
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
        previous_value=before, new_value=_serialize_voice_settings(s), request=request,
    )
    db.commit()
    from backend.voice_runtime.bot_config import invalidate_bot_config_sync

    invalidate_bot_config_sync(bot.tenant_id, bot.id)
    return ok(_serialize_voice_settings(s))
