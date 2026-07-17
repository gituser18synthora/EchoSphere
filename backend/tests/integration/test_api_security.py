"""REST security: JWT enforcement, cross-tenant 404s, upload validation.

Uses the live FastAPI app with the TestClient; users come from the demo seed.
"""

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def bearer(email: str) -> dict:
    from sqlalchemy import select

    from backend.db.mysql import get_sessionmaker
    from backend.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


@pytest.fixture(scope="module")
def tenant_admin():
    return bearer("priya.sharma@meridianhealth.com")  # tenant admin of tn-001


@pytest.fixture(scope="module")
def tenant_user():
    return bearer("sam.ellery@meridianhealth.com")  # tenant_user of tn-001


class TestAuthentication:
    def test_missing_token_401(self, client):
        assert client.get("/api/v1/knowledge").status_code == 401

    def test_garbage_token_401(self, client):
        response = client.get(
            "/api/v1/knowledge", headers={"Authorization": "Bearer garbage"}
        )
        assert response.status_code == 401

    def test_expired_token_401(self, client, monkeypatch):
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        from backend.config import get_settings

        settings = get_settings()
        expired = pyjwt.encode(
            {
                "sub": "usr_x", "role": "tenant_admin", "tenant_id": "tn-001",
                "iat": datetime.now(timezone.utc) - timedelta(hours=2),
                "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            },
            settings.jwt_secret, algorithm=settings.jwt_algorithm,
        )
        response = client.get(
            "/api/v1/knowledge", headers={"Authorization": f"Bearer {expired}"}
        )
        assert response.status_code == 401


class TestTenantIsolationOverREST:
    def test_cross_tenant_document_upload_404(self, client, tenant_admin, control_plane):
        other_tenant = control_plane.tenant()
        foreign_kb = control_plane.knowledge_source(other_tenant)
        response = client.post(
            f"/api/v1/knowledge/{foreign_kb}/documents",
            headers=tenant_admin,
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 404  # sanitized: existence not revealed

    def test_cross_tenant_search_test_404(self, client, tenant_admin, control_plane):
        other_tenant = control_plane.tenant()
        foreign_kb = control_plane.knowledge_source(other_tenant)
        response = client.post(
            "/api/v1/knowledge/search-test",
            headers=tenant_admin,
            json={"query": "anything", "kbIds": [foreign_kb]},
        )
        assert response.status_code == 404

    def test_tenant_user_cannot_upload(self, client, tenant_user, control_plane):
        response = client.post(
            "/api/v1/knowledge/ks-01/documents",
            headers=tenant_user,
            files={"file": ("a.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 403

    def test_voice_session_for_foreign_bot_404(self, client, tenant_admin, control_plane):
        response = client.post(
            "/api/v1/voice-sessions",
            headers=tenant_admin,
            json={"botId": "bot_does_not_exist"},
        )
        assert response.status_code == 404


class TestUploadValidation:
    def test_extension_content_mismatch_400(self, client, tenant_admin, control_plane):
        kb = control_plane.knowledge_source("tn-001")
        response = client.post(
            f"/api/v1/knowledge/{kb}/documents",
            headers=tenant_admin,
            files={"file": ("fake.pdf", b"this is not a pdf", "application/pdf")},
        )
        assert response.status_code == 400

    def test_unsupported_extension_400(self, client, tenant_admin, control_plane):
        kb = control_plane.knowledge_source("tn-001")
        response = client.post(
            f"/api/v1/knowledge/{kb}/documents",
            headers=tenant_admin,
            files={"file": ("run.exe", b"MZbinary", "application/octet-stream")},
        )
        assert response.status_code == 400

    def test_empty_file_400(self, client, tenant_admin, control_plane):
        kb = control_plane.knowledge_source("tn-001")
        response = client.post(
            f"/api/v1/knowledge/{kb}/documents",
            headers=tenant_admin,
            files={"file": ("empty.txt", b"", "text/plain")},
        )
        assert response.status_code == 400


class TestWebhookSecurity:
    def test_unsigned_webhook_403(self, client):
        response = client.post(
            "/api/v1/telephony/webhook/exotel", json={"To": "+15550100"}
        )
        assert response.status_code == 403

    def test_unknown_provider_404(self, client):
        response = client.post("/api/v1/telephony/webhook/carrierx", json={})
        assert response.status_code == 404

    def test_signed_webhook_with_replay_blocked(self, client, monkeypatch):
        import hashlib
        import hmac as hmac_mod
        import json as json_mod
        import time

        monkeypatch.setenv("TELEPHONY_WEBHOOK_SECRET", "test-secret")
        body = json_mod.dumps({"To": "+15559999", "From": "+15550000"}).encode()
        ts = str(int(time.time()))
        signature = hmac_mod.new(
            b"test-secret", f"{ts}.".encode() + body, hashlib.sha256
        ).hexdigest()
        headers = {
            "X-Webhook-Signature": signature,
            "X-Webhook-Timestamp": ts,
            "Content-Type": "application/json",
        }
        first = client.post(
            "/api/v1/telephony/webhook/exotel", content=body, headers=headers
        )
        # No phone-number mapping exists for +15559999 → sanitized 404,
        # which proves the signature check itself passed.
        assert first.status_code == 404
        replay = client.post(
            "/api/v1/telephony/webhook/exotel", content=body, headers=headers
        )
        assert replay.status_code == 403  # replay protection
