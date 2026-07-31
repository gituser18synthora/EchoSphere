"""Delivery tuning end-to-end: settings API, config resolution, preview.

Covers the canonical Delivery controls (speed, pauseMs, empathy, energy):
- persistence + serialization through /bots/{id}/voice-settings;
- sanitization of legacy per-provider pace/speed duplicates on save AND read;
- ResolvedBotConfig carries the values (clamped) for the runtime;
- /providers/tts-preview applies canonical speed / pause / native energy via
  the same shared mapping as live calls.
"""

import base64
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.audio.pcm import silence_pcm
from shared.bot_config import _load_config_sync
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import BotLanguage, User, VoiceBot, VoiceBotSetting

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
        id=new_id("bot"), tenant_id=TENANT, name=f"Delivery {uuid.uuid4().hex[:6]}",
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


def data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


class TestVoiceSettingsPersistence:
    def test_delivery_fields_round_trip(self, client, tenant_admin, bot):
        bot_id, _ = bot
        saved = data(client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
            "speed": 1.25, "pauseMs": 500, "empathy": 80, "energy": 20,
        }))
        assert (saved["speed"], saved["pauseMs"], saved["empathy"], saved["energy"]) == (
            1.25, 500, 80, 20,
        )
        loaded = data(client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin))
        assert (loaded["speed"], loaded["pauseMs"], loaded["empathy"], loaded["energy"]) == (
            1.25, 500, 80, 20,
        )

    def test_out_of_range_delivery_values_rejected(self, client, tenant_admin, bot):
        bot_id, _ = bot
        for payload in ({"speed": 3.0}, {"pauseMs": 9000},
                        {"empathy": 101}, {"energy": -1}):
            response = client.put(
                f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json=payload,
            )
            assert response.status_code == 422, payload

    def test_legacy_speed_params_stripped_on_save(self, client, tenant_admin, bot):
        bot_id, _ = bot
        saved = data(client.put(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin, json={
            "speed": 1.1,
            "ttsProvider": "sarvam", "ttsModel": "bulbul:v3", "ttsVoice": "shubh",
            "ttsSettings": {"pace": 0.7, "min_buffer_size": 60},
            "languageVoiceMap": {
                "hi-IN": {"provider": "sarvam", "model": "bulbul:v3",
                          "voice": "ritu", "params": {"pace": 0.8, "temperature": 0.5}},
            },
        }))
        # Duplicates are gone; unrelated provider settings survive.
        assert saved["ttsSettings"] == {"min_buffer_size": 60}
        assert saved["languageVoiceMap"]["hi-IN"]["params"] == {"temperature": 0.5}

    def test_legacy_speed_params_hidden_on_read_for_old_rows(
        self, client, tenant_admin, bot
    ):
        bot_id, session = bot
        # Simulate a pre-existing row saved before the sanitization existed.
        session.add(VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot_id, tenant_id=TENANT,
            tts_provider="sarvam", tts_model="bulbul:v3",
            tts_settings={"pace": 0.6, "speed": 0.6, "temperature": 0.4},
        ))
        session.commit()
        loaded = data(client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin))
        assert loaded["ttsSettings"] == {"temperature": 0.4}


class TestResolvedConfigDelivery:
    def test_delivery_settings_reach_resolved_config(self, bot):
        bot_id, session = bot
        session.add(VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot_id, tenant_id=TENANT,
            speed=1.3, pause_ms=600, empathy=90, energy=15,
            stt_provider="mock", tts_provider="mock", llm_provider="mock",
        ))
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.speed == 1.3
        assert config.pause_ms == 600
        assert config.empathy == 90
        assert config.energy == 15

    def test_malformed_stored_values_clamp_at_resolution(self, bot):
        bot_id, session = bot
        session.add(VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot_id, tenant_id=TENANT,
            speed=9.0, pause_ms=99999, empathy=500, energy=-40,
            stt_provider="mock", tts_provider="mock", llm_provider="mock",
        ))
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.speed == 2.0
        assert config.pause_ms == 5000
        assert config.empathy == 100
        assert config.energy == 0

    def test_bot_without_settings_row_gets_defaults(self, bot):
        bot_id, session = bot
        session.add(VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot_id, tenant_id=TENANT,
            stt_provider="mock", tts_provider="mock", llm_provider="mock",
        ))
        session.commit()
        config = _load_config_sync(bot_id, require_published=False)
        assert (config.speed, config.pause_ms, config.empathy, config.energy) == (
            1.0, 350, 50, 50,
        )


class TestPreviewDelivery:
    def test_multi_sentence_preview_inserts_configured_silence(
        self, client, tenant_admin
    ):
        text = "Hello preview world. This is the second sentence."
        base = {
            "provider": "mock", "model": "mock", "voice": "test-voice",
            "language": "en-US", "text": text,
        }
        gap = silence_pcm(16000, 350)

        with_pause = data(client.post(
            f"{API}/providers/tts-preview", headers=tenant_admin,
            json={**base, "pauseMs": 350},
        ))
        pcm = base64.b64decode(with_pause["audioBase64"])[44:]  # skip WAV header
        assert gap in pcm, "expected a 350ms silence gap between sentences"
        # The gap sits strictly between audio, never at the edges.
        assert not pcm.startswith(b"\x00" * 64)
        assert not pcm.endswith(b"\x00" * 64)

        without_pause = data(client.post(
            f"{API}/providers/tts-preview", headers=tenant_admin, json=base,
        ))
        assert gap not in base64.b64decode(without_pause["audioBase64"])[44:]

    def test_single_sentence_preview_has_no_gap(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/tts-preview", headers=tenant_admin, json={
            "provider": "mock", "model": "mock", "voice": "test-voice",
            "language": "en-US", "text": "Just one sentence here.",
            "pauseMs": 350,
        }))
        pcm = base64.b64decode(result["audioBase64"])[44:]
        assert silence_pcm(16000, 350) not in pcm

    def test_preview_applies_canonical_speed_and_native_energy(
        self, client, tenant_admin, monkeypatch
    ):
        """eleven_v3 REST preview: canonical speed is passed through the
        shared mapping (v3 supports no speed — never sent) and Energy maps to
        the documented `style` control without touching operator settings."""
        from shared.providers.base import TTSResult
        import shared.providers.tts.elevenlabs as elevenlabs_rest

        monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-preview-test")
        seen: dict = {}

        async def fake_synthesize(self, text, *, voice=None, language=None, speed=1.0):
            seen["params"] = dict(self._params)
            seen["speed"] = speed
            return TTSResult(audio=b"\x00\x01" * 320, sample_rate=16000, duration_ms=10.0)

        monkeypatch.setattr(elevenlabs_rest.ElevenLabsTTS, "synthesize", fake_synthesize)

        data(client.post(f"{API}/providers/tts-preview", headers=tenant_admin, json={
            "provider": "elevenlabs", "model": "eleven_v3", "voice": "vp-el-monika",
            "language": "hi-IN", "text": "Namaste ji.",
            "params": {"stability": 0.5},
            "speed": 1.2, "energy": 90,
        }))
        # Energy 81–100 → style 0.4 (fill-only); stability untouched; the
        # canonical speed reached the adapter; v3 never gets a speed param.
        assert seen["params"]["style"] == 0.4
        assert seen["params"]["stability"] == 0.5
        assert "speed" not in seen["params"]
        assert seen["speed"] == 1.2

    def test_preview_rejects_out_of_range_delivery_values(self, client, tenant_admin):
        for extra in ({"speed": 5.0}, {"pauseMs": 9001}, {"energy": 200}):
            response = client.post(
                f"{API}/providers/tts-preview", headers=tenant_admin,
                json={"provider": "mock", "model": "mock", "voice": "v",
                      "language": "en-US", "text": "Hello there.", **extra},
            )
            assert response.status_code == 422, extra
