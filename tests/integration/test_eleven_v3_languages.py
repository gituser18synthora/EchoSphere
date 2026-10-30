"""Eleven v3 language options are governed by the supported_languages table.

Covers: en-US/en-IN availability as separate records, enabled-only options,
exclusion of languages without catalog records, model/language combination
rejection, seed idempotency (no duplicate language or model rows), and
preservation of existing configurations whose language was deactivated later.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.core.provider_catalog import validate_voice_settings
from backend.core.security import create_access_token
from backend.main import app
from backend.seeds.provider_catalog_seed import (
    _LEGACY_ELEVEN_V3_BARE_CODES,
    seed_provider_catalog,
)
from shared.bot_config import _load_config_sync
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import (
    BotLanguage,
    ProviderModel,
    SupportedLanguage,
    User,
    VoiceBot,
    VoiceBotSetting,
)

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def bearer(email: str) -> dict:
    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(User.email == email))
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture(scope="module")
def tenant_admin():
    return bearer("priya.sharma@meridianhealth.com")


def data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


class _LanguageState:
    """Flip supported_languages.enabled and restore the original state."""

    def __init__(self):
        self._original: dict[str, bool] = {}

    def set_enabled(self, code: str, enabled: bool):
        db = get_sessionmaker()()
        try:
            row = db.scalar(select(SupportedLanguage).where(SupportedLanguage.code == code))
            assert row is not None, f"language {code} missing from catalog"
            if code not in self._original:
                self._original[code] = row.enabled
            row.enabled = enabled
            db.commit()
        finally:
            db.close()

    def restore(self):
        db = get_sessionmaker()()
        try:
            for code, enabled in self._original.items():
                row = db.scalar(
                    select(SupportedLanguage).where(SupportedLanguage.code == code)
                )
                if row is not None:
                    row.enabled = enabled
            db.commit()
        finally:
            db.close()


@pytest.fixture()
def language_state():
    state = _LanguageState()
    yield state
    state.restore()


def v3_language_codes(client, headers) -> set[str]:
    payload = data(client.get(
        f"{API}/providers/tts/elevenlabs/models/eleven_v3/languages", headers=headers,
    ))
    return {lang["code"] for lang in payload["languages"]}


class TestElevenV3LanguageOptions:
    def test_en_us_and_en_in_available_as_separate_records(
        self, client, tenant_admin, language_state
    ):
        language_state.set_enabled("en-US", True)
        language_state.set_enabled("en-IN", True)
        codes = v3_language_codes(client, tenant_admin)
        assert "en-US" in codes
        assert "en-IN" in codes

    def test_active_supported_languages_are_returned(
        self, client, tenant_admin, language_state
    ):
        language_state.set_enabled("hi-IN", True)
        language_state.set_enabled("bn-IN", True)
        codes = v3_language_codes(client, tenant_admin)
        assert {"hi-IN", "bn-IN"} <= codes

    def test_inactive_languages_are_excluded(self, client, tenant_admin, language_state):
        language_state.set_enabled("bn-IN", True)
        assert "bn-IN" in v3_language_codes(client, tenant_admin)
        language_state.set_enabled("bn-IN", False)
        assert "bn-IN" not in v3_language_codes(client, tenant_admin)

    def test_languages_absent_from_table_are_excluded(self, client, tenant_admin):
        """Officially supported v3 languages without a catalog record (e.g.
        Afrikaans, Japanese) never appear — neither in the options endpoint
        nor on the stored model row."""
        db = get_sessionmaker()()
        try:
            catalog_codes = set(db.scalars(select(SupportedLanguage.code)).all())
            row = db.scalar(select(ProviderModel).where(
                ProviderModel.provider_code == "elevenlabs",
                ProviderModel.capability == "tts",
                ProviderModel.code == "eleven_v3",
            ))
            assert row is not None
            stored = set(row.languages or [])
        finally:
            db.close()
        assert stored, "eleven_v3 row must list catalog locales"
        assert stored <= catalog_codes  # every stored locale is a table record
        offered = v3_language_codes(client, tenant_admin)
        assert offered <= catalog_codes
        for absent in ("af", "ja", "ja-JP", "sw", "af-ZA"):
            assert absent not in stored and absent not in offered

    def test_unsupported_model_language_combination_rejected(self, client, tenant_admin):
        # Odia exists in the catalog but is NOT an Eleven v3 language — the
        # combination must fail model-language validation server-side.
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {
                "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
                "ttsVoice": "vp-el-monika",
                "languageVoiceMap": {
                    "or-IN": {"provider": "elevenlabs", "model": "eleven_v3",
                              "voice": "vp-el-monika"},
                },
            },
        }))
        assert not result["valid"]
        assert any(
            "or-IN" in e and "is not supported by elevenlabs/eleven_v3" in e
            for e in result["errors"]
        )
        # Flash does not cover Odia either — bare-ISO matching still applies.
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {
                "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
                "ttsVoice": "vp-el-monika",
                "languageVoiceMap": {
                    "or-IN": {"provider": "elevenlabs", "model": "eleven_flash_v2_5",
                              "voice": "vp-el-monika"},
                },
            },
        }))
        assert not result["valid"]
        assert any(
            "or-IN" in e and "is not supported by elevenlabs/eleven_flash_v2_5" in e
            for e in result["errors"]
        )

    def test_language_missing_from_catalog_rejected(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {"sttProvider": "sarvam", "sttModel": "saaras:v3",
                       "sttLanguage": "xx-XX"},
        }))
        assert not result["valid"]
        assert any(
            "xx-XX" in e and "not in the platform language catalog" in e
            for e in result["errors"]
        )


class TestSeedIdempotency:
    def test_no_duplicate_language_or_model_rows(self):
        from backend.seeds.base_seed import run_base_seed

        run_base_seed()
        run_base_seed()
        db = get_sessionmaker()()
        try:
            for code in ("en-US", "en-IN"):
                count = db.scalar(
                    select(func.count()).select_from(SupportedLanguage)
                    .where(SupportedLanguage.code == code)
                )
                assert count == 1, f"duplicate language rows for {code}"
            count = db.scalar(
                select(func.count()).select_from(ProviderModel).where(
                    ProviderModel.provider_code == "elevenlabs",
                    ProviderModel.capability == "tts",
                    ProviderModel.code == "eleven_v3",
                )
            )
            assert count == 1
        finally:
            db.close()

    def test_legacy_bare_code_row_converts_once_then_stays_stable(self):
        """A pre-catalog row (bare ISO list) is converted to catalog locales
        exactly once; re-seeding neither duplicates nor rewrites it again."""
        db = get_sessionmaker()()
        try:
            row = db.scalar(select(ProviderModel).where(
                ProviderModel.provider_code == "elevenlabs",
                ProviderModel.capability == "tts",
                ProviderModel.code == "eleven_v3",
            ))
            assert row is not None
            original = list(row.languages or [])
            try:
                row.languages = list(_LEGACY_ELEVEN_V3_BARE_CODES)
                db.commit()

                seed_provider_catalog(db)
                db.commit()
                db.refresh(row)
                converted = list(row.languages or [])
                assert converted != _LEGACY_ELEVEN_V3_BARE_CODES
                assert "en-US" in converted and "en-IN" in converted
                assert len(converted) == len(set(converted))

                seed_provider_catalog(db)
                db.commit()
                db.refresh(row)
                assert list(row.languages or []) == converted  # stable
            finally:
                row.languages = original
                db.commit()
        finally:
            db.close()


_SUFFIX = uuid.uuid4().hex[:8]


@pytest.fixture()
def legacy_language_bot(language_state):
    """Bot configured (while enabled) for a language that gets disabled later."""
    language_state.set_enabled("gu-IN", True)
    language_state.set_enabled("en-IN", True)
    session = get_sessionmaker()()
    bot = VoiceBot(
        id=new_id("bot"), tenant_id="tn-001", name=f"LangGov {_SUFFIX}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(bot)
    session.flush()
    session.add(BotLanguage(bot_id=bot.id, language_code="en-IN"))
    session.add(BotLanguage(bot_id=bot.id, language_code="gu-IN"))
    vbs = VoiceBotSetting(
        id=new_id("vbs"), bot_id=bot.id, tenant_id="tn-001",
        stt_provider="sarvam", stt_model="saaras:v3", stt_language="gu-IN",
        tts_provider="sarvam", tts_model="bulbul:v3", tts_voice="vp-sv-shubh",
        llm_provider="mock", llm_model="mock",
        language_voice_map={
            "default": "en-IN",
            "gu-IN": {"provider": "sarvam", "model": "bulbul:v3", "voice": "vp-sv-ritu"},
        },
    )
    session.add(vbs)
    session.commit()
    yield bot.id, session
    session.query(BotLanguage).filter(BotLanguage.bot_id == bot.id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == bot.id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == bot.id).delete()
    session.commit()
    session.close()


class TestDeactivatedLanguagePreservation:
    def _payload(self, session, bot_id, **overrides):
        vbs = session.query(VoiceBotSetting).filter(
            VoiceBotSetting.bot_id == bot_id
        ).one()
        payload = {
            "stt_provider": vbs.stt_provider, "stt_model": vbs.stt_model,
            "stt_language": vbs.stt_language,
            "tts_provider": vbs.tts_provider, "tts_model": vbs.tts_model,
            "tts_voice": vbs.tts_voice,
            "language_voice_map": vbs.language_voice_map,
        }
        payload.update(overrides)
        return payload

    def test_existing_config_survives_language_deactivation(
        self, legacy_language_bot, language_state
    ):
        bot_id, session = legacy_language_bot
        bot = session.get(VoiceBot, bot_id)

        errors, warnings = validate_voice_settings(
            session, bot, self._payload(session, bot_id)
        )
        assert errors == []

        language_state.set_enabled("gu-IN", False)
        # End the open REPEATABLE READ snapshot so the other session's commit
        # becomes visible, then drop cached ORM state.
        session.rollback()
        session.expire_all()
        bot = session.get(VoiceBot, bot_id)
        errors, warnings = validate_voice_settings(
            session, bot, self._payload(session, bot_id)
        )
        # Preserved: the unchanged configuration produces warnings, not errors.
        assert errors == []
        joined = " ".join(warnings)
        assert "gu-IN" in joined and "disabled on the platform" in joined

        # Runtime resolution keeps working for live calls.
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["language_map"]["gu-IN"]["voice"] == "ritu"
        assert config.tts["voice_name"] == "Shubh"
        assert config.tts["voice_gender"] == "male"
        assert config.tts["language_map"]["gu-IN"]["voice_name"] == "Ritu"
        assert config.tts["language_map"]["gu-IN"]["voice_gender"] == "female"

    def test_new_selection_of_disabled_language_rejected(
        self, legacy_language_bot, language_state
    ):
        bot_id, session = legacy_language_bot
        language_state.set_enabled("te-IN", False)
        session.rollback()
        session.expire_all()
        bot = session.get(VoiceBot, bot_id)
        # stt_language CHANGED to a disabled language → hard error.
        errors, _ = validate_voice_settings(
            session, bot, self._payload(session, bot_id, stt_language="te-IN")
        )
        assert any(
            "te-IN" in e and "disabled on the platform" in e for e in errors
        )
