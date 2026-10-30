"""Tenant API tokens + conversation lookup by telephony call id — end to end.

Runs on a disposable MySQL schema (migrated from scratch with Alembic, seeded
with the development profile) because the feature adds columns/tables that
the shared development database only gets once its migration is applied.
Mongo and Redis are the shared local services, as in the other suites.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text

from shared.config import get_settings

pytestmark = pytest.mark.integration

API = "/api/v1"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DISPOSABLE_DB_PREFIX = "echosphere_disposable_"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture(scope="module")
def migrated_database():
    """A fresh schema at Alembic head with the base (dev) seed — dropped after."""
    import shared.db.mysql as mysql_db
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    if settings.mysql_host not in _LOCAL_HOSTS:
        pytest.fail(f"refusing to create a disposable database on {settings.mysql_host!r}")
    name = f"{DISPOSABLE_DB_PREFIX}{uuid.uuid4().hex[:10]}"
    admin = create_engine(settings.mysql_url.replace(f"/{settings.mysql_database}?", "/?"))
    with admin.begin() as conn:
        conn.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    previous_env = os.environ.get("MYSQL_DATABASE")
    previous_engine, previous_sessionmaker = mysql_db._engine, mysql_db._SessionLocal
    os.environ["MYSQL_DATABASE"] = name
    get_settings.cache_clear()
    mysql_db._engine = None
    mysql_db._SessionLocal = None
    try:
        assert get_settings().mysql_database == name
        command.upgrade(Config(os.path.join(REPO, "backend/alembic.ini")), "head")
        from backend.seeds.base_seed import run_base_seed

        run_base_seed(production=False)
        yield name
    finally:
        if mysql_db._engine is not None:
            mysql_db._engine.dispose()
        mysql_db._engine, mysql_db._SessionLocal = previous_engine, previous_sessionmaker
        if previous_env is None:
            os.environ.pop("MYSQL_DATABASE", None)
        else:
            os.environ["MYSQL_DATABASE"] = previous_env
        get_settings.cache_clear()
        with admin.begin() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{name}`"))
        admin.dispose()


@pytest.fixture(scope="module")
def client(migrated_database):
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as test_client:
        yield test_client


def _bearer_for(email: str) -> dict:
    from backend.core.security import create_access_token
    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        return {"Authorization": f"Bearer {create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)}"}
    finally:
        session.close()


@pytest.fixture(scope="module")
def super_admin(migrated_database):
    return _bearer_for(get_settings().superadmin_email)


def _data(response, expect=200):
    assert response.status_code == expect, response.text
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def tenants(client, super_admin):
    """Two tenants created through the API, each with a bot and conversations."""
    from shared.db.mysql import get_sessionmaker
    from shared.ids import new_id
    from shared.models import ConversationSession, VoiceBot

    out = {}
    for label in ("a", "b"):
        suffix = uuid.uuid4().hex[:8]
        created = _data(client.post(f"{API}/tenants", headers=super_admin, json={
            "name": f"Token Test {label.upper()} {suffix}",
            "domain": f"token-{label}-{suffix}.example.test",
            "adminEmail": f"token-{label}-{suffix}@example.com",
            "status": "active",
        }), expect=201)
        session = get_sessionmaker()()
        try:
            bot = VoiceBot(id=new_id("bot"), tenant_id=created["id"], name=f"Bot {label}",
                           status="published")
            session.add(bot)
            session.flush()
            base = datetime(2026, 10, 20, 9, 0, 0)
            rows = []
            for index, (call_id, duration) in enumerate([
                (f"CALL-{label}-{suffix}-1", 120), (f"CALL-{label}-{suffix}-2", 30),
                (f"CALL-{label}-{suffix}-dup", 45), (f"CALL-{label}-{suffix}-dup", 90),
                (None, 60),
            ]):
                rows.append(ConversationSession(
                    id=new_id("cv"), tenant_id=created["id"], bot_id=bot.id, channel="voice",
                    caller_masked="+91XXXXXX1234", started_at=base + timedelta(minutes=index),
                    duration_sec=duration, sentiment="neutral", contained=True,
                    cost_usd=0.01 * (index + 1), language="hi-IN", flagged=index == 1,
                    status="completed", call_id=call_id, session_id=f"vs_{uuid.uuid4().hex[:12]}",
                ))
            session.add_all(rows)
            session.commit()
            out[label] = {
                "tenant": created, "bot_id": bot.id, "suffix": suffix,
                "conversations": [(r.id, r.call_id, r.duration_sec) for r in rows],
                "token": created["apiToken"]["token"],
            }
        finally:
            session.close()
    return out


def _token_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestProvisioning:
    def test_create_tenant_returns_the_token_once(self, tenants):
        api_token = tenants["a"]["tenant"]["apiToken"]
        assert api_token["configured"] is True
        assert api_token["token"].startswith("esk_") and len(api_token["token"]) > 40
        assert api_token["prefix"] == api_token["token"][:12]
        assert api_token["readOnly"] is True and api_token["role"] == "tenant_user"
        assert api_token["serviceAccountEmail"].endswith("@service.echosphere.internal")

    def test_metadata_never_contains_the_token(self, client, super_admin, tenants):
        meta = _data(client.get(f"{API}/tenants/{tenants['a']['tenant']['id']}/api-token", headers=super_admin))
        assert meta["configured"] is True and meta["status"] == "active"
        assert meta["token"] is None
        assert meta["prefix"] == tenants["a"]["tenant"]["apiToken"]["prefix"]

    def test_only_super_admins_manage_tokens(self, client, tenants):
        tenant_admin = _bearer_for(tenants["a"]["tenant"]["adminEmail"])
        tid = tenants["a"]["tenant"]["id"]
        assert client.get(f"{API}/tenants/{tid}/api-token", headers=tenant_admin).status_code == 403
        assert client.post(f"{API}/tenants/{tid}/api-token/rotate", headers=tenant_admin).status_code == 403
        # The token itself cannot manage tokens either (read-only + role).
        assert client.post(f"{API}/tenants/{tid}/api-token/rotate",
                           headers=_token_headers(tenants["a"]["token"])).status_code == 403

    def test_service_account_is_hidden_and_cannot_sign_in(self, client, super_admin, tenants):
        tid = tenants["a"]["tenant"]["id"]
        email = tenants["a"]["tenant"]["apiToken"]["serviceAccountEmail"]
        team = _data(client.get(f"{API}/users?tenantId={tid}&pageSize=200", headers=super_admin))
        assert all(member["email"] != email for member in team)
        tenant = _data(client.get(f"{API}/tenants/{tid}", headers=super_admin))
        assert tenant["users"] == 1          # the admin only; the service account is not a seat
        response = client.post(f"{API}/auth/login", json={"email": email, "password": "anything"})
        assert response.status_code == 401   # unknown password, and never a session even if known
        from shared.db.mysql import get_sessionmaker
        from shared.models import User

        session = get_sessionmaker()()
        try:
            account = session.execute(select(User).where(User.email == email)).scalar_one()
            assert account.is_service_account is True and account.tenant_id == tid
            user_id = account.id
        finally:
            session.close()
        response = client.post(f"{API}/users/{user_id}/reset-password", headers=super_admin)
        assert response.status_code == 400
        response = client.delete(f"{API}/users/{user_id}", headers=super_admin)
        assert response.status_code == 400


class TestTokenReads:
    def test_token_lists_and_filters_conversations(self, client, tenants):
        t = tenants["a"]
        headers = _token_headers(t["token"])
        response = client.get(f"{API}/conversations?pageSize=50", headers=headers)
        items = _data(response)
        assert {c["id"] for c in items} == {cid for cid, _, _ in t["conversations"]}
        assert response.json()["meta"]["total"] == 5
        assert all(c["callId"] is not None or c["id"] == t["conversations"][-1][0] for c in items)
        # costs.view is withheld from the tenant_user role the token acts as.
        assert all(c["costUsd"] is None for c in items)

        # callId: exact filter; duplicates come back as two rows, newest first.
        dup = f"CALL-a-{t['suffix']}-dup"
        items = _data(client.get(f"{API}/conversations?callId={dup}", headers=headers))
        assert [c["durationSec"] for c in items] == [90, 45]
        # search also matches call ids (LIKE), alongside id/caller/reason.
        items = _data(client.get(f"{API}/conversations?search={t['suffix']}-2", headers=headers))
        assert [c["callId"] for c in items] == [f"CALL-a-{t['suffix']}-2"]
        # existing filters still apply; sorting is honoured now.
        items = _data(client.get(f"{API}/conversations?flagged=true", headers=headers))
        assert [c["durationSec"] for c in items] == [30]
        items = _data(client.get(f"{API}/conversations?sortBy=durationSec&sortDir=asc", headers=headers))
        assert [c["durationSec"] for c in items] == [30, 45, 60, 90, 120]
        items = _data(client.get(f"{API}/conversations?sortBy=durationSec&sortDir=desc&pageSize=2&page=2", headers=headers))
        assert [c["durationSec"] for c in items] == [60, 45]
        items = _data(client.get(f"{API}/conversations?dateFrom=2026-10-21&dateTo=2026-10-21", headers=headers))
        assert items == []

    def test_detail_by_conversation_id_or_call_id(self, client, tenants):
        t = tenants["a"]
        headers = _token_headers(t["token"])
        cv_id, call_id, _ = t["conversations"][0]
        by_id = _data(client.get(f"{API}/conversations/{cv_id}", headers=headers))
        by_call = _data(client.get(f"{API}/conversations/{call_id}", headers=headers))
        assert by_id["id"] == by_call["id"] == cv_id
        assert by_id["callId"] == call_id
        assert "transcript" in by_id and "summary" in by_id
        # A duplicated call id resolves to the newest call.
        dup = _data(client.get(f"{API}/conversations/CALL-a-{t['suffix']}-dup", headers=headers))
        assert dup["durationSec"] == 90
        assert client.get(f"{API}/conversations/CALL-nope", headers=headers).status_code == 404
        # X-API-Key works the same as the bearer form.
        assert client.get(f"{API}/conversations/{cv_id}", headers={"X-API-Key": t["token"]}).status_code == 200

    def test_tenant_isolation_by_id_and_call_id(self, client, tenants):
        a, b = tenants["a"], tenants["b"]
        headers = _token_headers(a["token"])
        other_id, other_call, _ = b["conversations"][0]
        assert client.get(f"{API}/conversations/{other_id}", headers=headers).status_code == 404
        assert client.get(f"{API}/conversations/{other_call}", headers=headers).status_code == 404
        assert client.get(f"{API}/conversations?callId={other_call}", headers=headers).json()["meta"]["total"] == 0
        assert client.get(f"{API}/conversations?tenantId={b['tenant']['id']}", headers=headers).status_code == 403

    def test_super_admin_lookup_by_call_id_needs_a_tenant(self, client, super_admin, tenants):
        a = tenants["a"]
        _, call_id, _ = a["conversations"][0]
        assert client.get(f"{API}/conversations/{call_id}", headers=super_admin).status_code == 404
        found = _data(client.get(f"{API}/conversations/{call_id}?tenantId={a['tenant']['id']}", headers=super_admin))
        assert found["callId"] == call_id

    def test_token_is_read_only(self, client, tenants):
        t = tenants["a"]
        headers = _token_headers(t["token"])
        cv_id, _, _ = t["conversations"][0]
        assert client.patch(f"{API}/conversations/{cv_id}", headers=headers, json={"flagged": True}).status_code == 403
        assert client.post(f"{API}/conversations", headers=headers, json={"botId": t["bot_id"]}).status_code == 403
        assert client.post(f"{API}/auth/logout", headers=headers).status_code == 403
        # Still flagged=false: the write never reached the row.
        assert _data(client.get(f"{API}/conversations/{cv_id}", headers=headers))["flagged"] is False

    def test_password_login_path_is_unchanged(self, client, tenants):
        tenant_admin = _bearer_for(tenants["a"]["tenant"]["adminEmail"])
        items = _data(client.get(f"{API}/conversations?pageSize=5", headers=tenant_admin))
        assert len(items) == 5 and all(c["costUsd"] is not None for c in items)


class TestRotationAndRevocation:
    def test_rotate_replaces_and_revokes(self, client, super_admin, tenants):
        t = tenants["b"]
        tid = t["tenant"]["id"]
        old = t["token"]
        rotated = _data(client.post(f"{API}/tenants/{tid}/api-token/rotate", headers=super_admin), expect=201)
        new = rotated["token"]
        assert new.startswith("esk_") and new != old
        assert client.get(f"{API}/conversations", headers=_token_headers(old)).status_code == 401
        assert client.get(f"{API}/conversations", headers=_token_headers(new)).status_code == 200
        meta = _data(client.get(f"{API}/tenants/{tid}/api-token", headers=super_admin))
        assert meta["prefix"] == new[:12] and meta["status"] == "active" and meta["lastUsedAt"]
        # The audit trail names prefixes only — never a token.
        audit = client.get(f"{API}/audit?pageSize=50", headers=super_admin)
        if audit.status_code == 200:
            blob = audit.text
            assert new not in blob and old not in blob
            assert "tenant API token" in blob
        t["token"] = new

    def test_revoke_without_replacement(self, client, super_admin, tenants):
        t = tenants["b"]
        tid = t["tenant"]["id"]
        revoked = _data(client.delete(f"{API}/tenants/{tid}/api-token", headers=super_admin))
        assert revoked["revoked"] == 1 and revoked["configured"] is False and revoked["status"] == "revoked"
        assert client.get(f"{API}/conversations", headers=_token_headers(t["token"])).status_code == 401
        # Generate again → usable; only one active row per tenant.
        again = _data(client.post(f"{API}/tenants/{tid}/api-token/rotate", headers=super_admin), expect=201)
        assert client.get(f"{API}/conversations", headers=_token_headers(again["token"])).status_code == 200
        from shared.db.mysql import get_sessionmaker
        from shared.models import TenantApiKey

        session = get_sessionmaker()()
        try:
            rows = session.scalars(select(TenantApiKey).where(TenantApiKey.tenant_id == tid)).all()
            assert sum(1 for r in rows if r.status == "active") == 1
            assert len(rows) == 3
            assert all(len(r.key_hash) == 64 and not r.key_hash.startswith("esk_") for r in rows)
        finally:
            session.close()

    def test_unknown_tenant_is_404(self, client, super_admin):
        assert client.get(f"{API}/tenants/tn_nope/api-token", headers=super_admin).status_code == 404
