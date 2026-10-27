"""bulbul:v4-flash voices in the provider voice catalog.

Selecting bulbul:v4-flash in the Studio showed an empty voice list: the
personas existed only in shared.providers.tts.sarvam_voices, and the seed
never touches existing voice rows. The Hindi and English personas of the
reviewed mapping are now catalog voices of their own (``vp-sv4-<persona>``,
model bulbul:v4-flash only, never Malayalam/Odia), bulbul:v3 stays the only
TTS default, and the seed no longer creates a second default on a database
that already has one.
"""

from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from backend.core.provider_catalog import list_voices
from backend.routers.providers import provider_voices
from backend.seeds import provider_catalog_seed as catalog_seed
from shared.models import ProviderDef, ProviderModel, SupportedLanguage, VoiceProfile
from shared.models.base import Base
from shared.providers.tts.sarvam_voices import (
    V3_TO_V4_PERSONA,
    V4_FLASH_PERSONAS,
    is_v4_persona,
    persona_parts,
    v4_catalog_personas,
    v4_roster_personas,
)

V4 = "bulbul:v4-flash"


def _db() -> Session:
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        ProviderDef.__table__, ProviderModel.__table__, VoiceProfile.__table__,
        SupportedLanguage.__table__,
    ])
    db = Session(engine)
    db.add(ProviderDef(id="pd_sarvam_tts", kind="tts", code="sarvam", name="Sarvam AI", status="active"))
    db.commit()
    return db


def _seed(db: Session) -> None:
    catalog_seed.seed_provider_catalog(db, include_dev_providers=False)
    db.commit()


def _v4_languages(db: Session) -> list[str]:
    row = db.query(ProviderModel).filter_by(provider_code="sarvam", capability="tts", code=V4).one()
    return list(row.languages)


def _endpoint(db: Session, **filters) -> list[dict]:
    user = SimpleNamespace(tenant_id="tn-x", role=SimpleNamespace(code="tenant_admin"))
    return provider_voices(code="sarvam", model=filters.get("model"), language=filters.get("language"),
                           gender=None, user=user, db=db)["data"]


def test_every_v4_flash_language_lists_at_least_one_voice():
    db = _db()
    _seed(db)
    languages = _v4_languages(db)
    assert len(languages) == 9 and "ml-IN" not in languages and "od-IN" not in languages
    for language in languages:
        voices = _endpoint(db, model=V4, language=language)
        assert voices, f"no bulbul:v4-flash voice for {language}"
        assert all(is_v4_persona(v["providerVoiceId"]) and v["modelCodes"] == [V4] for v in voices)


def test_the_catalog_lists_sarvams_whole_v4_flash_roster():
    db = _db()
    _seed(db)
    voices = list_voices(db, "sarvam", model=V4)
    # 222 personas (live API speaker list == docs voice catalog, 2026-10-26):
    # the 36 mapping personas plus the rest of the roster, each once.
    assert len(V4_FLASH_PERSONAS) == len(set(V4_FLASH_PERSONAS)) == 222
    assert sorted(v.provider_voice_id for v in voices) == sorted(V4_FLASH_PERSONAS)
    assert set(v4_catalog_personas()) <= set(V4_FLASH_PERSONAS)
    assert len(v4_roster_personas()) == 222 - len(v4_catalog_personas()) == 186
    # Assamese personas exist on the API but v4-flash rejects as-IN.
    assert not any("_as_" in p for p in V4_FLASH_PERSONAS)
    mapped = {row.get(lang) for row in V3_TO_V4_PERSONA.values() for lang in ("hi", "en")} - {None}
    assert mapped == set(v4_catalog_personas())
    for voice in voices:
        assert voice.id == f"vp-sv4-{voice.provider_voice_id}"
        assert "ml-IN" not in voice.languages and "or-IN" not in voice.languages
        assert voice.is_default is False
    shubh = next(v for v in voices if v.provider_voice_id == "shubh_hi_customer")
    assert (shubh.name, shubh.gender, shubh.locale) == ("Shubh · Hindi · Customer", "male", "hi-IN")
    assert next(v for v in voices if v.provider_voice_id == "ritu_hi_customer").gender == "female"


def test_roster_personas_are_offered_for_their_own_language_only():
    db = _db()
    _seed(db)
    by_wire = {v.provider_voice_id: v for v in list_voices(db, "sarvam", model=V4)}
    expected = {"hi": ["hi-IN"], "enhi": ["hi-IN", "en-IN"], "en": ["en-IN"], "bn": ["bn-IN"],
                "gu": ["gu-IN"], "kn": ["kn-IN"], "mr": ["mr-IN"], "pa": ["pa-IN"],
                "ta": ["ta-IN"], "te": ["te-IN"]}
    for persona in v4_roster_personas():
        voice = by_wire[persona]
        _speaker, tag, _style = persona_parts(persona)
        assert voice.languages == expected[tag], persona
        assert voice.locale == expected[tag][0]
        assert voice.is_default is False and voice.status == "active"
    aparna = by_wire["aparna_hi_customer"]
    assert (aparna.id, aparna.name) == ("vp-sv4-aparna_hi_customer", "Aparna · Hindi · Customer")
    assert by_wire["simran_enhi_banking_expressive"].name == "Simran · Hinglish · Banking expressive"
    tamil = [v.provider_voice_id for v in list_voices(db, "sarvam", model=V4, language="ta-IN")]
    assert {"aravind_ta_ads", "aravind_ta_suspense", "gokul_ta_narration", "vijay_ta_narration"} <= set(tamil)
    assert "aparna_hi_customer" not in tamil


def test_roster_gender_comes_from_the_v3_voice_or_sarvams_docs_never_a_guess():
    db = _db()
    _seed(db)
    by_wire = {v.provider_voice_id: v for v in list_voices(db, "sarvam", model=V4)}
    # Same speaker as a bulbul:v3 catalog voice -> that voice's gender.
    assert by_wire["ritu_hi_sales"].gender == "female"
    assert by_wire["shubh_en_narration"].gender == "male"
    # Gender published on Sarvam's featured voice cards.
    assert by_wire["aparna_hi_kyc"].gender == "female"
    assert by_wire["ashok_hi_news"].gender == "male"
    assert by_wire["sanchita_hi_banking"].gender == "female"
    assert by_wire["shalini_en_customer"].gender == "female"
    # Unpublished: neutral until an operator sets it.
    assert by_wire["aarti_hi_customer"].gender == "neutral"
    assert by_wire["zarina_en_conversation"].gender == "neutral"


def test_malayalam_and_odia_get_no_v4_voice():
    db = _db()
    _seed(db)
    assert _endpoint(db, model=V4, language="ml-IN") == []
    assert _endpoint(db, model=V4, language="or-IN") == []


def test_v3_lists_stay_unchanged():
    db = _db()
    _seed(db)
    v3 = list_voices(db, "sarvam", model="bulbul:v3", language="hi-IN")
    assert v3 and not any(is_v4_persona(v.provider_voice_id) for v in v3)
    assert all(v.model_codes == ["bulbul:v3"] for v in v3)


def test_bulbul_v3_stays_the_only_tts_default():
    db = _db()
    _seed(db)
    defaults = [m.code for m in db.query(ProviderModel).filter_by(
        provider_code="sarvam", capability="tts", is_default=True)]
    assert defaults == ["bulbul:v3"]
    assert catalog_seed._PROFILE_REMAP["tts"] == ("sarvam", "bulbul:v3")


def test_long_lived_database_gets_the_voices_without_a_second_default():
    # The state on local/demo: bulbul:v3 default, v3 voices only.
    db = _db()
    db.add(ProviderModel(
        id="pm_v3", provider_code="sarvam", capability="tts", code="bulbul:v3",
        display_name="Bulbul v3", is_default=True, status="active", sort_order=0,
        languages=["hi-IN"], codecs=[], sample_rates=[], streaming=True, params_schema={},
    ))
    db.add(VoiceProfile(id="vp-sv-shubh", name="Shubh", gender="male", provider="sarvam",
                        provider_voice_id="shubh", model_codes=["bulbul:v3"], languages=["hi-IN"]))
    db.commit()
    _seed(db)
    rows = {m.code: m.is_default for m in db.query(ProviderModel).filter_by(provider_code="sarvam", capability="tts")}
    assert rows["bulbul:v3"] is True and rows[V4] is False
    assert db.get(VoiceProfile, "vp-sv-shubh").model_codes == ["bulbul:v3"]
    hindi = [p for p in v4_roster_personas() if persona_parts(p)[1] in ("hi", "enhi")]
    assert len(list_voices(db, "sarvam", model=V4, language="hi-IN")) == len(v4_catalog_personas()) + len(hindi)


@pytest.mark.parametrize("language", ["hi-IN", "en-IN"])
def test_lists_start_with_shubh_and_include_native_personas(language):
    db = _db()
    _seed(db)
    voices = list_voices(db, "sarvam", model=V4, language=language)
    assert voices[0].provider_voice_id == "shubh_hi_customer"   # the v3 default voice's persona
    assert any(v.locale == language for v in voices)


@pytest.mark.parametrize("persona, locale, name, gender", [
    ("aparna_hi_customer", "hi-IN", "Aparna", "female"),
    ("shalini_enhi_customer", "en-IN", "Shalini", "female"),
    ("aravind_ta_ads", "ta-IN", "Aravind", "neutral"),
    ("ritu_hi_sales", "hi-IN", "Ritu", "female"),
])
def test_a_roster_voice_resolves_to_its_persona_for_the_runtime(persona, locale, name, gender):
    """Saved catalog id -> wire speaker + prompt identity, as the live call uses them."""
    from backend.core.provider_catalog import find_voice
    from shared import bot_config
    from shared.orchestration.voice_identity import active_voice_identity
    from shared.providers.tts.sarvam_voices import v4_speaker

    db = _db()
    _seed(db)
    voice_id = f"vp-sv4-{persona}"
    row = find_voice(db, "sarvam", voice_id)
    assert row is not None and V4 in row.model_codes and locale in row.languages  # validation inputs
    wire = bot_config._wire_voice(db, "sarvam", voice_id, "tn-x")
    assert wire == persona and v4_speaker(wire, locale) == persona
    identity = bot_config._voice_identity(db, "sarvam", voice_id, "tn-x")
    resolved = active_voice_identity(
        {"provider": "sarvam", "model": V4, "voice": wire,
         "voice_name": identity.name, "voice_gender": identity.gender}, locale)
    assert (resolved.name, resolved.gender) == (name, gender)


def test_every_roster_persona_is_sent_to_sarvam_unchanged():
    from shared.providers.tts.sarvam_voices import v4_speaker

    for persona in V4_FLASH_PERSONAS:
        _speaker, tag, _style = persona_parts(persona)
        assert is_v4_persona(persona)
        assert v4_speaker(persona, "hi-IN" if tag in ("hi", "enhi") else "en-IN") == persona
