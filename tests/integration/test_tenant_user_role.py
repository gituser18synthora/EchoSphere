"""Tenant User role: shared-resource editing, section blocking, cost hiding.

A Tenant User works on the SAME tenant records as the Tenant Admin (no
per-user copies), can edit knowledge/prompts/voice/workflows/testing, and is
denied channels, integrations, settings, voice cloning and every financial
field — enforced by the API, not by hidden UI.
"""

import uuid
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import (
    ConversationSession,
    KnowledgeSource,
    Role,
    Tenant,
    TenantSetting,
    TestScenario,
    User,
    VoiceBot,
    VoiceBotSetting,
    Workflow,
)

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def workspace():
    """One tenant with a tenant admin, a tenant user, a bot and a costed
    conversation; a second tenant+bot for isolation checks."""
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    try:
        admin_role = session.execute(
            select(Role).where(Role.code == "tenant_admin")).scalar_one()
        user_role = session.execute(
            select(Role).where(Role.code == "tenant_user")).scalar_one()

        tenant = Tenant(
            id=new_id("tn"), name=f"TU Role Test {suffix}", code=f"turole_{suffix}",
            domain=f"turole-{suffix}.example.test", status="active",
        )
        other_tenant = Tenant(
            id=new_id("tn"), name=f"TU Foreign {suffix}", code=f"tufor_{suffix}",
            domain=f"tufor-{suffix}.example.test", status="active",
        )
        session.add_all([tenant, other_tenant])
        session.flush()

        admin = User(
            id=new_id("usr"), email=f"turole.admin.{suffix}@example.test",
            name="TU Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=tenant.id, status="active",
        )
        member = User(
            id=new_id("usr"), email=f"turole.user.{suffix}@example.test",
            name="TU Member", password_hash="x", role_id=user_role.id,
            tenant_id=tenant.id, status="active",
        )
        session.add_all([admin, member])
        session.flush()

        bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"TU Bot {suffix}",
            status="published", owner_user_id=admin.id,
        )
        foreign_bot = VoiceBot(
            id=new_id("bot"), tenant_id=other_tenant.id, name=f"Foreign Bot {suffix}",
            status="published", owner_user_id=admin.id,
        )
        conversation = ConversationSession(
            id=new_id("cv"), tenant_id=tenant.id, bot_id=bot.id,
            started_at=datetime.combine(date.today() - timedelta(days=1), time(12)),
            duration_sec=60, cost_usd=1.23, channel="voice",
            sentiment="neutral", intents=[], contained=True, status="completed",
        )
        session.add_all([bot, foreign_bot, conversation])
        session.commit()

        def bearer(u: User, role_code: str) -> dict:
            token = create_access_token(
                user_id=u.id, role=role_code, tenant_id=u.tenant_id)
            return {"Authorization": f"Bearer {token}"}

        yield {
            "tenant_id": tenant.id,
            "other_tenant_id": other_tenant.id,
            "bot_id": bot.id,
            "foreign_bot_id": foreign_bot.id,
            "conversation_id": conversation.id,
            "admin_id": admin.id,
            "member_id": member.id,
            "admin": bearer(admin, "tenant_admin"),
            "member": bearer(member, "tenant_user"),
        }
    finally:
        tenant_ids = [tenant.id, other_tenant.id]
        session.rollback()
        # TenantSetting rows are auto-created by GET /tenant/settings.
        for model in (Workflow, TestScenario, VoiceBotSetting, KnowledgeSource,
                      ConversationSession, VoiceBot, User, TenantSetting):
            session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
        session.close()


# ── User management & escalation ─────────────────────────────────────────────


class TestUserManagement:
    def test_admin_creates_tenant_user_in_own_tenant(self, client, workspace):
        # example.com, not .test — EmailStr rejects special-use TLDs.
        email = f"invited.{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            f"{API}/users", headers=workspace["admin"],
            json={"name": "Invited Member", "email": email, "roleCode": "tenant_user"},
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["roleCode"] == "tenant_user"
        assert "temporaryPassword" in data
        # Belongs to the admin's tenant — never a tenant of the caller's choosing.
        session = get_sessionmaker()()
        try:
            created = session.execute(
                select(User).where(User.email == email)).scalar_one()
            assert created.tenant_id == workspace["tenant_id"]
            session.delete(created)
            session.commit()
        finally:
            session.close()

    def test_tenant_user_cannot_create_users(self, client, workspace):
        response = client.post(
            f"{API}/users", headers=workspace["member"],
            json={"name": "X", "email": "x@example.com", "roleCode": "tenant_user"},
        )
        assert response.status_code == 403

    def test_tenant_user_cannot_change_own_role(self, client, workspace):
        response = client.patch(
            f"{API}/users/{workspace['member_id']}", headers=workspace["member"],
            json={"roleCode": "tenant_admin"},
        )
        assert response.status_code == 403

    def test_admin_cannot_grant_platform_roles(self, client, workspace):
        response = client.post(
            f"{API}/users", headers=workspace["admin"],
            json={"name": "X", "email": "escalate@example.com", "roleCode": "super_admin"},
        )
        assert response.status_code == 403

    def test_admin_can_only_add_tenant_user_members(self, client, workspace):
        # The team flow adds members as Tenant User only — a tenant admin
        # cannot mint another admin, at creation or via a later promotion.
        create = client.post(
            f"{API}/users", headers=workspace["admin"],
            json={"name": "X", "email": "peer@example.com", "roleCode": "tenant_admin"},
        )
        assert create.status_code == 403
        promote = client.patch(
            f"{API}/users/{workspace['member_id']}", headers=workspace["admin"],
            json={"roleCode": "tenant_admin"},
        )
        assert promote.status_code == 403

    def test_direct_create_with_password_can_log_in(self, client, workspace):
        """Create User flow: no invite email — the account is active at once,
        belongs to the admin's tenant and carries the seeded tenant_user RBAC."""
        email = f"direct.{uuid.uuid4().hex[:8]}@example.com"
        password = "Direct2026pass"
        created = client.post(
            f"{API}/users", headers=workspace["admin"],
            json={"name": "Direct Member", "email": email,
                  "roleCode": "tenant_user", "password": password},
        )
        assert created.status_code == 201, created.text
        data = created.json()["data"]
        assert data["status"] == "active"
        assert "temporaryPassword" not in data

        login = client.post(f"{API}/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        payload = login.json()["data"]["user"]
        assert payload["role"] == "tenant_user"
        assert payload["tenantId"] == workspace["tenant_id"]
        # RBAC comes from the seeded role — not duplicated at creation time.
        assert "manage_prompts" in payload["permissions"]
        assert "costs.view" not in payload["permissions"]

        session = get_sessionmaker()()
        try:
            row = session.execute(select(User).where(User.email == email)).scalar_one()
            session.delete(row)
            session.commit()
        finally:
            session.close()

    def test_duplicate_email_is_rejected_not_duplicated(self, client, workspace):
        email = f"dup.{uuid.uuid4().hex[:8]}@example.com"
        first = client.post(
            f"{API}/users", headers=workspace["admin"],
            json={"name": "Dup", "email": email, "roleCode": "tenant_user"},
        )
        assert first.status_code == 201, first.text
        again = client.post(
            f"{API}/users", headers=workspace["admin"],
            json={"name": "Dup Two", "email": email, "roleCode": "tenant_user",
                  "password": "Another2026pw"},
        )
        assert again.status_code == 409
        session = get_sessionmaker()()
        try:
            rows = session.scalars(select(User).where(User.email == email)).all()
            assert len(rows) == 1
            for row in rows:
                session.delete(row)
            session.commit()
        finally:
            session.close()


class TestRolesListingScope:
    def test_tenant_caller_sees_only_tenant_roles_with_own_counts(self, client, workspace):
        response = client.get(f"{API}/roles", headers=workspace["admin"])
        assert response.status_code == 200
        roles = response.json()["data"]
        assert all(r["scope"] == "tenant" for r in roles)
        assert "super_admin" not in {r["code"] for r in roles}
        # Member counts cover ONLY this tenant — the fixture has exactly one
        # admin and one tenant user; platform-wide counts would be far higher.
        by_code = {r["code"]: r["members"] for r in roles}
        session = get_sessionmaker()()
        try:
            expected = session.execute(
                select(User.role_id, ).where(
                    User.tenant_id == workspace["tenant_id"], User.is_deleted.is_(False))
            ).all()
        finally:
            session.close()
        assert by_code["tenant_admin"] + by_code["tenant_user"] == len(expected)
        assert by_code["tenant_admin"] == 1
        assert by_code["tenant_user"] == 1

    def test_super_admin_keeps_full_catalog(self, client, workspace):
        session = get_sessionmaker()()
        try:
            root = session.scalars(
                select(User).where(User.is_deleted.is_(False))
            ).all()
            root = next(u for u in root if u.role.code == "super_admin")
            token = create_access_token(user_id=root.id, role="super_admin", tenant_id=None)
        finally:
            session.close()
        response = client.get(
            f"{API}/roles", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        codes = {r["code"] for r in response.json()["data"]}
        assert {"super_admin", "tenant_admin", "tenant_user"} <= codes


# ── Shared tenant resources: tenant_user edits, admin sees the same record ───


class TestSharedResourceEditing:
    def test_workflow_edit_is_visible_to_admin(self, client, workspace):
        bot_id = workspace["bot_id"]
        response = client.put(
            f"{API}/bots/{bot_id}/workflow", headers=workspace["member"],
            json={"name": "Edited by tenant user", "nodes": [], "edges": []},
        )
        assert response.status_code == 200, response.text
        seen = client.get(f"{API}/bots/{bot_id}/workflow", headers=workspace["admin"])
        assert seen.status_code == 200
        assert seen.json()["data"]["name"] == "Edited by tenant user"

    def test_knowledge_create_is_visible_to_admin(self, client, workspace):
        name = f"TU Shared KB {uuid.uuid4().hex[:6]}"
        response = client.post(
            f"{API}/knowledge", headers=workspace["member"],
            json={"name": name, "type": "faq", "scope": "tenant"},
        )
        assert response.status_code == 200 or response.status_code == 201, response.text
        listed = client.get(f"{API}/knowledge", headers=workspace["admin"])
        assert listed.status_code == 200
        assert name in [k["name"] for k in listed.json()["data"]]

    def test_voice_settings_editable_by_tenant_user(self, client, workspace):
        response = client.put(
            f"{API}/bots/{workspace['bot_id']}/voice-settings",
            headers=workspace["member"], json={"speed": 1.2},
        )
        assert response.status_code == 200, response.text
        seen = client.get(
            f"{API}/bots/{workspace['bot_id']}/voice-settings",
            headers=workspace["admin"],
        )
        assert seen.status_code == 200
        assert seen.json()["data"]["speed"] == 1.2

    def test_test_scenarios_creatable_by_tenant_user(self, client, workspace):
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/scenarios",
            headers=workspace["member"], json={"name": "TU regression check"},
        )
        assert response.status_code == 201, response.text


# ── Blocked sections ─────────────────────────────────────────────────────────


BLOCKED_GETS = [
    "/integrations",
    "/tenant/settings",
    "/voice-clones",
    "/voice-clones/config",
    "/usage/summary",
    "/usage/sessions/vs_does_not_exist",
    "/reports/ai_cost/export?format=csv",
    "/api-connections",
    # Team data (member roster, role catalog) is for team managers only.
    "/users?scope=tenant",
    "/roles",
    "/permissions",
    # Exchange rates power cost views — pricing-adjacent, costs.view only.
    "/currency/rates",
]


class TestBlockedSections:
    @pytest.mark.parametrize("path", BLOCKED_GETS)
    def test_tenant_user_gets_403(self, client, workspace, path):
        response = client.get(f"{API}{path}", headers=workspace["member"])
        assert response.status_code == 403, f"{path}: {response.status_code}"

    @pytest.mark.parametrize(
        "path",
        ["/integrations", "/tenant/settings", "/voice-clones", "/usage/summary",
         "/users?scope=tenant", "/roles", "/currency/rates"],
    )
    def test_tenant_admin_still_allowed(self, client, workspace, path):
        response = client.get(f"{API}{path}", headers=workspace["admin"])
        assert response.status_code == 200, f"{path}: {response.text[:200]}"

    def test_bot_channels_blocked_for_tenant_user(self, client, workspace):
        bot_id = workspace["bot_id"]
        assert client.get(
            f"{API}/bots/{bot_id}/channels", headers=workspace["member"]
        ).status_code == 403
        assert client.put(
            f"{API}/bots/{bot_id}/channels/whatsapp", headers=workspace["member"],
            json={"enabled": True},
        ).status_code == 403
        assert client.get(
            f"{API}/bots/{bot_id}/channels", headers=workspace["admin"]
        ).status_code == 200

    def test_bot_releases_and_intents_blocked_for_tenant_user(self, client, workspace):
        bot_id = workspace["bot_id"]
        assert client.get(
            f"{API}/bots/{bot_id}/releases", headers=workspace["member"]
        ).status_code == 403
        assert client.get(
            f"{API}/bots/{bot_id}/intents", headers=workspace["member"]
        ).status_code == 403
        assert client.get(
            f"{API}/bots/{bot_id}/releases", headers=workspace["admin"]
        ).status_code == 200

    def test_voice_clone_creation_blocked_for_tenant_user(self, client, workspace):
        response = client.post(f"{API}/voice-clones", headers=workspace["member"])
        assert response.status_code == 403

    def test_tenant_settings_write_blocked_for_tenant_user(self, client, workspace):
        response = client.put(
            f"{API}/tenant/settings", headers=workspace["member"], json={},
        )
        assert response.status_code == 403


# ── Financial data is stripped, not just hidden ──────────────────────────────


class TestCostVisibility:
    def test_analytics_has_no_cost_kpis_for_tenant_user(self, client, workspace):
        response = client.get(f"{API}/analytics/tenant?days=30", headers=workspace["member"])
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        labels = [k["label"] for k in data["kpis"]]
        assert "AI cost" not in labels
        assert "Avg cost / call" not in labels
        assert data["costSeries"] == []

    def test_analytics_keeps_cost_kpis_for_admin(self, client, workspace):
        response = client.get(f"{API}/analytics/tenant?days=30", headers=workspace["admin"])
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        labels = [k["label"] for k in data["kpis"]]
        assert "AI cost" in labels and "Avg cost / call" in labels
        assert len(data["costSeries"]) == 30

    def test_conversation_costs_nulled_for_tenant_user(self, client, workspace):
        listed = client.get(f"{API}/conversations", headers=workspace["member"])
        assert listed.status_code == 200
        row = next(r for r in listed.json()["data"]
                   if r["id"] == workspace["conversation_id"])
        assert row["costUsd"] is None
        assert row["costPerMinuteUsd"] is None

        detail = client.get(
            f"{API}/conversations/{workspace['conversation_id']}",
            headers=workspace["member"],
        )
        assert detail.status_code == 200
        assert detail.json()["data"]["cost"] is None
        assert detail.json()["data"]["costUsd"] is None

    def test_conversation_costs_present_for_admin(self, client, workspace):
        listed = client.get(f"{API}/conversations", headers=workspace["admin"])
        row = next(r for r in listed.json()["data"]
                   if r["id"] == workspace["conversation_id"])
        assert row["costUsd"] == pytest.approx(1.23)

    def test_bot_cost_per_call_nulled_for_tenant_user(self, client, workspace):
        member_bot = client.get(
            f"{API}/bots/{workspace['bot_id']}", headers=workspace["member"])
        assert member_bot.status_code == 200
        assert member_bot.json()["data"]["avgCostPerCall"] is None
        admin_bot = client.get(
            f"{API}/bots/{workspace['bot_id']}", headers=workspace["admin"])
        assert admin_bot.json()["data"]["avgCostPerCall"] is not None

    def test_conversations_export_has_no_cost_column_for_tenant_user(self, client, workspace):
        response = client.get(
            f"{API}/exports/conversations?format=csv", headers=workspace["member"])
        assert response.status_code == 200, response.text
        header = response.text.splitlines()[0]
        assert "Cost (USD)" not in header
        admin_response = client.get(
            f"{API}/exports/conversations?format=csv", headers=workspace["admin"])
        assert "Cost (USD)" in admin_response.text.splitlines()[0]

    def test_transcript_export_has_no_cost_column_for_tenant_user(self, client, workspace):
        path = f"{API}/conversations/{workspace['conversation_id']}/transcript/export?format=csv"
        response = client.get(path, headers=workspace["member"])
        assert response.status_code == 200, response.text
        assert "Cost (USD)" not in response.text.splitlines()[0]
        admin_response = client.get(path, headers=workspace["admin"])
        assert "Cost (USD)" in admin_response.text.splitlines()[0]

    def test_usage_report_still_exportable_and_costless(self, client, workspace):
        response = client.get(
            f"{API}/reports/usage/export?format=csv&days=30", headers=workspace["member"])
        assert response.status_code == 200, response.text
        assert "Cost" not in response.text.splitlines()[0]


# ── Tenant isolation ─────────────────────────────────────────────────────────


class TestTenantIsolation:
    def test_tenant_user_cannot_see_foreign_bot(self, client, workspace):
        response = client.get(
            f"{API}/bots/{workspace['foreign_bot_id']}", headers=workspace["member"])
        assert response.status_code == 404

    def test_tenant_user_cannot_request_foreign_tenant_listing(self, client, workspace):
        response = client.get(
            f"{API}/conversations?tenantId={workspace['other_tenant_id']}",
            headers=workspace["member"],
        )
        assert response.status_code == 403
