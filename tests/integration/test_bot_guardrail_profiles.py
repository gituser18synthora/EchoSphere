"""Bot-level guardrail profiles + compliance-policy lifecycle — API contract
against the real dev MySQL. Unique suffixes, cleaned up at teardown."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.guardrails import load_effective_guardrails_sync
from shared.ids import new_id
from shared.models import BotLanguage, Role, User, VoiceBot

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
            if table == "tenants":
                for child in ("subscriptions", "tenant_settings"):
                    conn.execute(sa_text(f"DELETE FROM `{child}` WHERE tenant_id = :id"),
                                 {"id": row_id})
                conn.execute(sa_text("DELETE FROM users WHERE tenant_id = :id"),
                             {"id": row_id})
                conn.execute(sa_text(
                    "DELETE FROM bot_languages WHERE bot_id IN "
                    "(SELECT id FROM voice_bots WHERE tenant_id = :id)"), {"id": row_id})
                conn.execute(sa_text("DELETE FROM voice_bot_readiness WHERE bot_id IN "
                                     "(SELECT id FROM voice_bots WHERE tenant_id = :id)"),
                             {"id": row_id})
                conn.execute(sa_text("DELETE FROM voice_bots WHERE tenant_id = :id"),
                             {"id": row_id})
            if table == "guardrail_profiles":
                conn.execute(sa_text(
                    "DELETE FROM guardrail_profile_rules WHERE profile_id = :id"),
                    {"id": row_id})
            if table == "compliance_policies":
                conn.execute(sa_text(
                    "DELETE FROM compliance_wordings WHERE policy_id = :id"),
                    {"id": row_id})
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})


def _bearer(email: str = "admin@aurexion.com") -> dict:
    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
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


def _profiles(client, super_admin) -> dict:
    return {p["code"]: p for p in _data(client.get(
        f"{API}/guardrail-profiles", headers=super_admin))}


@pytest.fixture(scope="module")
def tenant(client, super_admin) -> dict:
    suffix = uuid.uuid4().hex[:8]
    created = _data(client.post(f"{API}/tenants", headers=super_admin, json={
        "name": f"BotGR {suffix}", "domain": f"botgr-{suffix}.example.test",
        "industry": "banking",
        "adminEmail": f"admin-{suffix}@example.com",
    }), expected=201)
    _created.append(("tenants", created["id"]))
    return created


def _make_bot(tenant_id: str, name: str) -> str:
    session = get_sessionmaker()()
    try:
        bot = VoiceBot(id=new_id("bot"), tenant_id=tenant_id, name=name,
                       status="published")
        session.add(bot)
        session.flush()
        session.add(BotLanguage(bot_id=bot.id, language_code="hi-IN"))
        session.commit()
        return bot.id
    finally:
        session.close()


class TestBotProfileHierarchy:
    def test_bot_without_explicit_profile_inherits_tenant_default(
        self, client, super_admin, tenant
    ):
        profiles = _profiles(client, super_admin)
        bot_id = _make_bot(tenant["id"], f"Inheriting {_SUFFIX}")
        eff = _data(client.get(f"{API}/bots/{bot_id}/effective-guardrails",
                               headers=super_admin))
        assert eff["inherited"] is True
        assert eff["profile"]["code"] == "finance"  # banking industry default
        assert eff["tenantDefaultProfile"]["code"] == "finance"
        codes = {r["code"] for r in eff["rules"]}
        assert "payment_collection_restriction" in codes
        assert {"pii_redaction", "unsafe_tool_call_block"} <= codes

    def test_explicit_assignment_overrides_without_touching_siblings(
        self, client, super_admin, tenant
    ):
        profiles = _profiles(client, super_admin)
        explicit = _make_bot(tenant["id"], f"Explicit {_SUFFIX}")
        sibling = _make_bot(tenant["id"], f"Sibling {_SUFFIX}")

        updated = _data(client.patch(
            f"{API}/bots/{explicit}/guardrail-profile", headers=super_admin,
            json={"guardrailProfileId": profiles["development"]["id"]}))
        assert updated["guardrailProfileId"] == profiles["development"]["id"]

        eff = _data(client.get(f"{API}/bots/{explicit}/effective-guardrails",
                               headers=super_admin))
        assert eff["inherited"] is False
        assert eff["profile"]["code"] == "development"
        codes = {r["code"] for r in eff["rules"]}
        assert {"outbound_call_block", "state_changing_tool_block"} <= codes
        assert "pii_redaction" in codes  # mandatory rules always ride along

        sib = _data(client.get(f"{API}/bots/{sibling}/effective-guardrails",
                               headers=super_admin))
        assert sib["inherited"] is True and sib["profile"]["code"] == "finance"

        # Audit trail for the assignment.
        audit = _data(client.get(f"{API}/audit?search=bot guardrail",
                                 headers=super_admin))
        assert any(e["action"] == "Assigned bot guardrail profile" for e in audit)

    def test_tenant_default_change_does_not_touch_explicit_assignment(
        self, client, super_admin, tenant
    ):
        profiles = _profiles(client, super_admin)
        explicit = _make_bot(tenant["id"], f"Pinned {_SUFFIX}")
        inheriting = _make_bot(tenant["id"], f"Following {_SUFFIX}")
        _data(client.patch(f"{API}/bots/{explicit}/guardrail-profile",
                           headers=super_admin,
                           json={"guardrailProfileId": profiles["healthcare"]["id"]}))

        # Tenant default: finance → standard.
        _data(client.patch(f"{API}/tenants/{tenant['id']}", headers=super_admin,
                           json={"guardrailProfileId": profiles["standard"]["id"]}))
        try:
            pinned = _data(client.get(f"{API}/bots/{explicit}/effective-guardrails",
                                      headers=super_admin))
            follower = _data(client.get(f"{API}/bots/{inheriting}/effective-guardrails",
                                        headers=super_admin))
            assert pinned["profile"]["code"] == "healthcare"     # unchanged
            assert follower["profile"]["code"] == "standard"     # followed
        finally:
            _data(client.patch(f"{API}/tenants/{tenant['id']}", headers=super_admin,
                               json={"guardrailProfileId": profiles["finance"]["id"]}))

    def test_clearing_returns_to_inheritance(self, client, super_admin, tenant):
        profiles = _profiles(client, super_admin)
        bot_id = _make_bot(tenant["id"], f"Clearable {_SUFFIX}")
        _data(client.patch(f"{API}/bots/{bot_id}/guardrail-profile",
                           headers=super_admin,
                           json={"guardrailProfileId": profiles["development"]["id"]}))
        cleared = _data(client.patch(f"{API}/bots/{bot_id}/guardrail-profile",
                                     headers=super_admin,
                                     json={"guardrailProfileId": ""}))
        assert cleared["guardrailProfileId"] == ""
        eff = _data(client.get(f"{API}/bots/{bot_id}/effective-guardrails",
                               headers=super_admin))
        assert eff["inherited"] is True

    def test_inactive_profile_cannot_be_newly_assigned(self, client, super_admin, tenant):
        from shared.models import GuardrailProfile

        session = get_sessionmaker()()
        try:
            profile = GuardrailProfile(id=new_id("gp"), code=f"binact_{_SUFFIX}",
                                       name=f"BInactive {_SUFFIX}", status="inactive")
            session.add(profile)
            session.commit()
            _created.append(("guardrail_profiles", profile.id))
            profile_id = profile.id
        finally:
            session.close()
        bot_id = _make_bot(tenant["id"], f"Rejects {_SUFFIX}")
        response = client.patch(f"{API}/bots/{bot_id}/guardrail-profile",
                                headers=super_admin,
                                json={"guardrailProfileId": profile_id})
        assert response.status_code == 422

    def test_permissions_and_isolation(self, client, super_admin, tenant):
        bot_id = _make_bot(tenant["id"], f"Isolated {_SUFFIX}")
        # A tenant admin of ANOTHER tenant can neither view nor assign.
        other_admin = _bearer("priya.sharma@meridianhealth.com")
        assert client.get(f"{API}/bots/{bot_id}/effective-guardrails",
                          headers=other_admin).status_code == 404
        profiles = _profiles(client, super_admin)
        assert client.patch(f"{API}/bots/{bot_id}/guardrail-profile",
                            headers=other_admin,
                            json={"guardrailProfileId": profiles["standard"]["id"]},
                            ).status_code == 403

    def test_loader_resolves_bot_override_and_inheritance(self, client, super_admin, tenant):
        profiles = _profiles(client, super_admin)
        explicit = _make_bot(tenant["id"], f"LoaderX {_SUFFIX}")
        inheriting = _make_bot(tenant["id"], f"LoaderI {_SUFFIX}")
        _data(client.patch(f"{API}/bots/{explicit}/guardrail-profile",
                           headers=super_admin,
                           json={"guardrailProfileId": profiles["development"]["id"]}))
        assert load_effective_guardrails_sync(tenant["id"], explicit).profile_code == "development"
        assert load_effective_guardrails_sync(tenant["id"], inheriting).profile_code == "finance"
        # Migration compatibility: NULL column → tenant default, mandatory intact.
        eff = load_effective_guardrails_sync(tenant["id"], inheriting)
        assert {"pii_redaction", "secret_leakage_prevention",
                "unsafe_tool_call_block", "prompt_injection_protection"} <= {
            r.code for r in eff.rules}


class TestCompliancePolicyLifecycle:
    def _draft(self, client, super_admin, tenant, **overrides) -> dict:
        payload = {
            "tenantId": tenant["id"],
            "code": f"life_{uuid.uuid4().hex[:6]}",
            "name": "Lifecycle test policy",
            "regulator": "internal",
            "timezone": "Asia/Kolkata",
            "callingWindows": [{"days": [0, 1, 2, 3, 4, 5, 6],
                                "start": "08:00", "end": "19:00"}],
        }
        payload.update(overrides)
        created = _data(client.post(f"{API}/compliance-policies",
                                    headers=super_admin, json=payload), expected=201)
        _created.append(("compliance_policies", created["id"]))
        return created

    def test_draft_policies_never_enforce(self, client, super_admin, tenant):
        from shared.compliance import load_active_policies_sync

        draft = self._draft(client, super_admin, tenant)
        assert draft["status"] == "draft"
        active_codes = {p.code for p in load_active_policies_sync(tenant["id"])}
        assert draft["code"] not in active_codes

    def test_activation_requires_an_approval_note_and_is_audited(
        self, client, super_admin, tenant
    ):
        draft = self._draft(client, super_admin, tenant)
        response = client.post(
            f"{API}/compliance-policies/{draft['id']}/status",
            headers=super_admin, json={"status": "approved"})
        assert response.status_code == 422  # no approval note

        approved = _data(client.post(
            f"{API}/compliance-policies/{draft['id']}/status", headers=super_admin,
            json={"status": "approved", "approvalNote": "Reviewed by compliance owner."}))
        assert approved["approvedBy"] and approved["approvedAt"]

        active = _data(client.post(
            f"{API}/compliance-policies/{draft['id']}/status", headers=super_admin,
            json={"status": "active", "approvalNote": "Go live."}))
        assert active["status"] == "active"

        # Active policies are immutable.
        response = client.patch(
            f"{API}/compliance-policies/{draft['id']}", headers=super_admin,
            json={"code": draft["code"], "name": "edited", "timezone": "UTC"})
        assert response.status_code == 409

    def test_activating_a_new_version_retires_the_previous(self, client, super_admin, tenant):
        code = f"vers_{uuid.uuid4().hex[:6]}"
        first = self._draft(client, super_admin, tenant, code=code)
        second = self._draft(client, super_admin, tenant, code=code)
        assert (first["version"], second["version"]) == (1, 2)
        for policy in (first, second):
            for status in ("approved", "active"):
                _data(client.post(
                    f"{API}/compliance-policies/{policy['id']}/status",
                    headers=super_admin,
                    json={"status": status, "approvalNote": "ok"}))
        rows = _data(client.get(
            f"{API}/compliance-policies?tenantId={tenant['id']}", headers=super_admin))
        by_version = {p["version"]: p["status"] for p in rows if p["code"] == code}
        assert by_version == {1: "retired", 2: "active"}

    def test_wordings_are_immutable_versioned(self, client, super_admin, tenant):
        draft = self._draft(client, super_admin, tenant)
        _data(client.post(f"{API}/compliance-policies/{draft['id']}/wordings",
                          headers=super_admin,
                          json={"code": "notice", "language": "en", "text": "V1 text."}),
              expected=201)
        updated = _data(client.post(
            f"{API}/compliance-policies/{draft['id']}/wordings", headers=super_admin,
            json={"code": "notice", "language": "en", "text": "V2 corrected text."}),
            expected=201)
        versions = sorted(
            (w["version"], w["text"]) for w in updated["wordings"] if w["code"] == "notice")
        assert versions == [(1, "V1 text."), (2, "V2 corrected text.")]

    def test_invalid_timezone_and_windows_rejected(self, client, super_admin, tenant):
        response = client.post(f"{API}/compliance-policies", headers=super_admin, json={
            "tenantId": tenant["id"], "code": "badtz", "name": "x",
            "timezone": "Mars/OlympusMons"})
        assert response.status_code == 422
        response = client.post(f"{API}/compliance-policies", headers=super_admin, json={
            "tenantId": tenant["id"], "code": "badwin", "name": "x",
            "timezone": "UTC", "callingWindows": [{"start": "8am", "end": "19:00"}]})
        assert response.status_code == 422


class TestPreCallGate:
    """The webhook's deterministic accept/refuse checkpoint, fixed clock."""

    async def _activate_window_policy(self, client, super_admin, tenant) -> dict:
        created = _data(client.post(f"{API}/compliance-policies", headers=super_admin, json={
            "tenantId": tenant["id"],
            "code": f"gate_{uuid.uuid4().hex[:6]}",
            "name": "Gate test window",
            "regulator": "internal",
            "timezone": "Asia/Kolkata",
            "appliesTo": {"channels": ["phone"], "directions": ["outbound"],
                          "assume_direction": "outbound"},
            "callingWindows": [{"days": [0, 1, 2, 3, 4, 5, 6],
                                "start": "08:00", "end": "19:00"}],
        }), expected=201)
        _created.append(("compliance_policies", created["id"]))
        for status in ("approved", "active"):
            _data(client.post(f"{API}/compliance-policies/{created['id']}/status",
                              headers=super_admin,
                              json={"status": status, "approvalNote": "test"}))
        return created

    async def test_out_of_window_call_is_refused_before_connecting(
        self, client, super_admin, tenant
    ):
        from shared.errors import ApiError
        from shared.telephony_webhooks import enforce_pre_call_compliance

        policy = await self._activate_window_policy(client, super_admin, tenant)
        bot_id = _make_bot(tenant["id"], f"Gate {_SUFFIX}")
        night = datetime(2026, 8, 10, 16, 0, tzinfo=timezone.utc)  # 21:30 IST
        day = datetime(2026, 8, 10, 5, 0, tzinfo=timezone.utc)     # 10:30 IST

        with pytest.raises(ApiError) as exc:
            await enforce_pre_call_compliance(
                tenant_id=tenant["id"], bot_id=bot_id, caller="+911234509876",
                direction=None, call_id=f"call_{_SUFFIX}", now=night)
        assert exc.value.status_code == 403

        # Inside the window the same call proceeds.
        await enforce_pre_call_compliance(
            tenant_id=tenant["id"], bot_id=bot_id, caller="+911234509876",
            direction=None, call_id=f"call_{_SUFFIX}", now=day)

        # The refusal left a tenant-scoped ledger row with policy context and
        # no raw caller number.
        rows = _data(client.get(
            f"{API}/guardrail-triggers?tenantId={tenant['id']}", headers=super_admin))
        gate_rows = [r for r in rows if r["guardrailCode"] == "calling_window"]
        assert gate_rows, rows
        row = gate_rows[0]
        assert row["policyCode"] == policy["code"] and row["policyVersion"] == 1
        assert row["outcome"] == "blocked" and row["stage"] == "call"
        assert "1234509876" not in (row["detail"] or "")
        session = get_sessionmaker()()
        try:
            from shared.models import GuardrailTrigger

            for trig in session.scalars(select(GuardrailTrigger).where(
                    GuardrailTrigger.tenant_id == tenant["id"])):
                _created.append(("guardrail_triggers", trig.id))
        finally:
            session.close()

    async def test_development_profile_blocks_telephony_entirely(
        self, client, super_admin, tenant
    ):
        from shared.errors import ApiError
        from shared.telephony_webhooks import enforce_pre_call_compliance

        profiles = _profiles(client, super_admin)
        bot_id = _make_bot(tenant["id"], f"DevBlocked {_SUFFIX}")
        _data(client.patch(f"{API}/bots/{bot_id}/guardrail-profile",
                           headers=super_admin,
                           json={"guardrailProfileId": profiles["development"]["id"]}))
        noon = datetime(2026, 8, 10, 6, 30, tzinfo=timezone.utc)  # 12:00 IST
        with pytest.raises(ApiError) as exc:
            await enforce_pre_call_compliance(
                tenant_id=tenant["id"], bot_id=bot_id, caller="+911111222233",
                direction=None, now=noon)
        assert exc.value.status_code == 403
        session = get_sessionmaker()()
        try:
            from shared.models import GuardrailTrigger

            for trig in session.scalars(select(GuardrailTrigger).where(
                    GuardrailTrigger.tenant_id == tenant["id"])):
                _created.append(("guardrail_triggers", trig.id))
        finally:
            session.close()
