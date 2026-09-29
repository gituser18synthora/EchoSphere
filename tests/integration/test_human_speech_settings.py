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


@pytest.fixture()
def tenant_ambience():
    """Tenant-level Natural Conversation defaults for ambience; the previous
    tenant value is restored (and a row this fixture created is removed)."""
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
        **(previous or {}),
        "background_ambience": True,
        "background_ambience_preset": "call_center",
        "background_ambience_volume": 70,
    }
    session.commit()
    yield
    setting = session.scalar(
        select(TenantSetting).where(TenantSetting.tenant_id == TENANT)
    )
    if created:
        session.delete(setting)
    else:
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
        assert got["humanSpeechEffective"] == HUMAN_SPEECH_DEFAULTS
        assert set(got["humanSpeechSources"].values()) == {"platform"}
        assert got["humanSpeechInherited"] == HUMAN_SPEECH_DEFAULTS

        saved = data(client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin,
            json={"humanSpeech": {"backchannels": False,
                                  "thinking_filler_probability": 0.5}},
        ))
        assert saved["humanSpeech"] == {
            "backchannels": False, "thinking_filler_probability": 0.5,
        }
        assert saved["humanSpeechEffective"]["backchannels"] is False
        assert saved["humanSpeechSources"]["backchannels"] == "bot"
        assert saved["humanSpeechSources"]["thinking_filler_probability"] == "bot"
        assert saved["humanSpeechSources"]["enabled"] == "platform"

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


class TestBackgroundAmbienceSetting:
    """Natural Conversation → Background ambience: off by default, a sparse
    per-bot override through the existing voice-settings API, and the switch
    the voice runtime reads when it builds the call's output."""

    def test_off_by_default_persisted_per_bot_and_read_by_the_runtime(
        self, client, tenant_admin, bot,
    ):
        from voice_runtime.ambience import build_ambience

        bot_id, _ = bot
        url = f"{API}/bots/{bot_id}/voice-settings"
        got = data(client.get(url, headers=tenant_admin))
        assert got["humanSpeechEffective"]["background_ambience"] is False
        assert got["humanSpeechSources"]["background_ambience"] == "platform"
        config = _load_config_sync(bot_id, require_published=False)
        assert build_ambience(config, transport_kind="telephony", sample_rate=8000) is None

        saved = data(client.put(
            url, headers=tenant_admin, json={"humanSpeech": {"background_ambience": True}},
        ))
        assert saved["humanSpeech"] == {"background_ambience": True}
        assert saved["humanSpeechEffective"]["background_ambience"] is True
        assert saved["humanSpeechSources"]["background_ambience"] == "bot"
        config = _load_config_sync(bot_id, require_published=False)
        assert config.human_speech["background_ambience"] is True
        for kind, rate in (("telephony", 8000), ("browser", 16000), ("browser", 24000)):
            mixer = build_ambience(config, transport_kind=kind, sample_rate=rate)
            assert mixer is not None and mixer.sample_rate == rate

        # The Human speech layer is the master of every Natural Conversation
        # behaviour, ambience included.
        data(client.put(url, headers=tenant_admin, json={
            "humanSpeech": {"background_ambience": True, "enabled": False},
        }))
        config = _load_config_sync(bot_id, require_published=False)
        assert build_ambience(config, transport_kind="telephony", sample_rate=8000) is None

        bad = client.put(url, headers=tenant_admin, json={
            "humanSpeech": {"background_ambience": "on"},
        })
        assert bad.status_code == 422
        assert "background_ambience" in str(bad.json())

        cleared = data(client.put(url, headers=tenant_admin, json={"humanSpeech": {}}))
        assert cleared["humanSpeechEffective"]["background_ambience"] is False

    def test_sound_and_volume_round_trip_and_validation(self, client, tenant_admin, bot):
        from shared.audio.ambience_presets import ambience_volume_db
        from voice_runtime.ambience import build_ambience

        bot_id, _ = bot
        url = f"{API}/bots/{bot_id}/voice-settings"
        got = data(client.get(url, headers=tenant_admin))
        assert got["humanSpeechEffective"]["background_ambience_preset"] == "office"
        assert got["humanSpeechEffective"]["background_ambience_volume"] == 50

        wanted = {
            "background_ambience": True,
            "background_ambience_preset": "light_office",
            "background_ambience_volume": 80,
        }
        saved = data(client.put(url, headers=tenant_admin, json={"humanSpeech": wanted}))
        assert saved["humanSpeech"] == wanted
        assert saved["humanSpeechSources"]["background_ambience_preset"] == "bot"
        assert saved["humanSpeechSources"]["background_ambience_volume"] == "bot"
        config = _load_config_sync(bot_id, require_published=False)
        mixer = build_ambience(config, transport_kind="telephony", sample_rate=8000)
        assert mixer.bed.preset == "light_office" and mixer.volume == 80
        assert mixer.level_db == pytest.approx(ambience_volume_db(80))

        for bad in (
            {"background_ambience_preset": "jungle"},
            {"background_ambience_preset": "office_ambience_8000.wav"},
            {"background_ambience_volume": 101},
            {"background_ambience_volume": -1},
            {"background_ambience_volume": 12.5},
            {"background_ambience_volume": "70"},
            {"background_ambience_volume": True},
        ):
            response = client.put(url, headers=tenant_admin, json={"humanSpeech": bad})
            assert response.status_code == 422, bad
            assert next(iter(bad)) in str(response.json())
        # Rejected saves changed nothing.
        assert data(client.get(url, headers=tenant_admin))["humanSpeech"] == wanted

        # Volume 0 mutes: no room audio at all for the call.
        data(client.put(url, headers=tenant_admin, json={
            "humanSpeech": {**wanted, "background_ambience_volume": 0},
        }))
        config = _load_config_sync(bot_id, require_published=False)
        assert build_ambience(config, transport_kind="telephony", sample_rate=8000) is None

    def test_echo_ringing_is_offered_and_saves_per_bot(self, client, tenant_admin, bot):
        from shared.audio.ambience_presets import ambience_preset_catalog
        from voice_runtime.ambience import build_ambience

        bot_id, _ = bot
        url = f"{API}/bots/{bot_id}/voice-settings"
        got = data(client.get(url, headers=tenant_admin))
        assert got["ambiencePresets"] == ambience_preset_catalog()
        assert [p["id"] for p in got["ambiencePresets"] if p["productionEnabled"]] == [
            "office", "call_center", "light_office", "busy_office", "room_tone", "echo_ringing",
        ]
        wanted = {"background_ambience": True, "background_ambience_preset": "echo_ringing"}
        saved = data(client.put(url, headers=tenant_admin, json={"humanSpeech": wanted}))
        assert saved["humanSpeech"] == wanted
        assert saved["humanSpeechSources"]["background_ambience_preset"] == "bot"
        config = _load_config_sync(bot_id, require_published=False)
        assert build_ambience(config, transport_kind="telephony", sample_rate=8000).bed.preset == "echo_ringing"
        # Away and back again.
        data(client.put(url, headers=tenant_admin, json={"humanSpeech": {**wanted, "background_ambience_preset": "office"}}))
        again = data(client.put(url, headers=tenant_admin, json={"humanSpeech": {**wanted, "background_ambience_volume": 40}}))
        assert again["humanSpeechEffective"]["background_ambience_preset"] == "echo_ringing"

    def test_a_withdrawn_preset_is_refused_as_new_but_a_saved_one_keeps_saving(
        self, client, tenant_admin, bot, monkeypatch,
    ):
        """No preset is withdrawn today; withdrawing one is a registry flag."""
        from dataclasses import replace

        from shared.audio.ambience_presets import AMBIENCE_PRESETS

        bot_id, session = bot
        url = f"{API}/bots/{bot_id}/voice-settings"
        saved_before = {"background_ambience": True, "background_ambience_preset": "echo_ringing"}
        data(client.put(url, headers=tenant_admin, json={"humanSpeech": saved_before}))
        monkeypatch.setitem(
            AMBIENCE_PRESETS, "echo_ringing", replace(AMBIENCE_PRESETS["echo_ringing"], production_enabled=False),
        )
        got = data(client.get(url, headers=tenant_admin))
        assert {p["id"]: p["productionEnabled"] for p in got["ambiencePresets"]}["echo_ringing"] is False
        # Saved before it was withdrawn: other edits still save.
        kept = data(client.put(url, headers=tenant_admin, json={
            "humanSpeech": {**saved_before, "background_ambience_volume": 40},
        }))
        assert kept["humanSpeech"]["background_ambience_preset"] == "echo_ringing"
        # Once changed away, it cannot be picked again.
        data(client.put(url, headers=tenant_admin, json={"humanSpeech": {"background_ambience": True}}))
        refused = client.put(url, headers=tenant_admin, json={"humanSpeech": saved_before})
        assert refused.status_code == 422
        assert "not available for selection" in str(refused.json())

    def test_tenant_defaults_are_inherited_and_bot_overrides_win(
        self, client, tenant_admin, bot, tenant_ambience,
    ):
        from voice_runtime.ambience import build_ambience

        bot_id, _ = bot
        url = f"{API}/bots/{bot_id}/voice-settings"
        got = data(client.get(url, headers=tenant_admin))
        effective, sources = got["humanSpeechEffective"], got["humanSpeechSources"]
        assert (
            effective["background_ambience"],
            effective["background_ambience_preset"],
            effective["background_ambience_volume"],
        ) == (True, "call_center", 70)
        assert sources["background_ambience_preset"] == "tenant"
        assert sources["background_ambience_volume"] == "tenant"
        assert got["humanSpeechInherited"]["background_ambience_preset"] == "call_center"

        saved = data(client.put(url, headers=tenant_admin, json={
            "humanSpeech": {"background_ambience_volume": 20},
        }))
        assert saved["humanSpeechEffective"]["background_ambience_volume"] == 20
        assert saved["humanSpeechSources"]["background_ambience_volume"] == "bot"
        assert saved["humanSpeechEffective"]["background_ambience_preset"] == "call_center"
        assert saved["humanSpeechSources"]["background_ambience_preset"] == "tenant"
        config = _load_config_sync(bot_id, require_published=False)
        mixer = build_ambience(config, transport_kind="telephony", sample_rate=8000)
        assert mixer.bed.preset == "call_center" and mixer.volume == 20

    def test_echo_ringing_saves_as_a_tenant_default(self, client, tenant_admin, bot, tenant_ambience):
        from voice_runtime.ambience import build_ambience

        wanted = {"background_ambience": True, "background_ambience_preset": "echo_ringing"}
        saved = data(client.put(f"{API}/tenant/settings", headers=tenant_admin, json={"humanSpeech": wanted}))
        assert saved["humanSpeech"] == wanted
        assert saved["humanSpeechSources"]["background_ambience_preset"] == "tenant"
        bot_id, _ = bot
        got = data(client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin))
        assert got["humanSpeechEffective"]["background_ambience_preset"] == "echo_ringing"
        assert got["humanSpeechSources"]["background_ambience_preset"] == "tenant"
        config = _load_config_sync(bot_id, require_published=False)
        assert build_ambience(config, transport_kind="telephony", sample_rate=8000).bed.preset == "echo_ringing"

    def test_tenant_defaults_api_rejects_bad_ambience_values(self, client, tenant_admin):
        from shared.audio.ambience_presets import ambience_preset_catalog

        got = data(client.get(f"{API}/tenant/settings", headers=tenant_admin))
        assert got["ambiencePresets"] == ambience_preset_catalog()
        for bad in (
            {"background_ambience_volume": 150},
            {"background_ambience_preset": "forest"},
        ):
            response = client.put(f"{API}/tenant/settings", headers=tenant_admin, json={"humanSpeech": bad})
            assert response.status_code == 422, bad


class TestTenantSettingsApi:
    def test_tenant_round_trip_and_validation(self, client, tenant_admin):
        got = data(client.put(
            f"{API}/tenant/settings", headers=tenant_admin,
            json={"humanSpeech": {"backchannel_probability": 0.2}},
        ))
        assert got["humanSpeech"] == {"backchannel_probability": 0.2}
        assert got["humanSpeechEffective"]["backchannel_probability"] == 0.2
        assert got["humanSpeechSources"]["backchannel_probability"] == "tenant"
        assert got["humanSpeechSources"]["enabled"] == "platform"

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
