"""The catalog seed fills a MISSING default model and never replaces one.

On a fresh install the catalog migrations run first and insert their model
non-default (saaras:v4 from a9c1e3b5d7f9); the seed then created saaras:v3 as
non-default too (87781e5c8 moved the default to v4) and skipped the existing
v4 row — leaving speech-to-text with no default model at all.
"""

import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.seeds.provider_catalog_seed import seed_provider_catalog
from shared.models import ProviderModel, SupportedLanguage, VoiceProfile
from shared.models.base import Base


def _db() -> Session:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        ProviderModel.__table__, VoiceProfile.__table__, SupportedLanguage.__table__,
    ])
    return Session(engine)


def _model(code: str, *, default: bool) -> ProviderModel:
    return ProviderModel(
        id=f"pm_{code}", provider_code="sarvam", capability="stt", code=code,
        display_name=code, is_default=default, status="active", sort_order=0,
        languages=[], codecs=[], sample_rates=[], streaming=True, params_schema={},
    )


def _stt_defaults(db: Session) -> dict:
    return {
        m.code: m.is_default for m in db.query(ProviderModel).filter(
            ProviderModel.provider_code == "sarvam", ProviderModel.capability == "stt",
        )
    }


def test_fresh_install_gets_the_seed_default():
    with _db() as db:
        db.add(_model("saaras:v4", default=False))   # inserted by the migration
        db.commit()
        seed_provider_catalog(db, include_dev_providers=False)
        db.commit()
        defaults = _stt_defaults(db)
    assert defaults["saaras:v4"] is True
    assert defaults["saaras:v3"] is False


def test_an_existing_default_is_never_replaced():
    with _db() as db:
        db.add_all([_model("saaras:v3", default=True), _model("saaras:v4", default=False)])
        db.commit()
        seed_provider_catalog(db, include_dev_providers=False)
        db.commit()
        defaults = _stt_defaults(db)
    assert defaults == {**defaults, "saaras:v3": True, "saaras:v4": False}
