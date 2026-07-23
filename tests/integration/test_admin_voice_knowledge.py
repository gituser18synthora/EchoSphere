"""Provider-aware voice validation + admin knowledge filtering/detail.

- /master/voices: provider-specific settings validated against the provider
  model's params_schema (ElevenLabs vs Sarvam fields are NOT interchangeable),
  model↔provider membership, locale↔model language support.
- /knowledge: tenant/status/type server-side filters (admin view).
- /knowledge/{id}: full detail endpoint with tenant enforcement.

Live-app harness (same as test_admin_features); unique suffixes, module teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_created: list[tuple[str, str]] = []


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for table, row_id in reversed(_created):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})


def _bearer(email: str) -> dict:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


@pytest.fixture(scope="module")
def super_admin():
    return _bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def tenant_member():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant admin of tn-001


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def _field_errors(response) -> dict[str, str]:
    body = response.json()
    return {e["field"]: e["message"] for e in body.get("errors", []) if isinstance(e, dict)}


def _make_tenant(name: str) -> str:
    from shared.db.mysql import get_sessionmaker
    from shared.models import Tenant

    session = get_sessionmaker()()
    try:
        tenant_id = f"tn_test_{uuid.uuid4().hex[:10]}"
        session.add(Tenant(id=tenant_id, name=f"{name} {tenant_id[-4:]}",
                           domain=f"{tenant_id}.example.test", status="active"))
        session.commit()
        _created.append(("tenants", tenant_id))
        return tenant_id
    finally:
        session.close()


def _make_kb(tenant_id: str, *, name: str, status: str = "indexed") -> str:
    from shared.db.mysql import get_sessionmaker
    from shared.models import KnowledgeSource

    session = get_sessionmaker()()
    try:
        kb_id = f"kstest_{uuid.uuid4().hex[:10]}"
        session.add(KnowledgeSource(id=kb_id, tenant_id=tenant_id, scope="tenant",
                                    type="document", name=name, status=status))
        session.commit()
        _created.append(("knowledge_sources", kb_id))
        return kb_id
    finally:
        session.close()


ELEVEN_SETTINGS = {
    "stability": 0.4, "similarity_boost": 0.9, "style": 0.1,
    "use_speaker_boost": True, "speed": 1.0,
}


class TestVoiceProviderValidation:
    def _create(self, client, headers, **overrides):
        payload = {"name": f"Voice {uuid.uuid4().hex[:6]}", **overrides}
        return client.post(f"{API}/master/voices", headers=headers, json=payload)

    def test_elevenlabs_settings_saved_and_returned(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="elevenlabs", providerVoiceId="v_abc123",
            modelCodes=["eleven_flash_v2_5"], providerSettings=ELEVEN_SETTINGS,
        )
        created = _data(response)
        _created.append(("voice_profiles", created["id"]))
        assert created["modelCodes"] == ["eleven_flash_v2_5"]
        for key, value in ELEVEN_SETTINGS.items():
            assert created["providerSettings"][key] == value

    def test_sarvam_settings_saved(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="sarvam", providerVoiceId="shubh",
            modelCodes=["bulbul:v3"], locale="hi-IN",
            providerSettings={"pace": 1.2, "temperature": 0.5},
        )
        created = _data(response)
        _created.append(("voice_profiles", created["id"]))
        assert created["providerSettings"]["pace"] == 1.2

    def test_elevenlabs_fields_rejected_for_sarvam(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="sarvam", providerVoiceId="shubh", modelCodes=["bulbul:v3"],
            providerSettings={"stability": 0.5},  # an ElevenLabs parameter
        )
        assert response.status_code == 422
        assert "unknown parameter 'stability'" in _field_errors(response)["providerSettings"]

    def test_sarvam_fields_rejected_for_elevenlabs(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="elevenlabs", providerVoiceId="v_x", modelCodes=["eleven_flash_v2_5"],
            providerSettings={"pace": 1.2},  # a Sarvam parameter
        )
        assert response.status_code == 422
        assert "unknown parameter 'pace'" in _field_errors(response)["providerSettings"]

    def test_out_of_range_setting_rejected(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="elevenlabs", providerVoiceId="v_x", modelCodes=["eleven_flash_v2_5"],
            providerSettings={"stability": 1.5},
        )
        assert response.status_code == 422
        assert "between" in _field_errors(response)["providerSettings"]

    def test_model_of_other_provider_rejected(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="elevenlabs", providerVoiceId="v_x", modelCodes=["bulbul:v3"],
        )
        assert response.status_code == 422
        assert "does not belong" in _field_errors(response)["modelCodes"]

    def test_unknown_provider_rejected(self, client, super_admin):
        response = self._create(client, super_admin, provider="acme-tts")
        assert response.status_code == 422
        assert "provider" in _field_errors(response)

    def test_settings_without_model_use_provider_default_model(self, client, super_admin):
        # Valid vs the provider's default model schema (eleven_flash_v2_5).
        ok_response = self._create(
            client, super_admin,
            provider="elevenlabs", providerVoiceId="v_y",
            providerSettings={"stability": 0.2},
        )
        created = _data(ok_response)
        _created.append(("voice_profiles", created["id"]))

        bad = self._create(
            client, super_admin,
            provider="elevenlabs", providerVoiceId="v_z",
            providerSettings={"pace": 1.0},
        )
        assert bad.status_code == 422

    def test_settings_rejected_for_provider_without_models(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="platform", providerSettings={"anything": 1},
        )
        assert response.status_code == 422
        assert "no configured models" in _field_errors(response)["providerSettings"]

    def test_unsupported_locale_rejected(self, client, super_admin):
        response = self._create(
            client, super_admin,
            provider="sarvam", providerVoiceId="shubh",
            modelCodes=["bulbul:v3"], locale="fr-FR",
        )
        assert response.status_code == 422
        assert "not supported" in _field_errors(response)["locale"]

    def test_edit_revalidates_against_stored_model(self, client, super_admin):
        name = f"Edit Voice {_SUFFIX}"
        created = _data(self._create(
            client, super_admin, name=name,
            provider="elevenlabs", providerVoiceId="v_edit",
            modelCodes=["eleven_flash_v2_5"], providerSettings=ELEVEN_SETTINGS,
        ))
        _created.append(("voice_profiles", created["id"]))
        # Loading in edit mode: settings round-trip through the serializer.
        listed = _data(client.get(f"{API}/master/voices?search={name}", headers=super_admin))
        row = next(v for v in listed if v["id"] == created["id"])
        assert row["providerSettings"]["similarity_boost"] == 0.9
        assert row["modelCodes"] == ["eleven_flash_v2_5"]

        bad = client.patch(f"{API}/master/voices/{created['id']}", headers=super_admin,
                           json={"providerSettings": {"stability": 9}})
        assert bad.status_code == 422
        good = _data(client.patch(f"{API}/master/voices/{created['id']}", headers=super_admin,
                                  json={"providerSettings": {**ELEVEN_SETTINGS, "speed": 1.1}}))
        assert good["providerSettings"]["speed"] == 1.1


class TestAdminKnowledgeFilters:
    @pytest.fixture(scope="class")
    def seeded_kbs(self, client):
        tenant_a = _make_tenant("Filter Tenant A")
        tenant_b = _make_tenant("Filter Tenant B")
        kb_a = _make_kb(tenant_a, name=f"Alpha Docs {_SUFFIX}", status="indexed")
        kb_b = _make_kb(tenant_b, name=f"Beta Docs {_SUFFIX}", status="failed")
        return tenant_a, tenant_b, kb_a, kb_b

    def test_tenant_filter_returns_only_that_tenant(self, client, super_admin, seeded_kbs):
        tenant_a, _, kb_a, kb_b = seeded_kbs
        items = _data(client.get(f"{API}/knowledge?tenantId={tenant_a}&pageSize=100", headers=super_admin))
        ids = {k["id"] for k in items}
        assert kb_a in ids and kb_b not in ids

    def test_no_tenant_filter_shows_platform_view(self, client, super_admin, seeded_kbs):
        _, _, kb_a, kb_b = seeded_kbs
        items = _data(client.get(f"{API}/knowledge?pageSize=200&search={_SUFFIX}", headers=super_admin))
        ids = {k["id"] for k in items}
        assert {kb_a, kb_b} <= ids

    def test_status_and_search_filters_combine(self, client, super_admin, seeded_kbs):
        _, _, kb_a, kb_b = seeded_kbs
        items = _data(client.get(
            f"{API}/knowledge?search={_SUFFIX}&status=failed&pageSize=100", headers=super_admin))
        ids = {k["id"] for k in items}
        assert kb_b in ids and kb_a not in ids

    def test_type_filter(self, client, super_admin, seeded_kbs):
        items = _data(client.get(
            f"{API}/knowledge?search={_SUFFIX}&type=document&pageSize=100", headers=super_admin))
        assert all(k["type"] == "document" for k in items)

    def test_tenant_member_cannot_use_filter_to_read_other_tenants(self, client, tenant_member, seeded_kbs):
        tenant_a, _, _kb_a, _ = seeded_kbs
        # A tenant member asking for another tenant's data is rejected outright.
        response = client.get(f"{API}/knowledge?tenantId={tenant_a}&pageSize=200", headers=tenant_member)
        assert response.status_code == 403


class TestKnowledgeDetail:
    @pytest.fixture(scope="class")
    def kb(self, request, client):
        tenant = _make_tenant("Detail Tenant")
        kb_id = _make_kb(tenant, name=f"Detail KB {_SUFFIX}", status="indexed")
        request.cls.tenant_id = tenant
        return kb_id

    def test_detail_shape(self, client, super_admin, kb):
        detail = _data(client.get(f"{API}/knowledge/{kb}", headers=super_admin))
        assert detail["id"] == kb
        assert detail["tenantId"] == self.tenant_id
        assert detail["tenantName"]
        assert "stats" in detail and "documents" in detail
        stats = detail["stats"]
        for key in ("documentCount", "activeChunks", "embeddedChunks", "embeddingModels", "lastError"):
            assert key in stats
        assert detail["createdAt"]

    def test_cross_tenant_detail_is_404(self, client, tenant_member, kb):
        response = client.get(f"{API}/knowledge/{kb}", headers=tenant_member)
        assert response.status_code == 404

    def test_unknown_kb_is_404(self, client, super_admin):
        response = client.get(f"{API}/knowledge/ks_does_not_exist", headers=super_admin)
        assert response.status_code == 404

    def test_requires_auth(self, client, kb):
        assert client.get(f"{API}/knowledge/{kb}").status_code == 401
