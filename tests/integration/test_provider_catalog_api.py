"""Provider catalog / validation / test / preview APIs against the real DB."""

import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.models import AuditLog, User

pytestmark = pytest.mark.integration

API = "/api/v1"
_MARKER_KEY = f"sk-test-secret-{uuid.uuid4().hex}"


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


@pytest.fixture(scope="module")
def tenant_user():
    return bearer("sam.ellery@meridianhealth.com")


def data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


class TestCatalogReads:
    def test_catalog_lists_active_providers_with_credentials_flag(self, client, tenant_admin):
        catalog = data(client.get(f"{API}/providers/catalog", headers=tenant_admin))
        tts_codes = {p["code"] for p in catalog["tts"]}
        assert {"sarvam", "elevenlabs"} <= tts_codes
        sarvam = next(p for p in catalog["tts"] if p["code"] == "sarvam")
        assert sarvam["name"] == "Sarvam AI"
        assert "hasCredentials" in sarvam and isinstance(sarvam["hasCredentials"], bool)

    def test_models_exclude_inactive(self, client, tenant_admin):
        models = data(client.get(f"{API}/providers/tts/elevenlabs/models", headers=tenant_admin))
        codes = {m["code"] for m in models}
        assert "eleven_flash_v2_5" in codes
        assert "eleven_turbo_v2_5" not in codes  # seeded inactive
        flash = next(m for m in models if m["code"] == "eleven_flash_v2_5")
        assert flash["paramsSchema"]["stability"]["max"] == 1.0
        assert flash["isDefault"] is True

    def test_bulbul_languages_intersected_with_platform(self, client, tenant_admin):
        payload = data(client.get(
            f"{API}/providers/tts/sarvam/models/bulbul:v3/languages", headers=tenant_admin
        ))
        codes = {lang["code"] for lang in payload["languages"]}
        assert len(codes) == 11
        assert "or-IN" in codes          # platform form of Sarvam's od-IN
        assert "fr-FR" not in codes      # not a bulbul language
        assert payload["supportsAutoDetect"] is False

    def test_saarika_supports_auto_detect(self, client, tenant_admin):
        payload = data(client.get(
            f"{API}/providers/stt/sarvam/models/saarika:v2.5/languages", headers=tenant_admin
        ))
        assert payload["supportsAutoDetect"] is True

    def test_voice_filtering(self, client, tenant_admin):
        voices = data(client.get(
            f"{API}/providers/tts/sarvam/voices?language=hi-IN&gender=female",
            headers=tenant_admin,
        ))
        assert voices and all(v["gender"] == "female" for v in voices)
        # Wire codes are lowercase; display names are formatted.
        ritu = next(v for v in voices if v["name"] == "Ritu")
        assert ritu["providerVoiceId"] == "ritu"

        eleven = data(client.get(
            f"{API}/providers/tts/elevenlabs/voices?model=eleven_flash_v2_5",
            headers=tenant_admin,
        ))
        assert {v["name"] for v in eleven} == {
            "Monika", "Raju", "Niraj", "Leo", "Viraj", "Shardul", "Anvi", "Shivank"
        }

    def test_unknown_provider_404(self, client, tenant_admin):
        assert client.get(
            f"{API}/providers/tts/nosuch/models", headers=tenant_admin
        ).status_code == 404

    def test_requires_auth(self, client):
        assert client.get(f"{API}/providers/catalog").status_code == 401


class TestValidation:
    def test_rejects_cross_provider_model_and_voice(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {
                "ttsProvider": "elevenlabs", "ttsModel": "bulbul:v3",
                "ttsVoice": "vp-sv-shubh",
            },
        }))
        assert not result["valid"]
        joined = " ".join(result["errors"])
        assert "does not belong to provider 'elevenlabs'" in joined
        assert "does not belong to provider 'elevenlabs'" in joined

    def test_unknown_param_and_range(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {"ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                       "ttsSettings": {"temperature": 3.0, "made_up": 1}},
        }))
        joined = " ".join(result["errors"])
        assert "'temperature' must be between" in joined
        assert "unknown parameter 'made_up'" in joined


class TestSecretsNeverLeak:
    def test_responses_contain_no_key_material(self, client, tenant_admin, monkeypatch):
        monkeypatch.setenv("SARVAM_API_KEY", _MARKER_KEY)
        monkeypatch.setenv("ELEVENLABS_API_KEY", _MARKER_KEY)
        responses = [
            client.get(f"{API}/providers/catalog", headers=tenant_admin),
            client.get(f"{API}/providers/tts/sarvam/models", headers=tenant_admin),
            client.get(f"{API}/providers/tts/sarvam/voices", headers=tenant_admin),
            client.get(f"{API}/providers/voice-catalog", headers=tenant_admin),
            client.get(f"{API}/bots/bot-101/voice-settings", headers=tenant_admin),
        ]
        for response in responses:
            assert _MARKER_KEY not in response.text
        # With the key set, the credentials flag flips true — still no material.
        catalog = data(client.get(f"{API}/providers/catalog?capability=tts",
                                  headers=tenant_admin))
        sarvam = next(p for p in catalog["tts"] if p["code"] == "sarvam")
        assert sarvam["hasCredentials"] is True


class TestPermissionsAndAudit:
    def test_tenant_user_cannot_test_or_preview(self, client, tenant_user):
        assert client.post(f"{API}/providers/test", headers=tenant_user, json={
            "capability": "tts", "provider": "mock",
        }).status_code == 403
        assert client.post(f"{API}/providers/tts-preview", headers=tenant_user, json={
            "provider": "mock", "model": "mock", "voice": "x", "language": "en-US",
            "text": "hello",
        }).status_code == 403

    def test_provider_test_mock_and_audit(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/test", headers=tenant_admin, json={
            "capability": "tts", "provider": "mock",
        }))
        assert result["ok"] is True
        db = get_sessionmaker()()
        try:
            row = db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "Tested provider connection",
                    AuditLog.entity_id == "tts:mock",
                ).order_by(AuditLog.created_at.desc())
            ).first()
            assert row is not None
        finally:
            db.close()

    def test_missing_credentials_is_sanitized_failure(self, client, tenant_admin, monkeypatch):
        monkeypatch.delenv("SARVAM_API_KEY", raising=False)
        result = data(client.post(f"{API}/providers/test", headers=tenant_admin, json={
            "capability": "stt", "provider": "sarvam", "model": "saarika:v2.5",
        }))
        assert result["ok"] is False and result["error"] == "credentials_missing"

    def test_preview_with_mock_returns_wav_and_timings(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/tts-preview", headers=tenant_admin, json={
            "provider": "mock", "model": "mock", "voice": "test-voice",
            "language": "en-US", "text": "Hello preview world.",
        }))
        wav = base64.b64decode(result["audioBase64"])
        assert wav[:4] == b"RIFF" and result["mimeType"] == "audio/wav"
        assert result["totalMs"] >= 0 and "ttfaMs" in result
        db = get_sessionmaker()()
        try:
            row = db.scalars(
                select(AuditLog).where(AuditLog.action == "Generated voice preview")
                .order_by(AuditLog.created_at.desc())
            ).first()
            assert row is not None
            # Preview text itself is never logged — only its length.
            assert "Hello preview world" not in json.dumps(row.new_value or {})
        finally:
            db.close()

    def test_preview_rejects_overlong_text(self, client, tenant_admin):
        response = client.post(f"{API}/providers/tts-preview", headers=tenant_admin, json={
            "provider": "mock", "model": "mock", "voice": "v", "language": "en-US",
            "text": "x" * 501,
        })
        assert response.status_code == 422


class TestSarvamSpeakerConsistency:
    """bulbul:v3 speaker list consistency + validation (v2→v3 mismatch fix)."""

    def test_api_speaker_list_matches_seed_catalog(self, client, tenant_admin):
        # The frontend dropdown is fed by this endpoint, so API == seed means
        # frontend == backend == seed — one source of truth, no duplicates.
        from backend.seeds.provider_catalog_seed import SARVAM_SPEAKERS

        voices = data(client.get(
            f"{API}/providers/tts/sarvam/voices?model=bulbul:v3", headers=tenant_admin,
        ))
        assert {v["providerVoiceId"] for v in voices} == set(SARVAM_SPEAKERS)
        # Legacy bulbul:v2-only names must be gone.
        assert not ({"anushka", "abhilash", "manisha", "vidya", "arya", "karun",
                     "hitesh"} & {v["providerVoiceId"] for v in voices})

    def test_default_sarvam_voice_is_verified_speaker(self, client, tenant_admin):
        voices = data(client.get(
            f"{API}/providers/tts/sarvam/voices?model=bulbul:v3", headers=tenant_admin,
        ))
        defaults = [v for v in voices if v["isDefault"]]
        assert [v["providerVoiceId"] for v in defaults] == ["shubh"]

    def test_v2_speaker_on_v3_rejected_with_clear_error(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {"ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                       "ttsVoice": "anushka"},
        }))
        assert not result["valid"]
        assert any("'anushka' does not belong to provider 'sarvam'" in e
                   for e in result["errors"])

    def test_bot_update_rejects_outdated_speaker(self, client, tenant_admin):
        response = client.put(f"{API}/bots/bot-101/voice-settings", headers=tenant_admin, json={
            "ttsProvider": "sarvam", "ttsModel": "bulbul:v3", "ttsVoice": "anushka",
        })
        assert response.status_code == 422
        body = response.json()
        assert any("anushka" in e for e in body.get("errors", []))

    def test_case_and_whitespace_tolerated_in_validation(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {"ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                       "ttsVoice": "  SHUBH  "},
        }))
        voice_errors = [e for e in result["errors"] if "voice" in e.lower()]
        assert voice_errors == []

    def test_missing_speaker_is_allowed_but_model_required_rules_hold(self, client, tenant_admin):
        result = data(client.post(f"{API}/providers/validate-config", headers=tenant_admin, json={
            "botId": "bot-101",
            "config": {"ttsProvider": "sarvam", "ttsModel": "bulbul:v3"},
        }))
        # No voice given: runtime falls back to the model default explicitly.
        voice_errors = [e for e in result["errors"] if "voice" in e.lower()]
        assert voice_errors == []

    def test_inactive_voice_rejected(self, client, tenant_admin):
        from shared.models.bot_models import VoiceProfile

        db = get_sessionmaker()()
        vid = f"vp-test-inactive-{uuid.uuid4().hex[:8]}"
        try:
            db.add(VoiceProfile(
                id=vid, name="Test Inactive", gender="male", languages=[],
                provider="sarvam", provider_voice_id=f"ghost-{vid}",
                model_codes=["bulbul:v3"], status="inactive",
            ))
            db.commit()
            result = data(client.post(
                f"{API}/providers/validate-config", headers=tenant_admin, json={
                    "botId": "bot-101",
                    "config": {"ttsProvider": "sarvam", "ttsModel": "bulbul:v3",
                               "ttsVoice": vid},
                }))
            assert not result["valid"]
            assert any(f"'{vid}' does not belong to provider 'sarvam'" in e
                       for e in result["errors"])
        finally:
            db.query(VoiceProfile).filter(VoiceProfile.id == vid).delete()
            db.commit()
            db.close()
