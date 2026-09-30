"""Sarvam saaras:v4 + keyterms through the real API and DB catalog.

- the catalog lists saaras:v4 as selectable while saaras:v3 keeps the
  default flag, and v4's schema (not v3's) carries ``keyterms``;
- a bot that never chose a model still resolves to saaras:v3;
- PUT /voice-settings persists cleaned keyterms for v4, strips an empty
  list, rejects over-limit/comma-joined lists, and rejects keyterms for v3;
- the resolved runtime snapshot carries the terms for v4 only.
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


@pytest.fixture(scope="module", autouse=True)
def _requires_v4_catalog_row():
    db = get_sessionmaker()()
    try:
        row = db.scalar(select(ProviderModel).where(
            ProviderModel.provider_code == "sarvam", ProviderModel.capability == "stt",
            ProviderModel.code == "saaras:v4", ProviderModel.status == "active",
        ))
        if row is None:
            pytest.skip("saaras:v4 catalog row not present (run migration a9c1e3b5d7f9 "
                        "or backend/scripts/stage_saaras_v4_catalog.py)")
    finally:
        db.close()


@pytest.fixture()
def bot():
    session = get_sessionmaker()()
    row = VoiceBot(
        id=new_id("bot"), tenant_id=TENANT, name=f"Saaras v4 {uuid.uuid4().hex[:6]}",
        status="draft", version="v0.1.0", health="neutral",
    )
    session.add(row)
    session.flush()
    for code in ("hi-IN", "en-IN"):
        session.add(BotLanguage(bot_id=row.id, language_code=code))
    session.commit()
    yield row.id, session
    session.query(BotLanguage).filter(BotLanguage.bot_id == row.id).delete()
    session.query(VoiceBotSetting).filter(VoiceBotSetting.bot_id == row.id).delete()
    session.query(VoiceBot).filter(VoiceBot.id == row.id).delete()
    session.commit()
    session.close()


def _put(client, headers, bot_id, **payload):
    return client.put(f"{API}/bots/{bot_id}/voice-settings", json=payload, headers=headers)


class TestCatalog:
    def test_v4_listed_v3_default_and_only_v4_has_keyterms(self, client, tenant_admin):
        response = client.get(f"{API}/providers/stt/sarvam/models", headers=tenant_admin)
        assert response.status_code == 200
        models = {m["code"]: m for m in response.json()["data"]}
        assert "saaras:v3" in models and "saaras:v4" in models
        assert models["saaras:v3"]["isDefault"] is True
        assert models["saaras:v4"]["isDefault"] is False
        assert "keyterms" in models["saaras:v4"]["paramsSchema"]
        assert "keyterms" not in models["saaras:v3"]["paramsSchema"]
        assert models["saaras:v4"]["paramsSchema"]["keyterms"]["max_items"] == 50

    def test_v4_supports_auto_detect_like_v3(self, client, tenant_admin):
        response = client.get(
            f"{API}/providers/stt/sarvam/models/saaras:v4/languages", headers=tenant_admin
        )
        assert response.status_code == 200
        assert response.json()["data"]["supportsAutoDetect"] is True


class TestVoiceSettings:
    def test_bot_without_choice_resolves_to_saaras_v3(self, bot):
        bot_id, session = bot
        config = _load_config_sync(bot_id, require_published=False)
        assert config.stt["provider"] == "sarvam"
        assert config.stt["model"] == "saaras:v3"
        assert "keyterms" not in (config.stt.get("settings") or {})

    def test_v4_with_keyterms_persists_cleaned_and_reaches_runtime(self, client, tenant_admin, bot):
        bot_id, session = bot
        response = _put(
            client, tenant_admin, bot_id,
            sttProvider="sarvam", sttModel="saaras:v4",
            sttSettings={"mode": "transcribe", "keyterms": [" Zepto", "New Delhi", "Zepto", " "]},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["sttModel"] == "saaras:v4"
        assert data["sttSettings"]["keyterms"] == ["Zepto", "New Delhi"]

        reloaded = client.get(f"{API}/bots/{bot_id}/voice-settings", headers=tenant_admin)
        assert reloaded.json()["data"]["sttSettings"]["keyterms"] == ["Zepto", "New Delhi"]

        session.rollback()  # REPEATABLE READ: see the API's commit
        config = _load_config_sync(bot_id, require_published=False)
        assert config.stt["model"] == "saaras:v4"
        assert config.stt["settings"]["keyterms"] == ["Zepto", "New Delhi"]

    def test_empty_keyterms_are_dropped_not_stored(self, client, tenant_admin, bot):
        bot_id, _ = bot
        response = _put(
            client, tenant_admin, bot_id,
            sttProvider="sarvam", sttModel="saaras:v4",
            sttSettings={"mode": "transcribe", "keyterms": []},
        )
        assert response.status_code == 200, response.text
        assert "keyterms" not in response.json()["data"]["sttSettings"]

    def test_v4_rejects_over_limit_and_comma_joined(self, client, tenant_admin, bot):
        bot_id, _ = bot
        too_many = _put(
            client, tenant_admin, bot_id, sttProvider="sarvam", sttModel="saaras:v4",
            sttSettings={"keyterms": [f"term{i}" for i in range(51)]},
        )
        assert too_many.status_code == 422
        assert "at most 50" in too_many.text
        comma = _put(
            client, tenant_admin, bot_id, sttProvider="sarvam", sttModel="saaras:v4",
            sttSettings={"keyterms": ["Zepto, New Delhi"]},
        )
        assert comma.status_code == 422
        assert "one term" in comma.text
        too_long = _put(
            client, tenant_admin, bot_id, sttProvider="sarvam", sttModel="saaras:v4",
            sttSettings={"keyterms": ["x" * 65]},
        )
        assert too_long.status_code == 422

    def test_v3_rejects_keyterms(self, client, tenant_admin, bot):
        bot_id, _ = bot
        response = _put(
            client, tenant_admin, bot_id, sttProvider="sarvam", sttModel="saaras:v3",
            sttSettings={"mode": "transcribe", "keyterms": ["Zepto"]},
        )
        assert response.status_code == 422
        assert "unknown parameter 'keyterms'" in response.text

    def test_v3_and_v4_paths_do_not_interfere(self, client, tenant_admin, bot):
        bot_id, session = bot
        # v4 with terms …
        ok = _put(
            client, tenant_admin, bot_id, sttProvider="sarvam", sttModel="saaras:v4",
            sttSettings={"mode": "codemix", "keyterms": ["Zepto"]},
        )
        assert ok.status_code == 200, ok.text
        # … switching back to v3 with the UI's reconciled settings (no keyterms)
        back = _put(
            client, tenant_admin, bot_id, sttProvider="sarvam", sttModel="saaras:v3",
            sttSettings={"mode": "codemix"},
        )
        assert back.status_code == 200, back.text
        assert back.json()["data"]["sttModel"] == "saaras:v3"
        assert "keyterms" not in back.json()["data"]["sttSettings"]
        session.rollback()
        config = _load_config_sync(bot_id, require_published=False)
        assert config.stt["model"] == "saaras:v3"
        assert config.stt["settings"]["mode"] == "codemix"
        assert "keyterms" not in config.stt["settings"]
