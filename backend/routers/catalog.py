"""Platform catalogs: voice profiles and supported languages (tenant-facing,
read-only — management lives in the Super Admin master-data API)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.core.deps import get_current_user
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import SupportedLanguage, User, VoiceProfile
from backend.serializers import serialize_language, serialize_voice

router = APIRouter(tags=["Catalog"])


@router.get("/voices")
def list_voices(
    provider: str | None = Query(None),
    language: str | None = Query(None),
    locale: str | None = Query(None),
    gender: str | None = Query(None),
    search: str | None = Query(None, max_length=100),
    include_inactive: bool = Query(False, alias="includeInactive"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(VoiceProfile).where(VoiceProfile.is_deleted.is_(False))
    if not include_inactive:
        stmt = stmt.where(VoiceProfile.status == "active")
    if provider:
        stmt = stmt.where(VoiceProfile.provider == provider)
    if locale:
        stmt = stmt.where(VoiceProfile.locale == locale)
    if gender:
        stmt = stmt.where(VoiceProfile.gender == gender)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(VoiceProfile.name.like(like), VoiceProfile.accent.like(like)))
    rows = db.scalars(stmt.order_by(VoiceProfile.sort_order, VoiceProfile.name)).all()
    if language:
        # languages is a JSON list — filter in Python to stay portable.
        rows = [v for v in rows if language in (v.languages or [])]
    return ok([serialize_voice(v) for v in rows])


@router.get("/languages")
def list_languages(
    include_disabled: bool = Query(False, alias="includeDisabled"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(SupportedLanguage)
    if not include_disabled:
        stmt = stmt.where(SupportedLanguage.enabled.is_(True))
    rows = db.scalars(
        stmt.order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
    ).all()
    return ok([serialize_language(l) for l in rows])
