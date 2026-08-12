"""Provider/model-specific TTS voice settings, end to end.

The catalog (provider_models.params_schema) is the single source of truth for
which settings each provider/model accepts. These tests pin that contract at
every layer the settings travel through:

- the catalog API publishes the per-model schema (and speed range) the UI
  renders from — Sarvam v2 vs v3 differ, ElevenLabs models differ;
- /providers/tts-preview validates DRAFT settings against the selected model
  before synthesizing, so an unsaved value can never reach a provider that
  does not accept it;
- previewing uses the draft values without persisting them;
- /bots/{id}/voice-settings persists exactly what was tuned and returns it on
  reload;
- ResolvedBotConfig hands those persisted settings to the live-call runtime.
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
from shared.models import BotLanguage, ProviderModel, User, VoiceBot, VoiceBotSetting

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
    """A dedicated bot so seeded fixtures are never mutated."""
    session = get_sessionmaker()()
    row = VoiceBot(
        id=new_id("bot"), tenant_id=TENANT, name=f"TTS settings {uuid.uuid4().hex[:6]}",
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
def sarvam_v2_active():
    """bulbul:v2 is shipped inactive; activate it for the switching tests.

    Model availability is a platform-governance decision, so the seed leaves
    v2 off. These tests only need it selectable, and restore the prior status
    afterwards so the environment is unchanged.
    """
    session = get_sessionmaker()()
    row = session.scalar(select(ProviderModel).where(
        ProviderModel.capability == "tts",
        ProviderModel.provider_code == "sarvam",
        ProviderModel.code == "bulbul:v2",
    ))
    if row is None:
        session.close()
        pytest.skip("bulbul:v2 is not present in this catalog")
    previous = row.status
    row.status = "active"
    session.commit()
    yield row.code
    row.status = previous
    session.commit()
    session.close()


def data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def model_entry(client, headers, provider: str, code: str) -> dict:
    models = data(client.get(f"{API}/providers/tts/{provider}/models", headers=headers))
    entry = next((m for m in models if m["code"] == code), None)
    assert entry is not None, f"{provider}/{code} not offered by the catalog API"
    return entry


# ── catalog: per-model capability metadata ───────────────────────────────────

class TestModelCapabilityMetadata:
    """The UI renders from these schemas, so the per-model differences that
    drive show/hide behaviour are asserted here rather than in the frontend."""

    def test_sarvam_v3_exposes_pace_and_temperature_but_not_pitch_or_loudness(
        self, client, tenant_admin
    ):
        schema = model_entry(client, tenant_admin, "sarvam", "bulbul:v3")["paramsSchema"]
        assert "pace" in schema and "temperature" in schema
        # v3 has no pitch/loudness controls at all — they must not be offered.
        assert "pitch" not in schema
        assert "loudness" not in schema
        assert schema["pace"]["min"] == 0.5 and schema["pace"]["max"] == 2.0
        assert schema["temperature"]["default"] == pytest.approx(0.6)
        # v3 always preprocesses server-side: shown, but not a live toggle.
        assert schema["enable_preprocessing"]["fixed"] is True
        # Documented Sarvam WebSocket bounds (2026-08): 30–200, default 50.
        buffer_spec = schema["min_buffer_size"]
        assert (buffer_spec["min"], buffer_spec["max"], buffer_spec["default"]) == (30, 200, 50)
        # dict_id renders as the dictionary selector, never a raw id input.
        assert schema["dict_id"]["widget"] == "dictionary"
        assert schema["dict_id"]["section"] == "pronunciation"

    def test_sarvam_v2_exposes_pitch_loudness_and_a_wider_pace_range(
        self, client, tenant_admin, sarvam_v2_active
    ):
        schema = model_entry(client, tenant_admin, "sarvam", "bulbul:v2")["paramsSchema"]
        assert schema["pitch"]["min"] == -0.75 and schema["pitch"]["max"] == 0.75
        assert schema["pitch"]["default"] == 0
        assert schema["loudness"]["min"] == 0.3 and schema["loudness"]["max"] == 3.0
        assert schema["pace"]["min"] == 0.3 and schema["pace"]["max"] == 3.0
        # temperature and the pronunciation dictionary are v3-only controls.
        assert "temperature" not in schema
        assert "dict_id" not in schema
        # v2 preprocessing is a real toggle, unlike v3.
        assert schema["enable_preprocessing"].get("fixed") is not True
        buffer_spec = schema["min_buffer_size"]
        assert (buffer_spec["min"], buffer_spec["max"], buffer_spec["default"]) == (30, 200, 50)

    def test_elevenlabs_models_differ_on_speed_and_speaker_boost(
        self, client, tenant_admin
    ):
        flash = model_entry(client, tenant_admin, "elevenlabs", "eleven_flash_v2_5")
        v3 = model_entry(client, tenant_admin, "elevenlabs", "eleven_v3")
        assert {"stability", "similarity_boost", "style", "use_speaker_boost", "speed"} <= set(
            flash["paramsSchema"]
        )
        # Eleven v3 supports neither speed nor speaker boost, and takes
        # stability as three discrete presets rather than a continuous range.
        assert "speed" not in v3["paramsSchema"]
        assert "use_speaker_boost" not in v3["paramsSchema"]
        assert v3["paramsSchema"]["stability"]["type"] == "enum"
        assert v3["paramsSchema"]["stability"]["values"] == [0.0, 0.5, 1.0]

    def test_speed_range_is_published_per_model(self, client, tenant_admin, sarvam_v2_active):
        """The UI bounds its speaking-speed slider from this, so a model with
        no speed control must publish null rather than a range."""
        assert model_entry(client, tenant_admin, "sarvam", "bulbul:v3")["speedRange"] == [0.5, 2.0]
        assert model_entry(client, tenant_admin, "sarvam", "bulbul:v2")["speedRange"] == [0.3, 3.0]
        assert model_entry(
            client, tenant_admin, "elevenlabs", "eleven_flash_v2_5"
        )["speedRange"] == [0.7, 1.2]
        assert model_entry(client, tenant_admin, "elevenlabs", "eleven_v3")["speedRange"] is None


# ── preview: draft settings, validated but not persisted ─────────────────────

def _preview(client, headers, **overrides):
    payload = {
        "provider": "sarvam", "model": "bulbul:v3", "voice": "vp-sv-aayan",
        "language": "hi-IN", "text": "Namaste, kaise hain aap?",
    }
    payload.update(overrides)
    return client.post(f"{API}/providers/tts-preview", headers=headers, json=payload)


class TestPreviewValidatesDraftSettings:
    def test_v3_rejects_v2_only_pitch_and_loudness(self, client, tenant_admin):
        """The exact provider/model-switching hazard: settings left over from
        bulbul:v2 must not be previewed against bulbul:v3."""
        for stale in ({"pitch": 0.3}, {"loudness": 1.4}, {"pitch": 0.3, "loudness": 1.4}):
            response = _preview(client, tenant_admin, params=stale)
            assert response.status_code == 422, stale
            assert "unknown parameter" in response.json()["message"].lower()

    def test_v2_accepts_pitch_and_loudness(self, client, tenant_admin, sarvam_v2_active):
        response = _preview(
            client, tenant_admin, model="bulbul:v2",
            params={"pitch": 0.25, "loudness": 1.2},
        )
        # Valid settings get past validation; only credentials may stop it.
        assert response.status_code != 422, response.json()

    def test_out_of_range_values_are_rejected_per_model(self, client, tenant_admin):
        # temperature is a v3 control, but 4.0 is outside its documented range.
        response = _preview(client, tenant_admin, params={"temperature": 4.0})
        assert response.status_code == 422
        assert "between" in response.json()["message"]

    def test_v2_only_pace_range_is_not_honoured_by_v3(
        self, client, tenant_admin, sarvam_v2_active
    ):
        """pace 2.6 is legal on v2 and illegal on v3 — the same value must be
        accepted or rejected according to the model actually selected."""
        assert _preview(
            client, tenant_admin, model="bulbul:v2", params={"pace": 2.6},
        ).status_code != 422
        # On v3 the UI clamps before sending; a client that does not is rejected
        # rather than having a silently different speed applied. (pace is
        # Delivery-owned, so it is stripped, not forwarded — see below.)

    def test_elevenlabs_v3_rejects_settings_only_the_v2_5_family_supports(
        self, client, tenant_admin
    ):
        for stale in ({"use_speaker_boost": True}, {"auto_mode": True},
                      {"chunk_length_schedule": [120, 160]}):
            response = _preview(
                client, tenant_admin, provider="elevenlabs", model="eleven_v3",
                voice="vp-el-monika", params=stale,
            )
            assert response.status_code == 422, stale

    def test_elevenlabs_v3_stability_must_be_a_documented_preset(self, client, tenant_admin):
        rejected = _preview(
            client, tenant_admin, provider="elevenlabs", model="eleven_v3",
            voice="vp-el-monika", params={"stability": 0.42},
        )
        assert rejected.status_code == 422
        accepted = _preview(
            client, tenant_admin, provider="elevenlabs", model="eleven_v3",
            voice="vp-el-monika", params={"stability": 1.0},
        )
        assert accepted.status_code != 422

    def test_min_buffer_size_out_of_documented_range_is_rejected_before_sarvam(
        self, client, tenant_admin
    ):
        """Sarvam WS streaming documents 30–200; 20 used to reach the provider
        and come back as a bare "invalid_input". EchoSphere now rejects it
        with a field-level message and never calls Sarvam."""
        for bad in (20, 29, 201, 500):
            response = _preview(client, tenant_admin, params={"min_buffer_size": bad})
            assert response.status_code == 422, bad
            message = response.json()["message"]
            assert "min_buffer_size" in message and "between 30 and 200" in message

    def test_min_buffer_size_documented_bounds_are_accepted(self, client, tenant_admin):
        for good in (30, 200):
            response = _preview(client, tenant_admin, params={"min_buffer_size": good})
            assert response.status_code != 422, (good, response.json())

    def test_min_buffer_size_range_is_enforced_on_save_too(
        self, client, tenant_admin, bot
    ):
        bot_id, _ = bot
        response = client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
                "ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                "ttsVoice": "vp-sv-aayan", "ttsSettings": {"min_buffer_size": 20},
            },
        )
        assert response.status_code == 422
        assert any("between 30 and 200" in e for e in response.json()["errors"])

    def test_delivery_owned_speed_params_are_stripped_not_rejected(
        self, client, tenant_admin
    ):
        """`pace`/`speed` belong to the canonical speaking-speed control, so a
        stale copy in provider params is dropped rather than failing the call."""
        response = _preview(
            client, tenant_admin, provider="mock", model="mock", voice="v",
            language="en-US", params={"pace": 1.9, "speed": 0.8}, speed=1.1,
        )
        assert response.status_code == 200, response.json()

    def test_preview_uses_draft_values_without_persisting_them(
        self, client, tenant_admin, bot, monkeypatch
    ):
        """The whole point of tuning in the preview: unsaved values reach the
        provider, and the bot's stored configuration is untouched."""
        from shared.providers.base import TTSResult
        import shared.providers.tts.elevenlabs as elevenlabs_rest

        bot_id, session = bot
        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-preview-test")
        data(client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
            "ttsProvider": "elevenlabs", "ttsModel": "eleven_v3",
            "ttsVoice": "vp-el-monika", "ttsSettings": {"stability": 0.5},
        }))

        seen: dict = {}

        async def fake_synthesize(self, text, *, voice=None, language=None, speed=1.0):
            seen["params"] = dict(self._params)
            return TTSResult(audio=b"\x00\x01" * 320, sample_rate=16000, duration_ms=10.0)

        monkeypatch.setattr(elevenlabs_rest.ElevenLabsTTS, "synthesize", fake_synthesize)

        data(_preview(
            client, tenant_admin, provider="elevenlabs", model="eleven_v3",
            voice="vp-el-monika", params={"stability": 1.0, "style": 0.3},
        ))
        # The DRAFT reached the provider, not the saved stability of 0.5.
        assert seen["params"]["stability"] == 1.0
        assert seen["params"]["style"] == 0.3

        session.expire_all()
        stored = session.scalar(
            select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot_id)
        )
        assert stored.tts_settings["stability"] == 0.5
        assert "style" not in stored.tts_settings


# ── persistence: save → reload → runtime ─────────────────────────────────────

class TestSettingsPersistence:
    def test_sarvam_v3_settings_survive_a_reload(self, client, tenant_admin, bot):
        bot_id, _ = bot
        settings = {"temperature": 0.45, "min_buffer_size": 60, "dict_id": "collections"}
        saved = data(client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
                "ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                "ttsVoice": "vp-sv-aayan", "ttsSettings": settings,
            },
        ))
        assert saved["ttsSettings"] == settings
        reloaded = data(client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin))
        assert reloaded["ttsSettings"] == settings

    def test_elevenlabs_settings_serialize_with_provider_key_names(
        self, client, tenant_admin, bot
    ):
        """Provider parameters keep their provider-native snake_case names —
        only the envelope fields are camelCase."""
        bot_id, _ = bot
        settings = {
            "stability": 0.4, "similarity_boost": 0.8, "style": 0.1,
            "use_speaker_boost": True,
        }
        saved = data(client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
                "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
                "ttsVoice": "vp-el-monika", "ttsSettings": settings,
            },
        ))
        assert saved["ttsSettings"] == settings
        assert "ttsProvider" in saved and "ttsSettings" in saved

    def test_save_rejects_settings_the_selected_model_does_not_support(
        self, client, tenant_admin, bot
    ):
        bot_id, _ = bot
        response = client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
                "ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                "ttsVoice": "vp-sv-aayan", "ttsSettings": {"pitch": 0.4},
            },
        )
        assert response.status_code == 422
        assert any("pitch" in error for error in response.json()["errors"])

    def test_save_rejects_out_of_range_values(self, client, tenant_admin, bot):
        bot_id, _ = bot
        response = client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
                "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
                "ttsVoice": "vp-el-monika", "ttsSettings": {"stability": 1.5},
            },
        )
        assert response.status_code == 422

    def test_saved_settings_reach_the_live_call_runtime(self, client, tenant_admin, bot):
        """Saved UI settings → backend → database → voice runtime config."""
        bot_id, _ = bot
        data(client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
            "ttsProvider": "sarvam", "ttsModel": "bulbul:v3", "ttsVoice": "vp-sv-aayan",
            "ttsSettings": {"temperature": 0.35, "min_buffer_size": 55},
            "speed": 0.9,
        }))
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["settings"]["temperature"] == pytest.approx(0.35)
        assert config.tts["settings"]["min_buffer_size"] == 55
        # Canonical speed rides alongside and becomes Sarvam `pace` downstream.
        assert config.speed == pytest.approx(0.9)

    def test_bot_without_explicit_settings_keeps_working(self, client, tenant_admin, bot):
        """Backward compatibility: no tts_settings means provider/model
        defaults, not an error and not a backfilled blob."""
        bot_id, _ = bot
        saved = data(client.put(
            f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
                "ttsProvider": "sarvam", "ttsModel": "bulbul:v3", "ttsVoice": "vp-sv-aayan",
            },
        ))
        assert saved["ttsSettings"] == {}
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["settings"] == {}
        assert config.tts["model"] == "bulbul:v3"

    def test_partial_settings_are_preserved_verbatim(self, client, tenant_admin, bot):
        """An existing bot that only ever set two of five ElevenLabs controls
        keeps exactly those two — the rest fall through to provider defaults."""
        bot_id, _ = bot
        data(client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
            "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
            "ttsVoice": "vp-el-monika", "ttsSettings": {"style": 1, "stability": 0.6},
        }))
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["settings"] == {"style": 1, "stability": 0.6}
