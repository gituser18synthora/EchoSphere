"""Human speech naturalness configuration end-to-end.

- persistence + strict validation through /bots/{id}/voice-settings;
- tenant-wide override through /tenant/settings;
- ResolvedBotConfig.human_speech carries the fully merged result
  (platform defaults <- tenant override <- bot override).
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
from shared.orchestration.naturalness import HUMAN_SPEECH_DEFAULTS

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


@pytest.fixture()
def bot():
    session = get_sessionmaker()()
    row = VoiceBot(
        id=new_id("bot"), tenant_id=TENANT, name=f"HumanSpeech {uuid.uuid4().hex[:6]}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(row)
    session.flush()
    session.add(BotLanguage(bot_id=row.id, language_code="hi-IN"))
    session.commit()
    bot_id = row.id
    yield bot_id, session
    session.query(BotLanguage).filter(BotLanguage.bot_id == bot_id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == bot_id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == bot_id).delete()
    session.commit()
    session.close()


@pytest.fixture()
def tenant_override():
    """Set a tenant-level human_speech override; restore afterwards."""
    session = get_sessionmaker()()
    setting = session.scalar(
        select(TenantSetting).where(TenantSetting.tenant_id == TENANT)
    )
    created = setting is None
    if created:
        setting = TenantSetting(id=new_id("tset"), tenant_id=TENANT)
        session.add(setting)
        session.flush()
    previous = setting.human_speech
    setting.human_speech = {
        "backchannel_probability": 0.6,
        "self_correction": True,
    }
    session.commit()
    yield
    setting = session.scalar(
        select(TenantSetting).where(TenantSetting.tenant_id == TENANT)
    )
    setting.human_speech = previous
    session.commit()
    session.close()


def data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


class TestBotSettingsApi:
    def test_round_trip_and_default_empty(self, client, tenant_admin, bot):
        bot_id, _ = bot
        got = data(client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin))
        assert got["humanSpeech"] == {}

        saved = data(client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
            json={"humanSpeech": {"backchannels": False,
                                  "thinking_filler_probability": 0.5}},
        ))
        assert saved["humanSpeech"] == {
            "backchannels": False, "thinking_filler_probability": 0.5,
        }

    def test_invalid_override_is_rejected(self, client, tenant_admin, bot):
        bot_id, _ = bot
        response = client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
            json={"humanSpeech": {"enabled": "yes", "unknown_key": 1}},
        )
        assert response.status_code == 422
        messages = str(response.json())
        assert "unknown_key" in messages

    def test_empty_object_clears_override(self, client, tenant_admin, bot):
        bot_id, _ = bot
        client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
            json={"humanSpeech": {"backchannels": False}},
        )
        cleared = data(client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
            json={"humanSpeech": {}},
        ))
        assert cleared["humanSpeech"] == {}


class TestTenantSettingsApi:
    def test_tenant_round_trip_and_validation(self, client, tenant_admin):
        got = data(client.put(
            f"{API}/tenant/settings", headers=tenant_admin,
            json={"humanSpeech": {"backchannel_probability": 0.2}},
        ))
        assert got["humanSpeech"] == {"backchannel_probability": 0.2}

        bad = client.put(
            f"{API}/tenant/settings", headers=tenant_admin,
            json={"humanSpeech": {"backchannel_probability": 7}},
        )
        assert bad.status_code == 422

        # Restore: clear the tenant override.
        cleared = data(client.put(
            f"{API}/tenant/settings", headers=tenant_admin,
            json={"humanSpeech": {}},
        ))
        assert cleared["humanSpeech"] == {}


class TestResolution:
    def test_platform_defaults_without_overrides(self, bot):
        bot_id, _ = bot
        config = _load_config_sync(bot_id, require_published=False)
        assert config.human_speech == HUMAN_SPEECH_DEFAULTS

    def test_tenant_then_bot_override_wins(self, bot, tenant_override):
        bot_id, session = bot
        config = _load_config_sync(bot_id, require_published=False)
        # Tenant layer applied on top of platform defaults.
        assert config.human_speech["backchannel_probability"] == 0.6
        assert config.human_speech["self_correction"] is True
        assert config.human_speech["enabled"] is True  # untouched default

        # Bot layer outranks the tenant layer per key.
        session.add(VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot_id, tenant_id=TENANT,
            speed=1.0, pause_ms=150, empathy=0, energy=0,
            human_speech={"backchannel_probability": 0.1, "backchannels": False},
        ))
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.human_speech["backchannel_probability"] == 0.1
        assert config.human_speech["backchannels"] is False
        assert config.human_speech["self_correction"] is True  # tenant layer

    def test_snapshot_round_trips_through_cache_json(self, bot):
        bot_id, _ = bot
        config = _load_config_sync(bot_id, require_published=False)
        restored = type(config).from_json(config.to_json())
        assert restored.human_speech == config.human_speech
