"""Eleven v3 Conversational is explicitly selectable everywhere, end to end.

The catalog entry only matters if an operator can actually pick the model and
have it survive to the call. These tests pin that path: catalog publication,
save + reload as the default engine / a per-language override / the fallback
engine, config validation, and that ResolvedBotConfig hands the live runtime
a STREAMING engine (so the router picks the WebSocket path, not the segmented
REST one).

They also pin the contrast that motivated the work: ``eleven_v3`` stays
REST-only and is still rejected in the realtime-only slots.
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
from shared.models import BotLanguage, User, VoiceBot, VoiceBotSetting

pytestmark = pytest.mark.integration

API = "/api/v1"
TENANT = "tn-001"
MODEL = "eleven_v3_conversational"
VOICE = "vp-el-monika"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def tenant_admin():
    db = get_sessionmaker()()
    try:
        user = db.scalar(
            select(User).where(User.email == "priya.sharma@meridianhealth.com"))
        return {"Authorization": f"Bearer {create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)}"}
    finally:
        db.close()


@pytest.fixture()
def bot():
    """A dedicated bot — seeded/real bot configurations are never touched."""
    session = get_sessionmaker()()
    row = VoiceBot(
        id=new_id("bot"), tenant_id=TENANT, name=f"v3 conv {uuid.uuid4().hex[:6]}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(row)
    session.flush()
    for locale in ("hi-IN", "ml-IN"):
        session.add(BotLanguage(bot_id=row.id, language_code=locale))
    session.commit()
    bot_id = row.id
    yield bot_id, session
    session.query(BotLanguage).filter(BotLanguage.bot_id == bot_id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == bot_id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == bot_id).delete()
    session.commit()
    session.close()


def data(response):
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def models(client, headers):
    return {m["code"]: m for m in data(
        client.get(f"{API}/providers/tts/elevenlabs/models", headers=headers))}


class TestCatalog:
    def test_model_is_published_as_streaming_and_not_default(self, client, tenant_admin):
        entry = models(client, tenant_admin)[MODEL]
        assert entry["streaming"] is True
        assert entry["isDefault"] is False
        assert entry["displayName"] == "Eleven v3 Conversational"
        # Every rate the streaming router may request, all probed on the
        # Text-to-Dialogue endpoint.
        assert entry["sampleRates"] == [8000, 16000, 22050, 24000]
        # stability is the only setting sent to ElevenLabs; native_breathing
        # is an EchoSphere-side control consumed by the adapter.
        assert sorted(entry["paramsSchema"]) == ["native_breathing", "stability"]
        assert entry["paramsSchema"]["native_breathing"]["default"] is False
        # No speed control, so the UI hides the model's own speed slider.
        assert entry["speedRange"] is None

    def test_flash_remains_the_default_and_eleven_v3_stays_rest_only(
            self, client, tenant_admin):
        catalog = models(client, tenant_admin)
        assert catalog["eleven_flash_v2_5"]["isDefault"] is True
        assert catalog["eleven_flash_v2_5"]["streaming"] is True
        assert catalog["eleven_v3"]["streaming"] is False

    def test_it_offers_the_languages_flash_cannot_speak(self, client, tenant_admin):
        def languages(model):
            return {l["code"] for l in data(client.get(
                f"{API}/providers/tts/elevenlabs/models/{model}/languages",
                headers=tenant_admin))["languages"]}

        v3_conv, flash = languages(MODEL), languages("eleven_flash_v2_5")
        for locale in ("ml-IN", "mr-IN", "te-IN", "pa-IN", "gu-IN", "ur-IN"):
            assert locale in v3_conv, locale
            assert locale not in flash, locale

    def test_the_probed_voice_advertises_the_model(self, client, tenant_admin):
        voices = data(client.get(
            f"{API}/providers/tts/elevenlabs/voices?model={MODEL}",
            headers=tenant_admin))
        assert VOICE in {v["id"] for v in voices}


class TestSeedAndMigrationAgree:
    def test_migration_row_matches_the_seed_row(self):
        """A fresh install seeds the row; an existing install migrates it.
        The two must describe the same model or the catalog drifts by
        install age."""
        import importlib.util
        import json
        from pathlib import Path

        from backend.seeds.provider_catalog_seed import (
            PROVIDER_MODELS, ELEVEN_V3_DIALOGUE_VERIFIED_VOICES,
        )

        seed = next(r for r in PROVIDER_MODELS
                    if r[:3] == ("elevenlabs", "tts", MODEL))
        _, _, _, display, _, codecs, rates, streaming, schema, is_default, status, sort = seed

        path = (Path(__file__).resolve().parents[2] / "backend" / "alembic" /
                "versions" / "d1f3b5a7c9e1_eleven_v3_conversational_catalog.py")
        spec = importlib.util.spec_from_file_location("mig_d1f3", path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)

        assert mig._MODEL_CODE == MODEL
        assert mig._DISPLAY_NAME == display
        # The seed carries the current schema; the ORIGINAL model migration
        # carries the shape it shipped with. Later revisions add keys, so the
        # seed must be a SUPERSET that still agrees on every shared key.
        assert set(mig._SCHEMA) <= set(schema)
        for key in mig._SCHEMA:
            assert json.loads(json.dumps(mig._SCHEMA[key])) == \
                   json.loads(json.dumps(schema[key])), key
        assert [8000, 16000, 22050, 24000] == rates
        assert codecs == ["pcm", "ulaw", "alaw"]
        assert streaming is True and is_default is False and status == "active"
        assert sort == 2
        # The model row's own migration pinned the single first-probed voice;
        # the follow-up revision widened it to every synthesis-verified voice,
        # which is what the seed now carries.
        assert set(mig._VERIFIED_VOICE_IDS) <= set(ELEVEN_V3_DIALOGUE_VERIFIED_VOICES)
        assert VOICE in ELEVEN_V3_DIALOGUE_VERIFIED_VOICES

    def test_neither_seed_nor_migration_invents_a_price(self):
        import importlib.util
        from pathlib import Path

        path = (Path(__file__).resolve().parents[2] / "backend" / "alembic" /
                "versions" / "d1f3b5a7c9e1_eleven_v3_conversational_catalog.py")
        body = path.read_text().split("def upgrade")[1].split("def downgrade")[0]
        code = "\n".join(line for line in body.splitlines()
                         if not line.strip().startswith("#"))
        assert "provider_pricing" not in code, (
            "the migration must not insert a pricing row")

        from backend.seeds.base_seed import PROVIDER_PRICING
        codes = {row[2] for row in PROVIDER_PRICING if row[0] == "elevenlabs"}
        assert MODEL not in codes


class TestSaveAndReload:
    def _save(self, client, headers, bot_id, payload):
        return client.put(f"{API}/bots/{bot_id}/voice-settings",
                          headers=headers, json=payload)

    def test_default_engine_saves_and_reloads(self, client, tenant_admin, bot):
        bot_id, session = bot
        data(self._save(client, tenant_admin, bot_id, {
            "ttsProvider": "elevenlabs", "ttsModel": MODEL,
            "ttsVoice": VOICE, "ttsSettings": {"stability": 1.0},
        }))
        reloaded = data(client.get(f"{API}/bots/{bot_id}/voice-settings",
                                   headers=tenant_admin))
        assert reloaded["ttsModel"] == MODEL
        assert reloaded["ttsVoice"] == VOICE
        assert reloaded["ttsSettings"]["stability"] == 1.0

    def test_per_language_override_is_accepted(self, client, tenant_admin, bot):
        """The exact slot eleven_v3 is rejected in — a Malayalam override,
        which Flash v2.5 cannot speak at all."""
        bot_id, _ = bot
        response = self._save(client, tenant_admin, bot_id, {
            "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
            "ttsVoice": VOICE,
            "languageVoiceMap": {
                "default": "hi-IN",
                "ml-IN": {"provider": "elevenlabs", "model": MODEL,
                          "voice": VOICE, "params": {"stability": 0.5}},
            },
        })
        assert response.status_code == 200, response.json()
        reloaded = data(client.get(f"{API}/bots/{bot_id}/voice-settings",
                                   headers=tenant_admin))
        assert reloaded["languageVoiceMap"]["ml-IN"]["model"] == MODEL

    def test_fallback_engine_is_accepted(self, client, tenant_admin, bot):
        bot_id, _ = bot
        response = self._save(client, tenant_admin, bot_id, {
            "ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
            "ttsVoice": "vp-sv-aayan",
            "fallbackProvider": "elevenlabs", "fallbackModel": MODEL,
            "fallbackVoice": VOICE,
        })
        assert response.status_code == 200, response.json()
        reloaded = data(client.get(f"{API}/bots/{bot_id}/voice-settings",
                                   headers=tenant_admin))
        assert reloaded["fallbackModel"] == MODEL

    def test_eleven_v3_is_still_rejected_in_realtime_only_slots(
            self, client, tenant_admin, bot):
        """The REST-only model must keep failing where it cannot work —
        otherwise this change would have papered over a real limitation."""
        bot_id, _ = bot
        response = self._save(client, tenant_admin, bot_id, {
            "ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
            "ttsVoice": VOICE,
            "languageVoiceMap": {
                "default": "hi-IN",
                "ml-IN": {"provider": "elevenlabs", "model": "eleven_v3",
                          "voice": VOICE},
            },
        })
        assert response.status_code == 422
        assert any("does not support realtime streaming" in e
                   for e in response.json()["errors"])

    def test_unsupported_settings_are_rejected_on_save(
            self, client, tenant_admin, bot):
        """similarity_boost/style/speed are not in this model's schema."""
        bot_id, _ = bot
        for stale in ({"similarity_boost": 0.9}, {"style": 0.4},
                      {"use_speaker_boost": True}):
            response = self._save(client, tenant_admin, bot_id, {
                "ttsProvider": "elevenlabs", "ttsModel": MODEL,
                "ttsVoice": VOICE, "ttsSettings": stale,
            })
            assert response.status_code == 422, stale

    def test_stability_must_be_a_documented_preset(self, client, tenant_admin, bot):
        bot_id, _ = bot
        assert self._save(client, tenant_admin, bot_id, {
            "ttsProvider": "elevenlabs", "ttsModel": MODEL,
            "ttsVoice": VOICE, "ttsSettings": {"stability": 0.42},
        }).status_code == 422
        assert self._save(client, tenant_admin, bot_id, {
            "ttsProvider": "elevenlabs", "ttsModel": MODEL,
            "ttsVoice": VOICE, "ttsSettings": {"stability": 0.0},
        }).status_code == 200


class TestValidationAndRuntimeRouting:
    def test_validate_config_reports_no_errors_or_streaming_warning(
            self, client, tenant_admin, bot):
        bot_id, _ = bot
        result = data(client.post(f"{API}/providers/validate-config",
                                  headers=tenant_admin, json={
            "botId": bot_id,
            "config": {
                "ttsProvider": "elevenlabs", "ttsModel": MODEL,
                "ttsVoice": VOICE, "ttsSettings": {"stability": 0.5},
                "languageVoiceMap": {"default": "hi-IN"},
            },
        }))
        assert result["errors"] == []
        # The "does not stream in realtime" warning eleven_v3 triggers must
        # NOT appear, and neither must the unsupported-language warning.
        joined = " ".join(result["warnings"])
        assert "does not stream in realtime" not in joined
        assert "does not support ml-IN" not in joined

    def test_resolved_config_marks_the_engine_streaming(
            self, client, tenant_admin, bot):
        """This flag is what makes build_tts_service choose the WebSocket
        router instead of the segmented REST service."""
        bot_id, _ = bot
        data(client.put(f"{API}/bots/{bot_id}/voice-settings",
                        headers=tenant_admin, json={
            "ttsProvider": "elevenlabs", "ttsModel": MODEL,
            "ttsVoice": VOICE, "ttsSettings": {"stability": 0.5},
        }))
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["provider"] == "elevenlabs"
        assert config.tts["model"] == MODEL
        assert config.tts["streaming"] is True
        # The wire voice id, not the catalog row id.
        assert config.tts["voice"] == "f1abxvIEijusskcPWE5x"
        assert config.tts["voice_name"] == "Monika"

    def test_eleven_v3_still_resolves_as_non_streaming(
            self, client, tenant_admin, bot):
        bot_id, _ = bot
        data(client.put(f"{API}/bots/{bot_id}/voice-settings",
                        headers=tenant_admin, json={
            "ttsProvider": "elevenlabs", "ttsModel": "eleven_v3",
            "ttsVoice": VOICE, "ttsSettings": {"stability": 0.5},
        }))
        config = _load_config_sync(bot_id, require_published=False)
        assert config.tts["streaming"] is False

    def test_router_builds_the_dialogue_adapter_for_this_model(self):
        """The routing decision itself: per (provider, model), with the
        provider default untouched for Flash."""
        from shared.providers.tts.elevenlabs_v3_ws import (
            ElevenLabsV3DialogueTTSProvider,
        )
        from shared.providers.tts.elevenlabs_ws import (
            ElevenLabsWebSocketTTSProvider,
        )
        from shared.providers.tts.streaming import TTSStreamSettings
        from voice_runtime.tts_router import StreamingTTSRouter

        def build(model):
            settings = TTSStreamSettings(
                provider="elevenlabs", model=model, voice="v",
                language="hi-IN", sample_rate=8000, codec="pcm", api_key="k",
            )
            return StreamingTTSRouter._default_provider_factory(None, settings)

        assert isinstance(build(MODEL), ElevenLabsV3DialogueTTSProvider)
        assert isinstance(build("eleven_flash_v2_5"),
                          ElevenLabsWebSocketTTSProvider)


class TestPricing:
    def test_usage_is_recorded_unpriced_rather_than_free(self):
        """No rate is published for this model yet. The metering layer must
        keep the quantities and flag the event, never cost it at zero."""
        from decimal import Decimal

        from shared.billing.pricing import compute_cost

        def cost(model):
            db = get_sessionmaker()()
            try:
                return compute_cost(
                    db, capability="tts", provider_code="elevenlabs",
                    model_code=model,
                    quantities={"characters": Decimal(1200)},
                )
            finally:
                db.close()

        total, priced, missing = cost(MODEL)
        assert priced == []
        assert "characters" in missing
        assert total == 0
        # Flash still prices normally — the missing row is specific to the
        # new model, not a broken lookup.
        total, priced, missing = cost("eleven_flash_v2_5")
        assert missing == [] and priced and total > 0
