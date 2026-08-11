"""Industry-based guardrail profiles — API contract.

Covers the acceptance criteria end-to-end against the real dev MySQL:
onboarding suggestions per industry, explicit override persistence, tenant
create/update validation (active-only for NEW assignments, readable after
deactivation), industry changes never silently replacing a profile,
mandatory-guardrail protection, effective-guardrail resolution and
tenant-scoped trigger visibility. All rows created here carry unique
suffixes and are removed at teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import (
    Guardrail,
    GuardrailProfile,
    GuardrailTrigger,
    Role,
    Tenant,
    User,
)

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_created: list[tuple[str, str]] = []  # (table, id) — deleted children-first


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for table, row_id in reversed(_created):
            if table == "tenants":
                for child in ("subscriptions", "tenant_settings"):
                    conn.execute(
                        sa_text(f"DELETE FROM `{child}` WHERE tenant_id = :id"),
                        {"id": row_id},
                    )
                conn.execute(
                    sa_text("DELETE FROM users WHERE tenant_id = :id"),
                    {"id": row_id},
                )
            if table == "guardrail_profiles":
                conn.execute(
                    sa_text("DELETE FROM guardrail_profile_rules WHERE profile_id = :id"),
                    {"id": row_id},
                )
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})


def _bearer(email: str = "admin@aurexion.com") -> dict:
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
    return _bearer()


def _data(response, expected: int = 200):
    assert response.status_code == expected, response.text
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def _profiles_by_code(client, super_admin) -> dict:
    return {p["code"]: p for p in _data(client.get(
        f"{API}/guardrail-profiles", headers=super_admin))}


def _create_tenant(client, super_admin, *, industry: str,
                   guardrail_profile_id: str | None = None) -> dict:
    suffix = uuid.uuid4().hex[:8]
    body = {
        "name": f"GR Test {suffix}",
        "domain": f"gr-{suffix}.example.test",
        "industry": industry,
        "adminEmail": f"admin-{suffix}@example.com",
    }
    if guardrail_profile_id is not None:
        body["guardrailProfileId"] = guardrail_profile_id
    created = _data(client.post(f"{API}/tenants", headers=super_admin, json=body),
                    expected=201)
    _created.append(("tenants", created["id"]))
    return created


class TestOnboardingSuggestions:
    def test_options_expose_active_profiles_and_industry_defaults(self, client, super_admin):
        options = _data(client.get(f"{API}/onboarding/options", headers=super_admin))
        profiles = {p["code"]: p for p in options["guardrailProfiles"]}
        assert {"standard", "healthcare", "finance"} <= set(profiles)

        industries = {i["code"]: i for i in options["industries"]}
        assert industries["healthcare"]["defaultGuardrailProfileId"] == profiles["healthcare"]["id"]
        assert industries["banking"]["defaultGuardrailProfileId"] == profiles["finance"]["id"]
        assert industries["financial_services"]["defaultGuardrailProfileId"] == profiles["finance"]["id"]


class TestTenantAssignment:
    def test_explicit_override_is_persisted(self, client, super_admin):
        profiles = _profiles_by_code(client, super_admin)
        # Healthcare industry, but the Super Admin overrides to Finance.
        tenant = _create_tenant(client, super_admin, industry="healthcare",
                                guardrail_profile_id=profiles["finance"]["id"])
        assert tenant["guardrailProfileId"] == profiles["finance"]["id"]
        assert tenant["guardrailProfile"]["code"] == "finance"

    def test_industry_default_applies_when_no_profile_sent(self, client, super_admin):
        profiles = _profiles_by_code(client, super_admin)
        tenant = _create_tenant(client, super_admin, industry="healthcare")
        assert tenant["guardrailProfileId"] == profiles["healthcare"]["id"]

        tenant = _create_tenant(client, super_admin, industry="ecommerce")
        assert tenant["guardrailProfileId"] == profiles["standard"]["id"]

    def test_industry_change_never_silently_replaces_the_profile(self, client, super_admin):
        profiles = _profiles_by_code(client, super_admin)
        tenant = _create_tenant(client, super_admin, industry="healthcare")
        assert tenant["guardrailProfile"]["code"] == "healthcare"

        updated = _data(client.patch(f"{API}/tenants/{tenant['id']}",
                                     headers=super_admin, json={"industry": "banking"}))
        assert updated["industry"] == "banking"
        assert updated["guardrailProfileId"] == profiles["healthcare"]["id"]  # unchanged

        # Reassignment requires an explicit update.
        updated = _data(client.patch(
            f"{API}/tenants/{tenant['id']}", headers=super_admin,
            json={"guardrailProfileId": profiles["finance"]["id"]},
        ))
        assert updated["guardrailProfile"]["code"] == "finance"

    def test_inactive_profile_cannot_be_assigned(self, client, super_admin):
        session = get_sessionmaker()()
        try:
            profile = GuardrailProfile(
                id=new_id("gp"), code=f"inactive_{_SUFFIX}",
                name=f"Inactive {_SUFFIX}", status="inactive",
            )
            session.add(profile)
            session.commit()
            profile_id = profile.id
            _created.append(("guardrail_profiles", profile_id))
        finally:
            session.close()

        suffix = uuid.uuid4().hex[:8]
        response = client.post(f"{API}/tenants", headers=super_admin, json={
            "name": "X", "domain": f"gr-{suffix}.example.test",
            "adminEmail": f"a-{suffix}@example.com",
            "guardrailProfileId": profile_id,
        })
        assert response.status_code == 422
        assert "guardrail profile" in response.json()["message"].lower()

    def test_existing_assignment_stays_readable_after_deactivation(self, client, super_admin):
        profiles = _profiles_by_code(client, super_admin)
        created = _data(client.post(f"{API}/guardrail-profiles", headers=super_admin, json={
            "code": f"custom_{_SUFFIX}", "name": f"Custom {_SUFFIX}",
            "guardrailIds": profiles["standard"]["guardrailIds"],
        }), expected=201)
        _created.append(("guardrail_profiles", created["id"]))

        tenant = _create_tenant(client, super_admin, industry="ecommerce",
                                guardrail_profile_id=created["id"])
        _data(client.post(f"{API}/guardrail-profiles/{created['id']}/status",
                          headers=super_admin, json={"status": "inactive"}))

        fetched = _data(client.get(f"{API}/tenants/{tenant['id']}", headers=super_admin))
        assert fetched["guardrailProfileId"] == created["id"]
        assert fetched["guardrailProfile"]["status"] == "inactive"

        # The deactivated profile's rules still enforce for the tenant.
        effective = _data(client.get(
            f"{API}/tenants/{tenant['id']}/effective-guardrails", headers=super_admin))
        assert effective["profile"]["id"] == created["id"]
        codes = {r["code"] for r in effective["rules"]}
        assert "profanity_deescalation" in codes

        # But it cannot be assigned to a NEW tenant anymore.
        suffix = uuid.uuid4().hex[:8]
        response = client.post(f"{API}/tenants", headers=super_admin, json={
            "name": "Y", "domain": f"gr-{suffix}.example.test",
            "adminEmail": f"b-{suffix}@example.com",
            "guardrailProfileId": created["id"],
        })
        assert response.status_code == 422


class TestEffectiveGuardrails:
    def test_mandatory_rules_apply_even_without_a_profile(self, client, super_admin):
        session = get_sessionmaker()()
        try:
            tenant_id = f"tn_test_gr_{uuid.uuid4().hex[:8]}"
            session.add(Tenant(id=tenant_id, name="No Profile",
                               domain=f"{tenant_id}.example.test", status="active"))
            session.commit()
            _created.append(("tenants", tenant_id))
        finally:
            session.close()

        effective = _data(client.get(
            f"{API}/tenants/{tenant_id}/effective-guardrails", headers=super_admin))
        assert effective["profile"] is None
        codes = {r["code"] for r in effective["rules"]}
        assert {"pii_redaction", "secret_leakage_prevention",
                "unsafe_tool_call_block", "prompt_injection_protection"} <= codes
        assert all(r["mandatory"] for r in effective["rules"])

    def test_profile_rules_are_added_on_top_of_mandatory(self, client, super_admin):
        tenant = _create_tenant(client, super_admin, industry="healthcare")
        effective = _data(client.get(
            f"{API}/tenants/{tenant['id']}/effective-guardrails", headers=super_admin))
        codes = {r["code"] for r in effective["rules"]}
        assert "medical_advice_boundary" in codes
        assert "pii_redaction" in codes


class TestMandatoryGuardrailProtection:
    def test_mandatory_guardrail_cannot_be_disabled_or_weakened(self, client, super_admin):
        guardrails = {g["code"]: g for g in _data(client.get(
            f"{API}/guardrails", headers=super_admin))}
        mandatory = guardrails["pii_redaction"]
        assert mandatory["isMandatory"] is True

        response = client.patch(f"{API}/guardrails/{mandatory['id']}",
                                headers=super_admin, json={"enabled": False})
        assert response.status_code == 409
        response = client.patch(f"{API}/guardrails/{mandatory['id']}",
                                headers=super_admin, json={"enforcement": "flag"})
        assert response.status_code == 409

        # Still enabled afterwards.
        refreshed = {g["code"]: g for g in _data(client.get(
            f"{API}/guardrails", headers=super_admin))}
        assert refreshed["pii_redaction"]["enabled"] is True


class TestProfileManagement:
    def test_rule_membership_change_bumps_version_and_audits(self, client, super_admin):
        guardrails = {g["code"]: g for g in _data(client.get(
            f"{API}/guardrails", headers=super_admin))}
        created = _data(client.post(f"{API}/guardrail-profiles", headers=super_admin, json={
            "code": f"vtest_{_SUFFIX}", "name": f"Version Test {_SUFFIX}",
            "guardrailIds": [guardrails["profanity_deescalation"]["id"]],
        }), expected=201)
        _created.append(("guardrail_profiles", created["id"]))
        assert created["version"] == 1

        updated = _data(client.patch(
            f"{API}/guardrail-profiles/{created['id']}", headers=super_admin,
            json={"guardrailIds": [
                guardrails["profanity_deescalation"]["id"],
                guardrails["competitor_mention_flag"]["id"],
            ]},
        ))
        assert updated["version"] == 2
        assert len(updated["guardrails"]) == 2

        audit = _data(client.get(
            f"{API}/audit?search=guardrail profile", headers=super_admin))
        assert any(e["action"] == "Updated guardrail profile" for e in audit)


class TestTriggerScoping:
    def test_triggers_are_tenant_scoped(self, client, super_admin):
        tenant_a = _create_tenant(client, super_admin, industry="ecommerce")
        tenant_b = _create_tenant(client, super_admin, industry="ecommerce")

        session = get_sessionmaker()()
        try:
            guardrail = session.scalar(
                select(Guardrail).where(Guardrail.code == "pii_redaction"))
            for tenant_id in (tenant_a["id"], tenant_b["id"]):
                trigger = GuardrailTrigger(
                    id=new_id("gt"), tenant_id=tenant_id, bot_id=None,
                    session_id=f"vs_test_{uuid.uuid4().hex[:8]}",
                    guardrail_id=guardrail.id, guardrail_code="pii_redaction",
                    rule_name=guardrail.name, action="redact", stage="transcript",
                    detail="test trigger",
                )
                session.add(trigger)
                session.flush()
                _created.append(("guardrail_triggers", trigger.id))
            # Tenant-admin account under tenant A only.
            role = session.scalar(select(Role).where(Role.code == "tenant_admin"))
            admin_email = f"gradmin-{_SUFFIX}@example.test"
            user = User(id=new_id("usr"), email=admin_email, name="GR Admin",
                        password_hash="x", role_id=role.id,
                        tenant_id=tenant_a["id"], status="active")
            session.add(user)
            session.commit()
        finally:
            session.close()

        # Super admin sees both (filtered per tenant).
        rows = _data(client.get(
            f"{API}/guardrail-triggers?tenantId={tenant_b['id']}", headers=super_admin))
        assert rows and all(r["tenantId"] == tenant_b["id"] for r in rows)

        # Tenant A's admin sees only tenant A rows — the filter is forced.
        tenant_admin = _bearer(admin_email)
        rows = _data(client.get(
            f"{API}/guardrail-triggers?tenantId={tenant_b['id']}", headers=tenant_admin))
        assert all(r["tenantId"] == tenant_a["id"] for r in rows)
        # Raw sensitive values never appear.
        assert all("4111" not in (r["detail"] or "") for r in rows)
