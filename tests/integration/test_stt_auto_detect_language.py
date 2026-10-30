"""STT auto-detect language: derived multilingual default, explicit choice,
API exposure and runtime resolution — end to end through the real DB.

- a bot with more than one language auto-detects by default (nothing stored);
- a single-language bot pins by default;
- an explicit ``false``/``true`` saved through /voice-settings wins and
  survives reload; omitting the key returns to the derived default;
- ResolvedBotConfig stamps the EFFECTIVE value so the runtime never has to
  guess (and cached snapshots agree);
- a bot without its own languages inherits the tenant's default languages.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.bot_config import _load_config_sync
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import BotLanguage, TenantSetting, User, VoiceBot, VoiceBotSetting

pytestmark = pytest.mark.integration

API = "/api/v1"
TENANT = "tn-001"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tenant_admin():
    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(User.email == "priya.sharma@meridianhealth.com"))
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


def _make_bot(session, languages: list[str]) -> str:
    row = VoiceBot(
        id=new_id("bot"), tenant_id=TENANT, name=f"Auto-detect {uuid.uuid4().hex[:6]}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(row)
    session.flush()
    for code in languages:
        session.add(BotLanguage(bot_id=row.id, language_code=code))
    session.commit()
    return row.id


def _cleanup(session, bot_id: str) -> None:
    session.query(BotLanguage).filter(BotLanguage.bot_id == bot_id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == bot_id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == bot_id).delete()
    session.commit()


@pytest.fixture()
def multilingual_bot():
    session = get_sessionmaker()()
    bot_id = _make_bot(session, ["hi-IN", "en-IN"])
    yield bot_id, session
    _cleanup(session, bot_id)
    session.close()


@pytest.fixture()
def single_language_bot():
    session = get_sessionmaker()()
    bot_id = _make_bot(session, ["hi-IN"])
    yield bot_id, session
    _cleanup(session, bot_id)
    session.close()


@pytest.fixture()
def bot_without_languages():
    session = get_sessionmaker()()
    bot_id = _make_bot(session, [])
    yield bot_id, session
    _cleanup(session, bot_id)
    session.close()


def _sarvam_stt_payload(**settings) -> dict:
    return {
        "sttProvider": "sarvam",
        "sttModel": "saaras:v3",
        "sttLanguage": "",
        "sttSettings": {"mode": "transcribe", "vad_signals": True, **settings},
    }


def test_multilingual_bot_defaults_to_auto_detect(client, tenant_admin, multilingual_bot):
    bot_id, _ = multilingual_bot
    body = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin).json()["data"]
    assert body["sttAutoDetectLanguage"] == {
        "value": None, "effective": True, "source": "derived",
        "derivedDefault": True, "languages": ["en-IN", "hi-IN"],
    }
    # Runtime view: effective value stamped, nothing invented on disk.
    config = _load_config_sync(bot_id, require_published=False)
    assert config.stt["settings"]["auto_detect_language"] is True
    assert config.stt["auto_detect_language"]["source"] == "derived"
    assert config.languages == ["en-IN", "hi-IN"]


def test_single_language_bot_defaults_to_pinned(client, tenant_admin, single_language_bot):
    bot_id, _ = single_language_bot
    body = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin).json()["data"]
    assert body["sttAutoDetectLanguage"]["effective"] is False
    assert body["sttAutoDetectLanguage"]["source"] == "derived"
    config = _load_config_sync(bot_id, require_published=False)
    assert config.stt["settings"]["auto_detect_language"] is False


def test_saving_without_the_key_keeps_following_the_derived_default(
    client, tenant_admin, multilingual_bot,
):
    bot_id, session = multilingual_bot
    resp = client.put(
        f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json=_sarvam_stt_payload(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "auto_detect_language" not in data["sttSettings"]
    assert data["sttAutoDetectLanguage"]["effective"] is True
    assert data["sttAutoDetectLanguage"]["source"] == "derived"
    config = _load_config_sync(bot_id, require_published=False)
    assert config.stt["settings"]["auto_detect_language"] is True
    # Dropping to one language flips the derived default without any save.
    session.query(BotLanguage).filter(
        BotLanguage.bot_id == bot_id, BotLanguage.language_code == "en-IN"
    ).delete()
    session.commit()
    body = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin).json()["data"]
    assert body["sttAutoDetectLanguage"]["effective"] is False
    assert body["sttAutoDetectLanguage"]["derivedDefault"] is False


def test_explicit_off_is_preserved_for_a_multilingual_bot(client, tenant_admin, multilingual_bot):
    bot_id, _ = multilingual_bot
    resp = client.put(
        f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
        json=_sarvam_stt_payload(auto_detect_language=False),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["sttSettings"]["auto_detect_language"] is False
    assert data["sttAutoDetectLanguage"] == {
        "value": False, "effective": False, "source": "explicit",
        "derivedDefault": True, "languages": ["en-IN", "hi-IN"],
    }
    # Survives reload and reaches the runtime as an explicit pin.
    body = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin).json()["data"]
    assert body["sttAutoDetectLanguage"]["effective"] is False
    config = _load_config_sync(bot_id, require_published=False)
    assert config.stt["settings"]["auto_detect_language"] is False
    assert config.stt["auto_detect_language"]["source"] == "explicit"


def test_explicit_on_is_preserved_for_a_single_language_bot(
    client, tenant_admin, single_language_bot,
):
    bot_id, _ = single_language_bot
    resp = client.put(
        f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
        json=_sarvam_stt_payload(auto_detect_language=True),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["sttAutoDetectLanguage"]["effective"] is True
    assert resp.json()["data"]["sttAutoDetectLanguage"]["source"] == "explicit"
    config = _load_config_sync(bot_id, require_published=False)
    assert config.stt["settings"]["auto_detect_language"] is True


def test_toggling_back_to_automatic_removes_the_key(client, tenant_admin, multilingual_bot):
    bot_id, _ = multilingual_bot
    client.put(
        f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
        json=_sarvam_stt_payload(auto_detect_language=False),
    )
    resp = client.put(
        f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json=_sarvam_stt_payload(),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "auto_detect_language" not in data["sttSettings"]
    assert data["sttAutoDetectLanguage"]["source"] == "derived"
    assert data["sttAutoDetectLanguage"]["effective"] is True


def test_bot_without_languages_inherits_tenant_defaults(
    client, tenant_admin, bot_without_languages,
):
    bot_id, session = bot_without_languages
    settings = session.scalar(select(TenantSetting).where(TenantSetting.tenant_id == TENANT))
    assert settings is not None
    previous = settings.default_languages
    settings.default_languages = ["hi-IN", "en-IN"]
    session.commit()
    try:
        body = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin).json()["data"]
        assert body["sttAutoDetectLanguage"]["effective"] is True
        assert body["sttAutoDetectLanguage"]["languages"] == ["hi-IN", "en-IN"]
        config = _load_config_sync(bot_id, require_published=False)
        assert config.languages == ["hi-IN", "en-IN"]
        assert config.language == "hi-IN"  # inherited default, never a bare "en"
        assert config.stt["settings"]["auto_detect_language"] is True

        settings.default_languages = ["hi-IN"]
        session.commit()
        body = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin).json()["data"]
        assert body["sttAutoDetectLanguage"]["effective"] is False
    finally:
        settings.default_languages = previous
        session.commit()


def _sarvam_stt_payload_ml(**settings) -> dict:
    return {
        "sttProvider": "sarvam", "sttModel": "saaras:v3", "sttLanguage": "",
        "sttSettings": {"mode": "transcribe", "vad_signals": True, "high_vad_sensitivity": False, **settings},
    }


@pytest.fixture()
def malayalam_bot():
    session = get_sessionmaker()()
    bot_id = _make_bot(session, ["en-IN", "hi-IN", "ml-IN"])
    yield bot_id, session
    _cleanup(session, bot_id)
    session.close()


def test_unrelated_stt_change_keeps_the_tristate_absent(client, tenant_admin, malayalam_bot):
    bot_id, session = malayalam_bot
    client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json=_sarvam_stt_payload_ml())
    resp = client.put(
        f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
        json=_sarvam_stt_payload_ml(high_vad_sensitivity=True),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["sttSettings"]["high_vad_sensitivity"] is True
    assert "auto_detect_language" not in data["sttSettings"]
    assert data["sttAutoDetectLanguage"]["source"] == "derived"
    assert data["sttAutoDetectLanguage"]["effective"] is True
    # Languages (incl. Malayalam) are untouched by a voice-settings save.
    assert data["sttAutoDetectLanguage"]["languages"] == ["en-IN", "hi-IN", "ml-IN"]
    langs = session.scalars(select(BotLanguage.language_code).where(BotLanguage.bot_id == bot_id)).all()
    assert sorted(langs) == ["en-IN", "hi-IN", "ml-IN"]
    config = _load_config_sync(bot_id, require_published=False)
    assert config.languages == ["en-IN", "hi-IN", "ml-IN"]
    assert config.stt["settings"]["auto_detect_language"] is True


def test_unrelated_change_keeps_an_explicit_true(client, tenant_admin, malayalam_bot):
    bot_id, _ = malayalam_bot
    client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
               json=_sarvam_stt_payload_ml(auto_detect_language=True))
    resp = client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
                      json=_sarvam_stt_payload_ml(auto_detect_language=True, high_vad_sensitivity=True))
    data = resp.json()["data"]
    assert data["sttSettings"]["auto_detect_language"] is True
    assert data["sttAutoDetectLanguage"]["source"] == "explicit"


def test_non_stt_save_does_not_materialize_false(client, tenant_admin, malayalam_bot):
    """A save that touches only delivery tuning never writes auto_detect_language."""
    bot_id, _ = malayalam_bot
    client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json=_sarvam_stt_payload_ml())
    resp = client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={"speed": 1.1, "pauseMs": 400})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "auto_detect_language" not in data["sttSettings"]
    assert data["sttAutoDetectLanguage"]["effective"] is True


def test_explicit_stt_language_is_reported_and_pins_at_runtime(client, tenant_admin, malayalam_bot):
    bot_id, _ = malayalam_bot
    payload = _sarvam_stt_payload_ml(); payload["sttLanguage"] = "ml-IN"
    resp = client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json=payload)
    assert resp.status_code == 200, resp.text
    config = _load_config_sync(bot_id, require_published=False)
    assert config.stt["language"] == "ml-IN"
    # auto-detect derived ON, but an explicit fixed STT language outranks it.
    from shared.providers.stt_language_policy import resolve_auto_detect_language, stt_language_mode
    decision = resolve_auto_detect_language(config.stt["settings"], config.languages)
    assert decision.enabled is True
    assert stt_language_mode(config.stt["language"], decision, config.language) == ("pinned", "ml-IN")
