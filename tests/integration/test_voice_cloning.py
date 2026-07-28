"""Tenant voice cloning: capability config, ElevenLabs IVC create/delete
(mocked at the HTTP-helper boundary), tenant isolation, bot selection,
runtime wire-voice resolution, file validation, provider-error mapping and
TTS metering with a cloned voice.

Runs against the live app + local databases. Every row created here is
uniquely suffixed and hard-deleted in teardown — demo data is never mutated.
"""

import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sa_text

import backend.routers.voice_clones as vc
from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_engine, get_sessionmaker

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
TENANT_A = "tn-001"
TENANT_B = "tn_22a809aecf66"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _bearer(email: str) -> dict:
    from sqlalchemy import select

    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def tenant_a_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


@pytest.fixture(scope="module")
def tenant_b_admin():
    return _bearer("admin@pokket.com")  # tenant_admin of tn_22a809aecf66


@pytest.fixture(scope="module")
def tenant_user():
    return _bearer("sam.ellery@meridianhealth.com")  # tenant_user — no manage_voices


@pytest.fixture(scope="module")
def super_admin():
    return _bearer("admin@aurexion.com")


@pytest.fixture(scope="module", autouse=True)
def _cleanup_voices():
    yield
    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "DELETE FROM audit_logs WHERE entity_type = 'voice_profile' AND entity_id IN "
            "(SELECT id FROM voice_profiles WHERE provider_voice_id LIKE :p)"
        ), {"p": f"pvtest_{_SUFFIX}%"})
        conn.execute(sa_text(
            "DELETE FROM voice_profiles WHERE provider_voice_id LIKE :p"
        ), {"p": f"pvtest_{_SUFFIX}%"})


def _wav_bytes(payload: bytes = b"\x00\x01" * 512) -> bytes:
    body = b"WAVEfmt " + (16).to_bytes(4, "little") + b"\x01\x00\x01\x00" + payload
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def _patch_elevenlabs(monkeypatch, *, voice_id: str, delete_ok: bool = True,
                      requires_verification: bool = False):
    calls = {"create": [], "delete": []}

    async def fake_create(api_key, *, name, samples, description, remove_background_noise):
        assert api_key, "clone must resolve a provider key server-side"
        calls["create"].append({
            "name": name, "samples": samples, "description": description,
            "removeBackgroundNoise": remove_background_noise,
        })
        return {"voice_id": voice_id, "requires_verification": requires_verification}

    async def fake_delete(api_key, provider_voice_id):
        calls["delete"].append(provider_voice_id)
        return delete_ok

    monkeypatch.setattr(vc, "_elevenlabs_create_voice", fake_create)
    monkeypatch.setattr(vc, "_elevenlabs_delete_voice", fake_delete)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    return calls


def _create_clone(client, headers, monkeypatch, *, name: str, tag: str):
    _patch_elevenlabs(monkeypatch, voice_id=f"pvtest_{_SUFFIX}{tag}")
    response = client.post(
        f"{API}/voice-clones", headers=headers,
        data={"provider": "elevenlabs", "name": name},
        files=[("files", (f"sample-{tag}.wav", _wav_bytes(), "audio/wav"))],
    )
    assert response.status_code == 201, response.text
    return _data(response)


# ── capability config ────────────────────────────────────────────────────────


class TestCloneConfig:
    def test_config_reports_provider_capabilities(self, client, tenant_a_admin):
        cfg = _data(client.get(f"{API}/voice-clones/config", headers=tenant_a_admin))
        providers = {p["code"]: p for p in cfg["providers"]}
        assert providers["elevenlabs"]["supportsCloning"] is True
        assert any(f["name"] == "removeBackgroundNoise"
                   for f in providers["elevenlabs"]["cloneParams"])
        if "sarvam" in providers:  # Sarvam cloning is Studio-only, no public API
            assert providers["sarvam"]["supportsCloning"] is False
            assert "Studio" in (providers["sarvam"]["reason"] or "")
        assert cfg["maxFiles"] >= 1 and cfg["maxFileMb"] >= 1
        assert "wav" in cfg["allowedExtensions"]

    def test_catalog_marks_cloning_capable_providers(self, client, tenant_a_admin):
        catalog = _data(client.get(f"{API}/providers/catalog?capability=tts",
                                   headers=tenant_a_admin))
        by_code = {p["code"]: p for p in catalog["tts"]}
        assert by_code["elevenlabs"]["supportsCloning"] is True
        if "sarvam" in by_code:
            assert by_code["sarvam"]["supportsCloning"] is False


# ── create ───────────────────────────────────────────────────────────────────


class TestCreateClone:
    def test_requires_manage_voices_permission(self, client, tenant_user):
        response = client.post(
            f"{API}/voice-clones", headers=tenant_user,
            data={"provider": "elevenlabs", "name": "Nope"},
            files=[("files", ("a.wav", _wav_bytes(), "audio/wav"))],
        )
        assert response.status_code == 403

    def test_create_persists_provider_voice_id(self, client, tenant_a_admin, monkeypatch):
        calls = _patch_elevenlabs(monkeypatch, voice_id=f"pvtest_{_SUFFIX}main",
                                  requires_verification=True)
        response = client.post(
            f"{API}/voice-clones", headers=tenant_a_admin,
            data={"provider": "elevenlabs", "name": f"Clone Main {_SUFFIX}",
                  "description": "narrator", "removeBackgroundNoise": "true"},
            files=[
                ("files", ("s1.wav", _wav_bytes(), "audio/wav")),
                ("files", ("s2.mp3", b"ID3" + b"\x00" * 64, "audio/mpeg")),
            ],
        )
        assert response.status_code == 201, response.text
        voice = _data(response)
        assert voice["providerVoiceId"] == f"pvtest_{_SUFFIX}main"
        assert voice["source"] == "cloned"
        assert voice["tenantId"] == TENANT_A
        assert voice["status"] == "active"
        assert voice["provider"] == "elevenlabs"
        assert voice["modelCodes"], "clone must be usable with the provider's active models"
        meta = voice["cloneMetadata"]
        assert meta["requiresVerification"] is True
        assert meta["removeBackgroundNoise"] is True
        assert [s["fileName"] for s in meta["samples"]] == ["s1.wav", "s2.mp3"]
        # Provider got the samples; nothing is stored locally beyond metadata.
        assert len(calls["create"]) == 1
        assert calls["create"][0]["removeBackgroundNoise"] is True
        # Duplicate name within the tenant is rejected.
        dup = client.post(
            f"{API}/voice-clones", headers=tenant_a_admin,
            data={"provider": "elevenlabs", "name": f"clone main {_SUFFIX}"},
            files=[("files", ("s.wav", _wav_bytes(), "audio/wav"))],
        )
        assert dup.status_code == 422
        assert "already exists" in dup.text

    def test_file_validation(self, client, tenant_a_admin, monkeypatch):
        _patch_elevenlabs(monkeypatch, voice_id=f"pvtest_{_SUFFIX}zz")
        base = {"provider": "elevenlabs", "name": f"Bad Files {_SUFFIX}"}
        bad_ext = client.post(f"{API}/voice-clones", headers=tenant_a_admin, data=base,
                              files=[("files", ("notes.txt", b"hello", "text/plain"))])
        assert bad_ext.status_code == 422 and "unsupported file type" in bad_ext.text.lower()
        empty = client.post(f"{API}/voice-clones", headers=tenant_a_admin, data=base,
                            files=[("files", ("empty.wav", b"", "audio/wav"))])
        assert empty.status_code == 422 and "empty" in empty.text.lower()
        fake_wav = client.post(f"{API}/voice-clones", headers=tenant_a_admin, data=base,
                               files=[("files", ("fake.wav", b"not audio at all", "audio/wav"))])
        assert fake_wav.status_code == 422 and "valid" in fake_wav.text.lower()
        no_name = client.post(f"{API}/voice-clones", headers=tenant_a_admin,
                              data={"provider": "elevenlabs", "name": "  "},
                              files=[("files", ("s.wav", _wav_bytes(), "audio/wav"))])
        assert no_name.status_code == 422

    def test_unsupported_provider_is_explicit(self, client, tenant_a_admin, monkeypatch):
        _patch_elevenlabs(monkeypatch, voice_id=f"pvtest_{_SUFFIX}yy")
        response = client.post(
            f"{API}/voice-clones", headers=tenant_a_admin,
            data={"provider": "sarvam", "name": f"Sarvam Clone {_SUFFIX}"},
            files=[("files", ("s.wav", _wav_bytes(), "audio/wav"))],
        )
        assert response.status_code == 422
        assert "Studio" in response.text  # documented capability gap, not a fake feature
        unknown = client.post(
            f"{API}/voice-clones", headers=tenant_a_admin,
            data={"provider": "doesnotexist", "name": "X"},
            files=[("files", ("s.wav", _wav_bytes(), "audio/wav"))],
        )
        assert unknown.status_code == 422


class TestProviderErrorMapping:
    def test_voice_limit_reached_maps_to_actionable_422(self):
        response = httpx.Response(
            400, json={"detail": {"status": "voice_limit_reached", "message": "limit"}},
            request=httpx.Request("POST", "https://api.elevenlabs.io/v1/voices/add"),
        )
        err = vc._elevenlabs_error(response)
        assert err.status_code == 422 and "custom-voice" in err.message

    def test_scoped_key_without_cloning_permission_is_actionable(self):
        response = httpx.Response(
            401, json={"detail": {"type": "authentication_error", "code": "unauthorized",
                                  "status": "missing_permissions",
                                  "message": "missing create_instant_voice_clone"}},
            request=httpx.Request("POST", "https://api.elevenlabs.io/v1/voices/add"),
        )
        err = vc._elevenlabs_error(response)
        assert err.status_code == 422
        assert "create_instant_voice_clone" in err.message

    def test_bad_key_maps_to_502_without_leaking(self):
        response = httpx.Response(
            401, json={"detail": "bad key"},
            request=httpx.Request("POST", "https://api.elevenlabs.io/v1/voices/add"),
        )
        err = vc._elevenlabs_error(response)
        assert err.status_code == 502
        assert "key" in err.message and "test-key" not in err.message

    def test_unknown_provider_error_is_generic(self):
        response = httpx.Response(
            500, text="boom",
            request=httpx.Request("POST", "https://api.elevenlabs.io/v1/voices/add"),
        )
        err = vc._elevenlabs_error(response)
        assert err.status_code == 502


# ── isolation ────────────────────────────────────────────────────────────────


class TestTenantIsolation:
    @pytest.fixture(scope="class")
    def clone_a(self, client, tenant_a_admin):
        mp = pytest.MonkeyPatch()
        try:
            return _create_clone(client, tenant_a_admin, mp,
                                 name=f"Isolation Voice {_SUFFIX}", tag="iso")
        finally:
            mp.undo()

    def test_other_tenant_cannot_see_or_touch(self, client, tenant_b_admin, clone_a):
        listed = _data(client.get(f"{API}/voice-clones", headers=tenant_b_admin))
        assert clone_a["id"] not in {v["id"] for v in listed}
        assert client.get(f"{API}/voice-clones/{clone_a['id']}",
                          headers=tenant_b_admin).status_code == 404
        assert client.patch(f"{API}/voice-clones/{clone_a['id']}", headers=tenant_b_admin,
                            json={"name": "hijack"}).status_code == 404
        assert client.delete(f"{API}/voice-clones/{clone_a['id']}",
                             headers=tenant_b_admin).status_code == 404

    def test_owner_sees_clone_everywhere(self, client, tenant_a_admin, clone_a):
        listed = _data(client.get(f"{API}/voice-clones", headers=tenant_a_admin))
        assert clone_a["id"] in {v["id"] for v in listed}
        catalog = _data(client.get(f"{API}/voices?source=cloned", headers=tenant_a_admin))
        assert clone_a["id"] in {v["id"] for v in catalog}
        provider_voices = _data(client.get(f"{API}/providers/tts/elevenlabs/voices",
                                           headers=tenant_a_admin))
        assert clone_a["id"] in {v["id"] for v in provider_voices}

    def test_clone_hidden_from_other_tenant_catalogs(self, client, tenant_b_admin, clone_a):
        catalog = _data(client.get(f"{API}/voices?includeInactive=true", headers=tenant_b_admin))
        assert clone_a["id"] not in {v["id"] for v in catalog}
        provider_voices = _data(client.get(f"{API}/providers/tts/elevenlabs/voices",
                                           headers=tenant_b_admin))
        assert clone_a["id"] not in {v["id"] for v in provider_voices}

    def test_other_tenant_cannot_preview_by_wire_id(self, client, tenant_b_admin, clone_a):
        response = client.post(f"{API}/providers/tts-preview", headers=tenant_b_admin, json={
            "provider": "elevenlabs", "model": "eleven_flash_v2_5",
            "voice": clone_a["providerVoiceId"], "language": "en-IN", "text": "hi",
        })
        assert response.status_code == 404

    def test_super_admin_can_inspect_tenant_clones(self, client, super_admin, clone_a):
        listed = _data(client.get(f"{API}/voice-clones?tenantId={TENANT_A}",
                                  headers=super_admin))
        assert clone_a["id"] in {v["id"] for v in listed}


# ── bot selection + runtime resolution ───────────────────────────────────────


@pytest.fixture(scope="module")
def test_bot(client, tenant_a_admin):
    created = _data(client.post(f"{API}/bots", headers=tenant_a_admin, json={
        "name": f"Clone Voice Bot {_SUFFIX}", "useCase": "clone-test", "languages": ["en-US"],
    }))
    bot_id = created["id"]
    yield {"id": bot_id}
    with get_engine().begin() as conn:
        for table in ("channel_configs", "voice_bot_settings", "voice_bot_readiness",
                      "bot_languages", "workflows", "phone_numbers", "prompts"):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE bot_id = :b"), {"b": bot_id})
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


class TestBotSelectionAndRuntime:
    @pytest.fixture(scope="class")
    def clone(self, client, tenant_a_admin):
        mp = pytest.MonkeyPatch()
        try:
            return _create_clone(client, tenant_a_admin, mp,
                                 name=f"Runtime Voice {_SUFFIX}", tag="rt")
        finally:
            mp.undo()

    def test_bot_can_select_cloned_voice(self, client, tenant_a_admin, test_bot, clone):
        response = client.put(
            f"{API}/bots/{test_bot['id']}/voice-settings", headers=tenant_a_admin,
            json={"voiceId": clone["id"], "ttsProvider": "elevenlabs",
                  "ttsModel": "eleven_flash_v2_5", "ttsVoice": clone["id"]},
        )
        assert response.status_code == 200, response.text
        assert _data(response)["ttsVoice"] == clone["id"]

    def test_other_tenants_bot_cannot_select_it(self, client, tenant_b_admin, clone):
        created = _data(client.post(f"{API}/bots", headers=tenant_b_admin, json={
            "name": f"B Bot {_SUFFIX}", "useCase": "isolation", "languages": ["en-US"],
        }))
        try:
            by_fk = client.put(
                f"{API}/bots/{created['id']}/voice-settings", headers=tenant_b_admin,
                json={"voiceId": clone["id"]},
            )
            assert by_fk.status_code == 422
            by_engine = client.put(
                f"{API}/bots/{created['id']}/voice-settings", headers=tenant_b_admin,
                json={"ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
                      "ttsVoice": clone["id"]},
            )
            assert by_engine.status_code == 422
            by_wire = client.put(
                f"{API}/bots/{created['id']}/voice-settings", headers=tenant_b_admin,
                json={"ttsProvider": "elevenlabs", "ttsModel": "eleven_flash_v2_5",
                      "ttsVoice": clone["providerVoiceId"]},
            )
            # The raw wire id must not validate as a catalog voice for tenant B.
            assert by_wire.status_code in (200, 422)
            if by_wire.status_code == 200:  # passthrough wire codes are allowed...
                session = get_sessionmaker()()
                try:
                    from shared.bot_config import _wire_voice
                    # ...but the runtime still refuses the cross-tenant profile id.
                    assert _wire_voice(session, "elevenlabs", clone["id"],
                                       created["tenantId"]) == ""
                finally:
                    session.close()
        finally:
            with get_engine().begin() as conn:
                for table in ("channel_configs", "voice_bot_settings", "voice_bot_readiness",
                              "bot_languages", "workflows", "phone_numbers", "prompts"):
                    conn.execute(sa_text(f"DELETE FROM `{table}` WHERE bot_id = :b"),
                                 {"b": created["id"]})
                conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"),
                             {"b": created["id"]})

    def test_runtime_wire_voice_resolution(self, clone):
        from shared.bot_config import _wire_voice

        session = get_sessionmaker()()
        try:
            # Owner tenant: catalog id → provider voice id (the ElevenLabs clone id).
            assert _wire_voice(session, "elevenlabs", clone["id"], TENANT_A) == clone["providerVoiceId"]
            # Cross-tenant: fails closed instead of using another tenant's clone.
            assert _wire_voice(session, "elevenlabs", clone["id"], TENANT_B) == ""
            # Platform voices are unaffected by tenant scoping.
            from sqlalchemy import select

            from shared.models import VoiceProfile
            platform_voice = session.execute(select(VoiceProfile).where(
                VoiceProfile.provider == "elevenlabs",
                VoiceProfile.tenant_id.is_(None),
                VoiceProfile.is_deleted.is_(False),
            ).limit(1)).scalar_one_or_none()
            if platform_voice is not None:
                assert _wire_voice(session, "elevenlabs", platform_voice.id, TENANT_A) == (
                    platform_voice.provider_voice_id or platform_voice.name
                )
        finally:
            session.close()

    def test_find_voice_is_tenant_scoped(self, clone):
        from backend.core.provider_catalog import find_voice

        session = get_sessionmaker()()
        try:
            assert find_voice(session, "elevenlabs", clone["id"], tenant_id=TENANT_A) is not None
            assert find_voice(session, "elevenlabs", clone["id"], tenant_id=TENANT_B) is None
            assert find_voice(session, "elevenlabs", clone["providerVoiceId"],
                              tenant_id=TENANT_B) is None
            assert find_voice(session, "elevenlabs", clone["id"],
                              include_all_tenants=True) is not None
        finally:
            session.close()


# ── lifecycle: metadata, status, delete ──────────────────────────────────────


class TestCloneLifecycle:
    @pytest.fixture(scope="class")
    def clone(self, client, tenant_a_admin):
        mp = pytest.MonkeyPatch()
        try:
            return _create_clone(client, tenant_a_admin, mp,
                                 name=f"Lifecycle Voice {_SUFFIX}", tag="lc")
        finally:
            mp.undo()

    def test_update_local_metadata(self, client, tenant_a_admin, clone):
        updated = _data(client.patch(
            f"{API}/voice-clones/{clone['id']}", headers=tenant_a_admin,
            json={"description": "warm narrator", "gender": "female",
                  "sampleText": "Namaste, welcome to support."},
        ))
        assert updated["description"] == "warm narrator"
        assert updated["gender"] == "female"
        assert updated["sample"] == "Namaste, welcome to support."
        # Provider voice id is immutable through this API.
        assert updated["providerVoiceId"] == clone["providerVoiceId"]

    def test_deactivate_removes_from_selection(self, client, tenant_a_admin, clone):
        deactivated = _data(client.post(
            f"{API}/voice-clones/{clone['id']}/status", headers=tenant_a_admin,
            json={"status": "inactive"},
        ))
        assert deactivated["status"] == "inactive"
        provider_voices = _data(client.get(f"{API}/providers/tts/elevenlabs/voices",
                                           headers=tenant_a_admin))
        assert clone["id"] not in {v["id"] for v in provider_voices}
        reactivated = _data(client.post(
            f"{API}/voice-clones/{clone['id']}/status", headers=tenant_a_admin,
            json={"status": "active"},
        ))
        assert reactivated["status"] == "active"

    def test_delete_blocked_while_in_use(self, client, tenant_a_admin, test_bot, clone,
                                         monkeypatch):
        _patch_elevenlabs(monkeypatch, voice_id="unused")
        assert client.put(
            f"{API}/bots/{test_bot['id']}/voice-settings", headers=tenant_a_admin,
            json={"voiceId": clone["id"], "ttsProvider": "elevenlabs",
                  "ttsModel": "eleven_flash_v2_5", "ttsVoice": clone["id"]},
        ).status_code == 200
        blocked = client.delete(f"{API}/voice-clones/{clone['id']}", headers=tenant_a_admin)
        assert blocked.status_code == 409
        assert "Deactivate or archive" in blocked.text
        # Unassign, then deletion becomes possible.
        with get_engine().begin() as conn:
            conn.execute(sa_text(
                "UPDATE voice_bot_settings SET voice_id = NULL, tts_voice = NULL "
                "WHERE bot_id = :b"), {"b": test_bot["id"]})
            conn.execute(sa_text(
                "UPDATE voice_bots SET voice_id = NULL WHERE id = :b"), {"b": test_bot["id"]})

    def test_delete_keeps_local_row_when_provider_fails(self, client, tenant_a_admin,
                                                        clone, monkeypatch):
        _patch_elevenlabs(monkeypatch, voice_id="unused", delete_ok=False)
        response = client.delete(f"{API}/voice-clones/{clone['id']}", headers=tenant_a_admin)
        assert response.status_code == 502
        still_there = client.get(f"{API}/voice-clones/{clone['id']}", headers=tenant_a_admin)
        assert still_there.status_code == 200

    def test_delete_removes_provider_voice_first(self, client, tenant_a_admin, clone,
                                                 monkeypatch):
        calls = _patch_elevenlabs(monkeypatch, voice_id="unused", delete_ok=True)
        response = client.delete(f"{API}/voice-clones/{clone['id']}", headers=tenant_a_admin)
        assert response.status_code == 200
        assert _data(response) == {"deleted": True, "providerDeleted": True}
        assert calls["delete"] == [clone["providerVoiceId"]]
        assert client.get(f"{API}/voice-clones/{clone['id']}",
                          headers=tenant_a_admin).status_code == 404


# ── orphan handling ──────────────────────────────────────────────────────────


def test_provider_voice_removed_when_persistence_fails(client, tenant_a_admin, monkeypatch):
    calls = _patch_elevenlabs(monkeypatch, voice_id=f"pvtest_{_SUFFIX}orph")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(vc, "record_audit", boom)
    response = client.post(
        f"{API}/voice-clones", headers=tenant_a_admin,
        data={"provider": "elevenlabs", "name": f"Orphan Voice {_SUFFIX}"},
        files=[("files", ("s.wav", _wav_bytes(), "audio/wav"))],
    )
    assert response.status_code == 500
    # The provider voice created before the failure was cleaned up.
    assert calls["delete"] == [f"pvtest_{_SUFFIX}orph"]
    listed = _data(client.get(f"{API}/voice-clones", headers=tenant_a_admin))
    assert f"Orphan Voice {_SUFFIX}" not in {v["name"] for v in listed}


# ── metering ─────────────────────────────────────────────────────────────────


class TestClonedVoiceMetering:
    def test_tts_with_cloned_voice_is_priced_like_normal_tts(self, control_plane):
        from shared.billing.metering import record_usage_event
        from shared.models import UsageEvent, UsageRecord

        tenant_id = control_plane.tenant("Clone Metering")
        wire_id = f"pvtest_{_SUFFIX}meter"
        request_id = f"vctest:{_SUFFIX}:tts"
        session = get_sessionmaker()()
        try:
            event = record_usage_event(
                session,
                tenant_id=tenant_id,
                capability="tts",
                provider_code="elevenlabs",
                model_code="eleven_flash_v2_5",
                voice_code=wire_id,  # the cloned voice's provider id
                characters=1000,
                request_id=request_id,
            )
            assert event is not None
            # Priced from the provider/model pricing row — never from voice_id.
            assert event.pricing_status == "priced"
            assert float(event.cost_usd) == pytest.approx(0.05, abs=1e-6)
            snapshot = event.pricing_snapshot["characters"]
            assert snapshot["unit"] == "per_1k_characters"
            assert snapshot["priceId"]
            assert event.voice_code == wire_id

            # Same request id → idempotent, no double billing.
            duplicate = record_usage_event(
                session,
                tenant_id=tenant_id,
                capability="tts",
                provider_code="elevenlabs",
                model_code="eleven_flash_v2_5",
                voice_code=wire_id,
                characters=1000,
                request_id=request_id,
            )
            assert duplicate is None
        finally:
            from sqlalchemy import delete

            session.execute(delete(UsageEvent).where(UsageEvent.tenant_id == tenant_id))
            session.execute(delete(UsageRecord).where(UsageRecord.tenant_id == tenant_id))
            session.commit()
            session.close()


# ── regression: normal voices unaffected ─────────────────────────────────────


def test_platform_voices_still_visible_to_all_tenants(client, tenant_a_admin, tenant_b_admin):
    for headers in (tenant_a_admin, tenant_b_admin):
        voices = _data(client.get(f"{API}/voices?provider=elevenlabs", headers=headers))
        assert all(v["tenantId"] is None or v["source"] == "cloned" for v in voices)
        assert any(v["source"] == "platform" for v in voices)
