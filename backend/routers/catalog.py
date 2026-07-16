"""Platform catalogs: voice profiles and supported languages."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.deps import get_current_user
from backend.core.responses import ok
from backend.db.mysql import get_db
from backend.models import SupportedLanguage, User, VoiceProfile
from backend.serializers import serialize_language, serialize_voice

router = APIRouter(tags=["Catalog"])


@router.get("/voices")
def list_voices(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(VoiceProfile)
        .where(VoiceProfile.is_deleted.is_(False), VoiceProfile.status == "active")
        .order_by(VoiceProfile.name)
    ).all()
    return ok([serialize_voice(v) for v in rows])


@router.get("/languages")
def list_languages(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(SupportedLanguage)
        .where(SupportedLanguage.enabled.is_(True))
        .order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
    ).all()
    return ok([serialize_language(l) for l in rows])
