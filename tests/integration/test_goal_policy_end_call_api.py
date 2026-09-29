"""PATCH /bots/{id}/goal-policy/end-call — the end-call switch, and only it.

goal_policy is one JSON document the generic voice-settings PUT replaces
whole; the switch has its own endpoint so a UI toggle can never drop the
bot's goals/safety rules. Covers: merge into the LATEST stored value, default
OFF, enable/disable, strict body, bot-management permission, cross-tenant
404, audit row, bot-config cache invalidation, and save-time rejection of an
invalid endCall through the generic PUT.
"""

import uuid

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.security import create_access_token
from backend.main import app
from shared.config import get_settings
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import AuditLog, Role, Tenant, User, VoiceBot, VoiceBotSetting

pytestmark = pytest.mark.integration

API = "/api/v1"

# Manappuram-shaped authored policy: every key must survive every switch.
SURVEY_POLICY = {
    "role": "closure feedback executive",
    "domain": "Customer feedback survey",
    "goals": [{"id": "feedback", "description": "Collect closure feedback."}],
    "safety": ["A clear wrong-person answer at opening means no survey."],
    "toolRules": ["Product facts come from the knowledge base."],
    "outOfScope": "Briefly return to the feedback survey.",
    "allowedTopics": ["gold-loan closure experience", "service rating"],
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def ws():
    """A tenant (admin + tenant user + two bots) and a foreign tenant's bot."""
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    tenant_ids: list[str] = []
    try:
        roles = {r.code: r for r in session.scalars(
            select(Role).where(Role.code.in_(("tenant_admin", "tenant_user"))))}
        tenant = Tenant(id=new_id("tn"), name=f"EndCall Test {suffix}", code=f"endcall_{suffix}",
                        domain=f"endcall-{suffix}.example.test", status="active")
        foreign = Tenant(id=new_id("tn"), name=f"EndCall Foreign {suffix}", code=f"endfor_{suffix}",
                         domain=f"endfor-{suffix}.example.test", status="active")
        session.add_all([tenant, foreign])
        session.flush()
        tenant_ids = [tenant.id, foreign.id]
        admin = User(id=new_id("usr"), email=f"endcall.admin.{suffix}@example.test", name="EC Admin",
                     password_hash="x", role_id=roles["tenant_admin"].id, tenant_id=tenant.id,
                     status="active")
        member = User(id=new_id("usr"), email=f"endcall.user.{suffix}@example.test", name="EC Member",
                      password_hash="x", role_id=roles["tenant_user"].id, tenant_id=tenant.id,
                      status="active")
        foreign_admin = User(id=new_id("usr"), email=f"endcall.for.{suffix}@example.test",
                             name="EC Foreign", password_hash="x",
                             role_id=roles["tenant_admin"].id, tenant_id=foreign.id, status="active")
        session.add_all([admin, member, foreign_admin])
        session.flush()
        survey = VoiceBot(id=new_id("bot"), tenant_id=tenant.id, name=f"Survey {suffix}",
                          status="published", owner_user_id=admin.id)
        bare = VoiceBot(id=new_id("bot"), tenant_id=tenant.id, name=f"Bare {suffix}",
                        status="draft", owner_user_id=admin.id)
        foreign_bot = VoiceBot(id=new_id("bot"), tenant_id=foreign.id, name=f"Foreign {suffix}",
                               status="published", owner_user_id=foreign_admin.id)
        session.add_all([survey, bare, foreign_bot])
        session.flush()
        session.add_all([
            VoiceBotSetting(id=new_id("vbs"), bot_id=survey.id, tenant_id=tenant.id,
                            goal_policy=dict(SURVEY_POLICY)),
            VoiceBotSetting(id=new_id("vbs"), bot_id=foreign_bot.id, tenant_id=foreign.id,
                            goal_policy=dict(SURVEY_POLICY)),
        ])
        session.commit()

        def bearer(u: User, role: str) -> dict:
            return {"Authorization": "Bearer " + create_access_token(
                user_id=u.id, role=role, tenant_id=u.tenant_id)}

        yield {
            "tenant_id": tenant.id, "survey": survey.id, "bare": bare.id,
            "foreign_bot": foreign_bot.id, "foreign_tenant_id": foreign.id,
            "admin": bearer(admin, "tenant_admin"), "member": bearer(member, "tenant_user"),
            "foreign_admin": bearer(foreign_admin, "tenant_admin"),
        }
    finally:
        session.rollback()
        for model in (AuditLog, VoiceBotSetting, VoiceBot, User):
            session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
        session.close()


def stored_policy(bot_id: str):
    session = get_sessionmaker()()
    try:
        return session.scalar(
            select(VoiceBotSetting.goal_policy).where(VoiceBotSetting.bot_id == bot_id))
    finally:
        session.close()


def set_stored_policy(bot_id: str, policy) -> None:
    session = get_sessionmaker()()
    try:
        row = session.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot_id))
        row.goal_policy = policy
        session.commit()
    finally:
        session.close()


def audit_rows(bot_id: str) -> list[AuditLog]:
    session = get_sessionmaker()()
    try:
        return list(session.scalars(select(AuditLog).where(
            AuditLog.entity_id == bot_id,
            AuditLog.action.like("%end call on goal decision"),
        ).order_by(AuditLog.created_at)))
    finally:
        session.close()


def patch(client, ws, bot_key, body, who="admin"):
    return client.patch(f"{API}/bots/{ws[bot_key]}/goal-policy/end-call",
                        headers=ws[who], json=body)


class TestSwitch:
    def test_default_is_off(self, client, ws):
        data = client.get(f"{API}/bots/{ws['survey']}/voice-settings",
                          headers=ws["admin"]).json()["data"]
        assert data["goalPolicy"] == SURVEY_POLICY
        assert "endCall" not in data["goalPolicy"]
        # Turning OFF an absent switch is a no-op: nothing written, no audit.
        r = patch(client, ws, "survey", {"enabled": False})
        assert r.status_code == 200, r.text
        assert r.json()["data"] == {"botId": ws["survey"], "enabled": False}
        assert stored_policy(ws["survey"]) == SURVEY_POLICY
        assert audit_rows(ws["survey"]) == []

    def test_enable_then_disable_preserves_every_other_key(self, client, ws):
        r = patch(client, ws, "survey", {"enabled": True})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["enabled"] is True
        assert stored_policy(ws["survey"]) == {**SURVEY_POLICY, "endCall": {"enabled": True}}

        r = patch(client, ws, "survey", {"enabled": False})
        assert r.status_code == 200, r.text
        assert r.json()["data"]["enabled"] is False
        assert stored_policy(ws["survey"]) == {**SURVEY_POLICY, "endCall": {"enabled": False}}

    def test_merges_into_the_latest_stored_value(self, client, ws):
        # Another writer (bot import, script, second admin) changed the policy
        # AFTER a UI loaded it: the switch must not resurrect the old copy.
        client.get(f"{API}/bots/{ws['survey']}/voice-settings", headers=ws["admin"])
        newer = {**SURVEY_POLICY, "restrictedTopics": ["investment advice"],
                 "endCall": {"enabled": False, "minConfidence": 0.95}}
        set_stored_policy(ws["survey"], newer)
        assert patch(client, ws, "survey", {"enabled": True}).status_code == 200
        assert stored_policy(ws["survey"]) == {
            **newer, "endCall": {"enabled": True, "minConfidence": 0.95}}

    def test_invalid_stored_switch_is_replaced_and_rest_kept(self, client, ws):
        set_stored_policy(ws["survey"], {**SURVEY_POLICY, "endCall": {"enabled": "yes",
                                                                      "signals": ["angry"]}})
        r = patch(client, ws, "survey", {"enabled": True})
        assert r.status_code == 200 and r.json()["data"]["enabled"] is True
        assert stored_policy(ws["survey"]) == {**SURVEY_POLICY, "endCall": {"enabled": True}}

    def test_bot_without_settings_row_or_policy(self, client, ws):
        r = patch(client, ws, "bare", {"enabled": True})
        assert r.status_code == 200, r.text
        assert stored_policy(ws["bare"]) == {"endCall": {"enabled": True}}

    @pytest.mark.parametrize("body", [
        {}, {"enabled": "true"}, {"enabled": 1}, {"enabled": None},
        {"enabled": True, "signals": ["refusal"]},
        {"enabled": True, "minConfidence": 0.5},
        {"enabled": True, "goalPolicy": {}},
    ])
    def test_body_accepts_only_the_boolean_switch(self, client, ws, body):
        r = patch(client, ws, "survey", body)
        assert r.status_code == 422, r.text
        assert stored_policy(ws["survey"]) == SURVEY_POLICY


class TestAccess:
    def test_tenant_user_cannot_switch_it(self, client, ws):
        # tenant_user holds manage_voices (general voice editing) but not
        # bots.manage — the level this switch requires.
        r = patch(client, ws, "survey", {"enabled": True}, who="member")
        assert r.status_code == 403, r.text
        assert stored_policy(ws["survey"]) == SURVEY_POLICY

    def test_other_tenant_gets_not_found(self, client, ws):
        r = patch(client, ws, "survey", {"enabled": True}, who="foreign_admin")
        assert r.status_code == 404, r.text
        assert stored_policy(ws["survey"]) == SURVEY_POLICY
        r = patch(client, ws, "foreign_bot", {"enabled": True})
        assert r.status_code == 404, r.text
        assert stored_policy(ws["foreign_bot"]) == SURVEY_POLICY

    def test_missing_bot_is_not_found(self, client, ws):
        r = client.patch(f"{API}/bots/bot_does_not_exist/goal-policy/end-call",
                         headers=ws["admin"], json={"enabled": True})
        assert r.status_code == 404


class TestAuditAndCache:
    def test_change_is_audited_and_invalidates_the_config_cache(self, client, ws):
        cache = redis.from_url(get_settings().redis_url)
        keys = [f"botcfg:{ws['tenant_id']}:{ws['survey']}",
                f"botcfg:by-bot:{ws['survey']}:True", f"botcfg:by-bot:{ws['survey']}:False"]
        try:
            for key in keys:
                cache.set(key, "{}", ex=300)
            assert patch(client, ws, "survey", {"enabled": True}).status_code == 200
            assert not any(cache.exists(key) for key in keys)

            rows = audit_rows(ws["survey"])
            assert [r.action for r in rows] == ["Enabled end call on goal decision"]
            assert rows[0].tenant_id == ws["tenant_id"]
            assert rows[0].previous_value == {"goalPolicy": {"endCall": None}}
            assert rows[0].new_value == {"goalPolicy": {"endCall": {"enabled": True}}}

            # An unchanged switch writes nothing and leaves the cache alone.
            for key in keys:
                cache.set(key, "{}", ex=300)
            assert patch(client, ws, "survey", {"enabled": True}).status_code == 200
            assert all(cache.exists(key) for key in keys)
            assert len(audit_rows(ws["survey"])) == 1

            assert patch(client, ws, "survey", {"enabled": False}).status_code == 200
            # created_at has second precision: compare as a multiset, not order.
            assert sorted(r.action for r in audit_rows(ws["survey"])) == [
                "Disabled end call on goal decision", "Enabled end call on goal decision"]
        finally:
            cache.delete(*keys)
            cache.close()


class TestGenericPutStillGuards:
    def test_put_rejects_an_invalid_end_call(self, client, ws):
        r = client.put(f"{API}/bots/{ws['survey']}/voice-settings", headers=ws["admin"],
                       json={"goalPolicy": {**SURVEY_POLICY,
                                            "endCall": {"enabled": True, "signals": ["angry"]}}})
        assert r.status_code == 422, r.text
        assert any(e.get("field") == "goalPolicy.endCall" for e in r.json()["errors"])
        assert stored_policy(ws["survey"]) == SURVEY_POLICY

    def test_put_accepts_a_valid_end_call(self, client, ws):
        policy = {**SURVEY_POLICY, "endCall": {"enabled": True, "minConfidence": 0.95}}
        r = client.put(f"{API}/bots/{ws['survey']}/voice-settings", headers=ws["admin"],
                       json={"goalPolicy": policy})
        assert r.status_code == 200, r.text
        assert stored_policy(ws["survey"]) == policy
