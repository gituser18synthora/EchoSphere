"""Master data, tenant/user profiles, password change, prompts lifecycle,
intents/entities CRUD + testing, API connections (SSRF guard) and languages.

Runs against the live app + local databases (same harness as the other
integration suites). All created records use unique suffixes and are removed
in the module teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_created: list[tuple[str, str]] = []  # (table, id) — cleaned up at teardown


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    # Best-effort hard cleanup of rows this module created (children first).
    from sqlalchemy import text as sa_text

    from backend.db.mysql import get_engine

    with get_engine().begin() as conn:
        for table, row_id in reversed(_created):
            if table == "prompts":
                conn.execute(sa_text("DELETE FROM prompt_versions WHERE prompt_id = :id"),
                             {"id": row_id})
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})


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
def super_admin():
    return bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def tenant_admin():
    return bearer("priya.sharma@meridianhealth.com")  # tenant admin of tn-001


@pytest.fixture(scope="module")
def tenant_user():
    return bearer("sam.ellery@meridianhealth.com")  # tenant_user of tn-001


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


# ── Master data ───────────────────────────────────────────────────────────────


class TestMasterData:
    def test_industry_crud_and_onboarding_visibility(self, client, super_admin):
        code = f"ind_{_SUFFIX}"
        created = _data(client.post(
            f"{API}/master/industries", headers=super_admin,
            json={"code": code, "name": f"Test Industry {_SUFFIX}", "description": "x"},
        ))
        _created.append(("industries", created["id"]))
        assert created["code"] == code and created["status"] == "active"
        assert created["createdBy"]  # audit fields populated

        # Appears in onboarding options while active
        options = _data(client.get(f"{API}/onboarding/options", headers=super_admin))
        assert any(i["code"] == code for i in options["industries"])

        # Edit
        updated = _data(client.patch(
            f"{API}/master/industries/{created['id']}", headers=super_admin,
            json={"name": "Renamed Industry"},
        ))
        assert updated["name"] == "Renamed Industry"

        # Deactivate → hidden from onboarding for NEW tenants
        _data(client.post(
            f"{API}/master/industries/{created['id']}/status", headers=super_admin,
            json={"status": "inactive"},
        ))
        options = _data(client.get(f"{API}/onboarding/options", headers=super_admin))
        assert not any(i["code"] == code for i in options["industries"])

        # Inactive industry rejected for new tenants
        response = client.post(f"{API}/tenants", headers=super_admin, json={
            "name": "X", "domain": f"x-{_SUFFIX}.example", "industry": code,
            "adminEmail": f"x-{_SUFFIX}@example.com",
        })
        assert response.status_code == 422

        # Audit trail recorded
        audit = _data(client.get(
            f"{API}/master/industries/{created['id']}/audit", headers=super_admin))
        assert any("Created" in e["action"] for e in audit)
        assert any("Deactivated" in e["action"] for e in audit)

    def test_referenced_industry_cannot_be_deleted(self, client, super_admin):
        code = f"ind_ref_{_SUFFIX}"
        created = _data(client.post(
            f"{API}/master/industries", headers=super_admin,
            json={"code": code, "name": f"Referenced {_SUFFIX}"},
        ))
        _created.append(("industries", created["id"]))
        original = _data(client.get(f"{API}/tenants/tn-001", headers=super_admin))["industry"]
        try:
            _data(client.patch(f"{API}/tenants/tn-001", headers=super_admin,
                               json={"industry": code}))
            response = client.delete(
                f"{API}/master/industries/{created['id']}", headers=super_admin)
            assert response.status_code == 409
            message = response.json()["message"]
            assert "cannot be deleted" in message.lower() or "used by" in message.lower()
            assert "foreign key" not in message.lower()
        finally:
            client.patch(f"{API}/tenants/tn-001", headers=super_admin,
                         json={"industry": original or "healthcare"})

    def test_data_region_plan_ai_profile_create(self, client, super_admin):
        region = _data(client.post(f"{API}/master/data-regions", headers=super_admin, json={
            "code": f"dr_{_SUFFIX}", "name": "Test Region", "country": "India",
        }))
        _created.append(("data_regions", region["id"]))
        assert region["infrastructureReady"] is False  # never claims deployment

        plan = _data(client.post(f"{API}/master/plans", headers=super_admin, json={
            "code": f"pl_{_SUFFIX}", "name": "Test Plan", "priceMonthly": 99,
            "botLimit": 3, "minutesIncluded": 500,
        }))
        _created.append(("plans", plan["id"]))
        assert plan["priceMonthly"] == 99

        profile = _data(client.post(f"{API}/master/ai-profiles", headers=super_admin, json={
            "code": f"aip_{_SUFFIX}", "name": "Test Profile", "llmProvider": "mock",
            "llmModel": "mock-1", "costCategory": "low",
        }))
        _created.append(("ai_config_profiles", profile["id"]))
        assert profile["llmProvider"] == "mock"

        options = _data(client.get(f"{API}/onboarding/options", headers=super_admin))
        assert any(p["code"] == plan["code"] for p in options["plans"])
        assert any(a["code"] == profile["code"] for a in options["aiProfiles"])
        assert any(r["code"] == region["code"] for r in options["dataRegions"])

    def test_plan_duplicate_and_tenants(self, client, super_admin):
        listing = _data(client.get(f"{API}/master/plans?search=starter", headers=super_admin))
        starter = next(p for p in listing if p["code"] == "starter")
        clone = _data(client.post(
            f"{API}/master/plans/{starter['id']}/duplicate", headers=super_admin))
        _created.append(("plans", clone["id"]))
        assert clone["status"] == "inactive" and clone["code"].startswith("starter_copy")
        tenants = _data(client.get(
            f"{API}/master/plans/{starter['id']}/tenants", headers=super_admin))
        assert isinstance(tenants, list)

    def test_master_data_rbac(self, client, tenant_admin, tenant_user):
        for headers in (tenant_admin, tenant_user):
            response = client.post(f"{API}/master/industries", headers=headers,
                                   json={"code": "nope", "name": "Nope"})
            assert response.status_code == 403
        assert client.get(f"{API}/master/industries").status_code == 401


# ── Languages ─────────────────────────────────────────────────────────────────


class TestLanguages:
    def test_indian_languages_seeded_with_metadata(self, client, tenant_admin):
        languages = _data(client.get(f"{API}/languages", headers=tenant_admin))
        by_code = {l["code"]: l for l in languages}
        for code in ("en-IN", "hi-IN", "bn-IN", "ta-IN", "te-IN", "ml-IN", "pa-IN",
                     "ur-IN", "sa-IN", "sat-IN", "ne-IN", "sd-IN"):
            assert code in by_code, f"{code} missing"
        assert by_code["ur-IN"]["direction"] == "rtl"
        assert by_code["sd-IN"]["direction"] == "rtl"
        assert by_code["hi-IN"]["script"] == "Devanagari"
        # Platform listing ≠ provider support: minor languages carry empty STT lists
        assert by_code["sa-IN"]["providerSupport"]["stt"] == []
        assert "sarvam" in by_code["hi-IN"]["providerSupport"]["stt"]

    def test_language_deactivate_hides_from_catalog(self, client, super_admin, tenant_admin):
        created = _data(client.post(f"{API}/master/languages", headers=super_admin, json={
            "code": f"x{_SUFFIX[:4]}-XX", "name": "Testish", "direction": "ltr",
        }))
        _created.append(("supported_languages", created["id"]))
        _data(client.post(f"{API}/master/languages/{created['id']}/status",
                          headers=super_admin, json={"status": "inactive"}))
        codes = [l["code"] for l in _data(client.get(f"{API}/languages", headers=tenant_admin))]
        assert created["code"] not in codes


# ── Tenant profile ────────────────────────────────────────────────────────────


class TestTenantProfile:
    def test_tenant_admin_updates_allowed_fields(self, client, tenant_admin):
        profile = _data(client.put(f"{API}/tenant/profile", headers=tenant_admin, json={
            "website": "https://meridian.example",
            "contactName": "Priya Sharma",
            "contactEmail": "contact@meridianhealth.com",
            "supportEmail": "support@meridianhealth.com",
            "timezone": "Asia/Kolkata",
        }))
        assert profile["website"] == "https://meridian.example"
        assert profile["supportEmail"] == "support@meridianhealth.com"
        assert profile["timezone"] == "Asia/Kolkata"

    def test_restricted_fields_rejected_in_payload(self, client, tenant_admin):
        # extra="forbid": smuggling plan/status/code into the payload is a 422.
        for payload in ({"plan": "enterprise"}, {"status": "active"},
                        {"code": "hax"}, {"dataRegion": "eu"}):
            response = client.put(f"{API}/tenant/profile", headers=tenant_admin, json=payload)
            assert response.status_code == 422, payload

    def test_cross_tenant_profile_blocked(self, client, tenant_admin):
        response = client.get(f"{API}/tenant/profile?tenantId=tn-002", headers=tenant_admin)
        assert response.status_code == 403

    def test_tenant_user_cannot_edit(self, client, tenant_user):
        response = client.put(f"{API}/tenant/profile", headers=tenant_user,
                              json={"website": "https://nope.example"})
        assert response.status_code == 403
        # but may view
        assert client.get(f"{API}/tenant/profile", headers=tenant_user).status_code == 200

    def test_super_admin_changes_plan_and_region(self, client, super_admin):
        before = _data(client.get(f"{API}/tenants/tn-001", headers=super_admin))
        updated = _data(client.patch(f"{API}/tenants/tn-001", headers=super_admin,
                                     json={"region": "in-mumbai"}))
        assert updated["region"] == "in-mumbai"
        client.patch(f"{API}/tenants/tn-001", headers=super_admin,
                     json={"region": before["region"] or "in"})

    def test_audit_written_for_profile_update(self, client, tenant_admin):
        from sqlalchemy import select

        from backend.db.mysql import get_sessionmaker
        from backend.models import AuditLog

        session = get_sessionmaker()()
        try:
            row = session.execute(
                select(AuditLog).where(AuditLog.action == "Updated tenant profile")
                .order_by(AuditLog.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            assert row is not None
        finally:
            session.close()


# ── User profile & password ───────────────────────────────────────────────────


class TestPasswordChange:
    @pytest.fixture(scope="class")
    def temp_user(self, client, super_admin):
        email = f"pwtest-{_SUFFIX}@example.com"
        created = _data(client.post(f"{API}/users", headers=super_admin, json={
            "name": "PW Test", "email": email, "roleCode": "tenant_user",
            "tenantId": "tn-001", "password": "Original@2026x",
        }))
        _created.append(("users", created["id"]))
        return {"id": created["id"], "email": email, "password": "Original@2026x"}

    def _login(self, client, email, password):
        return client.post(f"{API}/auth/login", json={"email": email, "password": password})

    def test_profile_update(self, client, temp_user):
        token = self._login(client, temp_user["email"], temp_user["password"]).json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        me = _data(client.patch(f"{API}/users/me", headers=headers, json={
            "firstName": "Pat", "lastName": "Tester", "phone": "+911234567890",
            "locale": "hi-IN", "timezone": "Asia/Kolkata",
        }))
        assert me["name"] == "Pat Tester" and me["phone"] == "+911234567890"

    def test_wrong_current_password(self, client, temp_user):
        token = self._login(client, temp_user["email"], temp_user["password"]).json()["data"]["token"]
        response = client.post(f"{API}/users/me/password",
                               headers={"Authorization": f"Bearer {token}"},
                               json={"currentPassword": "Wrong@2026x",
                                     "newPassword": "Fresh@2026pw1",
                                     "confirmPassword": "Fresh@2026pw1"})
        assert response.status_code == 400

    def test_weak_same_and_mismatch_rejected(self, client, temp_user):
        token = self._login(client, temp_user["email"], temp_user["password"]).json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}
        current = temp_user["password"]
        cases = [
            ("short1A", "short1A", 422),                # too short
            ("password123", "password123", 422),        # weak/common (no upper anyway)
            ("Password12345", "Password99999", 422),    # mismatch
            (current, current, 422),                    # same as current
        ]
        for new, confirm, expected in cases:
            response = client.post(f"{API}/users/me/password", headers=headers, json={
                "currentPassword": current, "newPassword": new, "confirmPassword": confirm,
            })
            assert response.status_code == expected, (new, response.json())

    def test_successful_change_rotates_sessions(self, client, temp_user):
        from datetime import datetime, timedelta, timezone

        import jwt as pyjwt

        from backend.config import get_settings

        # A pre-existing "other session" token, issued 60s in the past.
        settings = get_settings()
        old_token = pyjwt.encode(
            {"sub": temp_user["id"], "role": "tenant_user", "tenant_id": "tn-001",
             "iat": datetime.now(timezone.utc) - timedelta(seconds=60),
             "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
            settings.jwt_secret, algorithm=settings.jwt_algorithm,
        )
        login_token = self._login(client, temp_user["email"], temp_user["password"]).json()["data"]["token"]
        result = _data(client.post(f"{API}/users/me/password",
                                   headers={"Authorization": f"Bearer {login_token}"},
                                   json={"currentPassword": temp_user["password"],
                                         "newPassword": "Rotated@2026pw",
                                         "confirmPassword": "Rotated@2026pw"}))
        assert result["changed"] is True and result["token"]

        # Old password stops working; new one works.
        assert self._login(client, temp_user["email"], temp_user["password"]).status_code == 401
        assert self._login(client, temp_user["email"], "Rotated@2026pw").status_code == 200
        temp_user["password"] = "Rotated@2026pw"

        # The pre-change "other session" token is now rejected...
        response = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {old_token}"})
        assert response.status_code == 401
        # ...while the fresh token returned by the change keeps this session alive.
        response = client.get(f"{API}/auth/me",
                              headers={"Authorization": f"Bearer {result['token']}"})
        assert response.status_code == 200

    def test_audit_contains_no_password(self, client):
        from sqlalchemy import select

        from backend.db.mysql import get_sessionmaker
        from backend.models import AuditLog

        session = get_sessionmaker()()
        try:
            row = session.execute(
                select(AuditLog).where(AuditLog.action == "Changed own password")
                .order_by(AuditLog.created_at.desc()).limit(1)
            ).scalar_one_or_none()
            assert row is not None
            blob = str(row.previous_value) + str(row.new_value)
            assert "Rotated@2026pw" not in blob and "Original@2026x" not in blob
        finally:
            session.close()

    def test_admin_reset_password(self, client, super_admin, temp_user, tenant_user):
        # tenant_user lacks reset_user_password
        response = client.post(f"{API}/users/{temp_user['id']}/reset-password",
                               headers=tenant_user)
        assert response.status_code == 403

        result = _data(client.post(f"{API}/users/{temp_user['id']}/reset-password",
                                   headers=super_admin))
        temp = result["temporaryPassword"]
        assert result["reset"] is True and len(temp) >= 12
        assert self._login(client, temp_user["email"], temp).status_code == 200
        temp_user["password"] = temp


# ── Knowledge base creation rules ─────────────────────────────────────────────


class TestKnowledgeCreation:
    def test_upload_config_exposed(self, client, tenant_admin):
        config = _data(client.get(f"{API}/knowledge/upload-config", headers=tenant_admin))
        assert "pdf" in config["allowedExtensions"] and config["maxFileMb"] >= 1
        assert ".pdf" in config["accept"]

    def test_kb_name_required_and_unique(self, client, tenant_admin):
        name = f"KB {_SUFFIX}"
        response = client.post(f"{API}/knowledge", headers=tenant_admin,
                               json={"name": "   ", "type": "document", "scope": "tenant"})
        assert response.status_code == 422

        created = _data(client.post(f"{API}/knowledge", headers=tenant_admin,
                                    json={"name": f"  {name}  ", "type": "document",
                                          "scope": "tenant"}))
        _created.append(("knowledge_sources", created["id"]))
        assert created["name"] == name  # trimmed
        assert created["status"] == "pending"  # not "indexing" before any upload

        duplicate = client.post(f"{API}/knowledge", headers=tenant_admin,
                                json={"name": name.lower(), "type": "document",
                                      "scope": "tenant"})
        assert duplicate.status_code == 409
        assert "already exists" in duplicate.json()["message"]

    def test_tenant_user_cannot_create_kb(self, client, tenant_user):
        response = client.post(f"{API}/knowledge", headers=tenant_user,
                               json={"name": "nope", "type": "document", "scope": "tenant"})
        assert response.status_code == 403


# ── Prompts: structured builder + lifecycle + test ────────────────────────────


class TestPrompts:
    CONFIG = {
        "identity": {"botName": "Meridian Assist", "organizationName": "Meridian Health",
                     "role": "patient support voice assistant", "allowedScope": "appointments and billing"},
        "conversationStart": {"initialGreeting": "Namaste! You've reached Meridian Health.",
                              "recordingConsent": "This call may be recorded.", "reasonForCall": True},
        "behavior": {"tone": "warm", "responseLength": "short", "confirmBeforeActions": True},
        "recovery": {"firstClarification": "Sorry, could you repeat that?",
                     "maxClarificationAttempts": 2, "handoffThreshold": 3},
        "handoff": {"onExplicitRequest": True, "onNegativeSentiment": True},
        "closing": {"askAnythingElse": True, "closingMessage": "Thank you for calling Meridian."},
        "special": {"silence": "Gently check if the caller is still there."},
    }

    @pytest.fixture(scope="class")
    def prompt(self, client, tenant_admin):
        created = _data(client.post(f"{API}/bots/bot-101/prompts", headers=tenant_admin, json={
            "type": "system", "name": f"Structured {_SUFFIX}",
            "structuredConfig": self.CONFIG,
        }))
        _created.append(("prompts", created["id"]))
        return created

    def test_compile_preview_validates(self, client, tenant_admin):
        result = _data(client.post(f"{API}/prompts/compile-preview", headers=tenant_admin,
                                   json={"structuredConfig": {"identity": {}}}))
        assert result["valid"] is False
        assert any(e["field"] == "identity.botName" for e in result["errors"])

        result = _data(client.post(f"{API}/prompts/compile-preview", headers=tenant_admin,
                                   json={"structuredConfig": self.CONFIG}))
        assert result["valid"] is True
        assert "Meridian Assist" in result["compiled"]
        assert "# Conversation start" in result["compiled"]
        assert "# Human handoff" in result["compiled"]
        assert result["tokenEstimate"] > 0

    def test_created_with_compiled_prompt(self, prompt):
        version = prompt["versions"][0]
        assert version["compiledPrompt"] and "Namaste" in version["compiledPrompt"]
        assert version["structuredConfig"]["identity"]["botName"] == "Meridian Assist"

    def test_compilation_is_deterministic(self, client, tenant_admin):
        first = _data(client.post(f"{API}/prompts/compile-preview", headers=tenant_admin,
                                  json={"structuredConfig": self.CONFIG}))
        second = _data(client.post(f"{API}/prompts/compile-preview", headers=tenant_admin,
                                   json={"structuredConfig": self.CONFIG}))
        assert first["compiled"] == second["compiled"]

    def test_lifecycle_draft_approve_publish_rollback(self, client, tenant_admin, prompt):
        pid = prompt["id"]
        # new draft version
        v2 = _data(client.post(f"{API}/prompts/{pid}/versions", headers=tenant_admin, json={
            "note": "v2", "structuredConfig": self.CONFIG, "submitForApproval": True,
        }))
        assert v2["state"] == "pending_approval" and v2["activeVersion"] == 2
        approved = _data(client.patch(f"{API}/prompts/{pid}", headers=tenant_admin,
                                      json={"state": "approved"}))
        assert approved["state"] == "approved" and approved["approvedBy"]
        published = _data(client.patch(f"{API}/prompts/{pid}", headers=tenant_admin,
                                       json={"state": "published"}))
        assert published["publishedVersion"] == 2 and published["publishedAt"]
        rolled = _data(client.patch(f"{API}/prompts/{pid}", headers=tenant_admin,
                                    json={"activeVersion": 1}))
        assert rolled["publishedVersion"] == 1

    def test_tenant_user_cannot_approve(self, client, tenant_user, prompt):
        response = client.patch(f"{API}/prompts/{prompt['id']}", headers=tenant_user,
                                json={"state": "approved"})
        assert response.status_code == 403

    def test_duplicate_prompt(self, client, tenant_admin, prompt):
        clone = _data(client.post(f"{API}/prompts/{prompt['id']}/duplicate",
                                  headers=tenant_admin))
        _created.append(("prompts", clone["id"]))
        assert clone["state"] == "draft" and clone["name"].endswith("(copy)")

    def test_prompt_test_runs_with_mock_llm(self, client, tenant_admin, prompt):
        result = _data(client.post(f"{API}/prompts/{prompt['id']}/test", headers=tenant_admin,
                                   json={"message": "What are your working hours?",
                                         "language": "en-IN"}))
        assert result["error"] is None, result
        assert result["provider"] == "mock"
        assert result["response"]
        assert result["latencyMs"] >= 0
        assert result["route"] in ("knowledge", "chat", "clarify", "intent")


# ── Intents & entities ────────────────────────────────────────────────────────


class TestEntities:
    @pytest.fixture(scope="class")
    def entity(self, client, tenant_admin):
        created = _data(client.post(f"{API}/entities", headers=tenant_admin, json={
            "name": f"policy_number_{_SUFFIX}", "kind": "regex", "dataType": "policy_number",
            "regexPattern": r"\bPOL[-/]?\d{6,10}\b", "pii": True,
            "example": "POL-1234567",
        }))
        _created.append(("entity_defs", created["id"]))
        return created

    def test_forbidden_secret_entities_rejected(self, client, tenant_admin):
        for name in ("cvv", "card PIN", "otp_code", "password_field"):
            response = client.post(f"{API}/entities", headers=tenant_admin,
                                   json={"name": name, "kind": "custom"})
            assert response.status_code == 422, name

    def test_invalid_regex_rejected(self, client, tenant_admin):
        response = client.post(f"{API}/entities", headers=tenant_admin, json={
            "name": f"badregex_{_SUFFIX}", "kind": "regex", "regexPattern": "([unclosed",
        })
        assert response.status_code == 422

    def test_extraction_masks_sensitive_values(self, client, tenant_admin, entity):
        result = _data(client.post(f"{API}/entities/{entity['id']}/test", headers=tenant_admin,
                                   json={"text": "My policy is POL-9876543 thanks"}))
        assert result["matched"] is True
        assert result["sensitive"] is True
        assert result["value"] is None            # raw value never returned for PII
        assert "9876543" not in (result["maskedValue"] or "") or result["maskedValue"].startswith("PO")
        assert "•" in result["maskedValue"]

    def test_allowed_values_and_synonyms(self, client, tenant_admin):
        created = _data(client.post(f"{API}/entities", headers=tenant_admin, json={
            "name": f"department_{_SUFFIX}", "kind": "custom", "dataType": "list",
            "allowedValues": ["cardiology", "orthopedics"],
            "synonyms": {"cardiology": ["heart department", "cardio"]},
        }))
        _created.append(("entity_defs", created["id"]))
        result = _data(client.post(f"{API}/entities/{created['id']}/test",
                                   headers=tenant_admin,
                                   json={"text": "connect me to the heart department"}))
        assert result["matched"] is True and result["value"] == "cardiology"

    def test_update_duplicate_delete(self, client, tenant_admin, entity):
        updated = _data(client.patch(f"{API}/entities/{entity['id']}", headers=tenant_admin,
                                     json={"description": "Policy id", "status": "active"}))
        assert updated["description"] == "Policy id"
        clone = _data(client.post(f"{API}/entities/{entity['id']}/duplicate",
                                  headers=tenant_admin))
        _created.append(("entity_defs", clone["id"]))
        assert clone["name"].endswith("(copy)")
        deleted = _data(client.delete(f"{API}/entities/{clone['id']}", headers=tenant_admin))
        assert deleted["archived"] is True

    def test_tenant_user_cannot_edit(self, client, tenant_user, entity):
        response = client.patch(f"{API}/entities/{entity['id']}", headers=tenant_user,
                                json={"description": "hax"})
        assert response.status_code == 403


class TestIntents:
    @pytest.fixture(scope="class")
    def entity(self, client, tenant_admin):
        created = _data(client.post(f"{API}/entities", headers=tenant_admin, json={
            "name": f"claim_id_{_SUFFIX}", "kind": "regex", "dataType": "claim_number",
            "regexPattern": r"\bCLM[-/]?\d{6,10}\b",
        }))
        _created.append(("entity_defs", created["id"]))
        return created

    @pytest.fixture(scope="class")
    def intent(self, client, tenant_admin, entity):
        created = _data(client.post(f"{API}/bots/bot-101/intents", headers=tenant_admin, json={
            "name": f"Claim Status {_SUFFIX}",
            "description": "Caller asks about claim status",
            "samples": ["what is the status of my claim",
                        "check my claim status",
                        "has my claim been approved"],
            "entities": [entity["name"]],
            "confidenceThreshold": 0.3,
            "priority": 10,
            "fallbackBehavior": "clarify",
        }))
        _created.append(("intents", created["id"]))
        return created

    def test_create_sets_code_and_status(self, intent):
        assert intent["code"].startswith("claim_status")
        assert intent["status"] == "active"

    def test_duplicate_phrases_rejected(self, client, tenant_admin):
        response = client.post(f"{API}/bots/bot-101/intents", headers=tenant_admin, json={
            "name": f"Dup {_SUFFIX}",
            "samples": ["check my balance", "Check  my   BALANCE"],
        })
        assert response.status_code == 422
        assert "duplicate" in response.json()["message"].lower()

    def test_unknown_entity_reference_rejected(self, client, tenant_admin):
        response = client.post(f"{API}/bots/bot-101/intents", headers=tenant_admin, json={
            "name": f"BadRef {_SUFFIX}", "samples": ["a", "b", "c"],
            "entities": ["does_not_exist_xyz"],
        })
        assert response.status_code == 422

    def test_runtime_intent_match_with_extraction(self, client, tenant_admin, intent, entity):
        result = _data(client.post(f"{API}/bots/bot-101/intents/test", headers=tenant_admin,
                                   json={"utterance": "please check my claim status for CLM-778899"}))
        assert result["matchedIntent"] == intent["name"]
        assert result["confidence"] >= 0.3
        extracted = {e["name"]: e for e in result["entities"]}
        assert extracted[entity["name"]]["matched"] is True

    def test_update_duplicate_archive(self, client, tenant_admin, intent):
        updated = _data(client.patch(f"{API}/intents/{intent['id']}", headers=tenant_admin,
                                     json={"priority": 5, "handoffEnabled": True}))
        assert updated["priority"] == 5 and updated["handoffEnabled"] is True
        clone = _data(client.post(f"{API}/intents/{intent['id']}/duplicate",
                                  headers=tenant_admin))
        _created.append(("intents", clone["id"]))
        assert clone["status"] == "disabled"
        _data(client.delete(f"{API}/intents/{clone['id']}", headers=tenant_admin))

    def test_cross_tenant_intent_access_404(self, client, super_admin, intent):
        # Create a throwaway tenant admin in ANOTHER tenant (tn-002).
        email = f"other-admin-{_SUFFIX}@example.com"
        created = _data(client.post(f"{API}/users", headers=super_admin, json={
            "name": "Other Admin", "email": email, "roleCode": "tenant_admin",
            "tenantId": "tn-002", "password": "Other@2026pass",
        }))
        _created.append(("users", created["id"]))
        other = bearer(email)
        response = client.patch(f"{API}/intents/{intent['id']}", headers=other,
                                json={"priority": 1})
        # 404, not 403 — other tenants' records must not leak their existence.
        assert response.status_code == 404


# ── API connections ───────────────────────────────────────────────────────────


class TestApiConnections:
    def test_unknown_variable_rejected(self, client, tenant_admin):
        response = client.post(f"{API}/api-connections", headers=tenant_admin, json={
            "name": f"BadVar {_SUFFIX}", "url": "https://api.example.com/{{not_a_var}}",
        })
        assert response.status_code == 422
        assert "variable" in response.json()["message"].lower()

    def test_raw_secret_rejected(self, client, tenant_admin):
        response = client.post(f"{API}/api-connections", headers=tenant_admin, json={
            "name": f"RawSecret {_SUFFIX}", "url": "https://api.example.com/x",
            "authType": "bearer", "secretRef": "sk-live-abc123",
        })
        assert response.status_code == 422

    @pytest.fixture(scope="class")
    def connection(self, client, tenant_admin):
        created = _data(client.post(f"{API}/api-connections", headers=tenant_admin, json={
            "name": f"CRM {_SUFFIX}", "method": "GET",
            "url": "https://api.example.com/customers/{{customer_phone}}",
            "headers": {"X-Tenant": "{{tenant_id}}"},
            "queryParams": {"claim": "{{entities.claim_id}}"},
            "timeoutMs": 3000, "successCondition": "status < 400",
            "botId": "bot-101",
        }))
        _created.append(("api_connections", created["id"]))
        return created

    def test_ssrf_private_target_blocked(self, client, tenant_admin):
        created = _data(client.post(f"{API}/api-connections", headers=tenant_admin, json={
            "name": f"SSRF {_SUFFIX}", "url": "http://169.254.169.254/latest/meta-data/",
        }))
        _created.append(("api_connections", created["id"]))
        result = _data(client.post(f"{API}/api-connections/{created['id']}/test",
                                   headers=tenant_admin, json={}))
        assert result["ok"] is False
        assert "private or internal" in (result["error"] or "")

        created2 = _data(client.post(f"{API}/api-connections", headers=tenant_admin, json={
            "name": f"SSRF2 {_SUFFIX}", "url": "http://localhost:8000/api/health",
        }))
        _created.append(("api_connections", created2["id"]))
        result2 = _data(client.post(f"{API}/api-connections/{created2['id']}/test",
                                    headers=tenant_admin, json={}))
        assert result2["ok"] is False

    def test_local_test_server_with_allowlist(self, client, tenant_admin, monkeypatch):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b'{"ok": true, "customer": "found"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from backend.config import get_settings

            real = get_settings()
            monkeypatch.setattr(real, "api_connect_allowed_hosts", "127.0.0.1")

            created = _data(client.post(f"{API}/api-connections", headers=tenant_admin, json={
                "name": f"Local {_SUFFIX}", "method": "GET",
                "url": f"http://127.0.0.1:{port}/customers",
                "headers": {"Authorization": "{{tenant_id}}", "X-Plain": "1"},
                "sensitiveMasks": ["Authorization"],
                "successCondition": "status < 400",
            }))
            _created.append(("api_connections", created["id"]))
            result = _data(client.post(f"{API}/api-connections/{created['id']}/test",
                                       headers=tenant_admin, json={}))
            assert result["ok"] is True
            assert result["status"] == 200
            assert '"customer"' in result["body"]
            # Sensitive headers are masked in the echo, plain ones are not.
            assert result["headersSent"]["Authorization"] == "•••"
            assert result["headersSent"]["X-Plain"] == "1"
        finally:
            server.shutdown()

    def test_associations_validated_and_stored(self, client, tenant_admin, connection):
        response = client.patch(f"{API}/api-connections/{connection['id']}",
                                headers=tenant_admin,
                                json={"allowedIntents": ["in_does_not_exist"]})
        assert response.status_code == 422

        intents = _data(client.get(f"{API}/bots/bot-101/intents", headers=tenant_admin))
        if intents:
            updated = _data(client.patch(f"{API}/api-connections/{connection['id']}",
                                         headers=tenant_admin,
                                         json={"allowedIntents": [intents[0]["id"]]}))
            assert updated["allowedIntents"] == [intents[0]["id"]]

    def test_duplicate_and_archive(self, client, tenant_admin, connection):
        clone = _data(client.post(f"{API}/api-connections/{connection['id']}/duplicate",
                                  headers=tenant_admin))
        _created.append(("api_connections", clone["id"]))
        assert clone["status"] == "untested"
        _data(client.delete(f"{API}/api-connections/{clone['id']}", headers=tenant_admin))

    def test_tenant_user_cannot_manage(self, client, tenant_user, connection):
        response = client.patch(f"{API}/api-connections/{connection['id']}",
                                headers=tenant_user, json={"name": "hax"})
        assert response.status_code == 403


# ── Voices master ─────────────────────────────────────────────────────────────


class TestVoicesMaster:
    def test_create_and_single_default(self, client, super_admin):
        created = _data(client.post(f"{API}/master/voices", headers=super_admin, json={
            "name": f"Asha {_SUFFIX}", "gender": "female", "provider": "platform",
            "locale": "hi-IN", "languages": ["hi-IN", "en-IN"], "speakingRate": 1.1,
        }))
        _created.append(("voice_profiles", created["id"]))
        assert created["speakingRate"] == 1.1

        promoted = _data(client.patch(f"{API}/master/voices/{created['id']}",
                                      headers=super_admin, json={"isDefault": True}))
        assert promoted["isDefault"] is True
        listing = _data(client.get(f"{API}/master/voices?pageSize=200", headers=super_admin))
        defaults = [v for v in listing if v["isDefault"]]
        assert len(defaults) == 1 and defaults[0]["id"] == created["id"]
        # Reset: no default voice.
        _data(client.patch(f"{API}/master/voices/{created['id']}",
                           headers=super_admin, json={"isDefault": False}))

    def test_voice_catalog_filters(self, client, tenant_admin):
        voices = _data(client.get(f"{API}/voices?language=hi-IN", headers=tenant_admin))
        assert all("hi-IN" in v["languages"] for v in voices)
