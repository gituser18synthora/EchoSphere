"""Bot Delete (archive): DELETE /bots/{id}.

Deleting a bot must take it out of service immediately — soft-deleted (gone
from lists), channels archived + disabled, phone numbers released back to the
pool, runtime config cache invalidated — while RETAINING its configuration
children (prompts, intents, workflows, knowledge, scenarios, context schema)
and history (conversations, usage) under the soft-deleted bot. Tenant-shared
resources are never touched. Cross-tenant access is a sanitized 404.
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.errors import NotFoundError
from shared.ids import new_id
from shared.models import (
    ApiConnection,
    AuditLog,
    BotLanguage,
    ChannelConfig,
    ConversationSession,
    EntityDef,
    Intent,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    PromptVersion,
    Role,
    RuntimeContextRecord,
    RuntimeContextSchema,
    Tenant,
    TestScenario,
    User,
    VoiceBot,
    VoiceBotReadiness,
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
    """One tenant with two bots — `bot` (fully loaded, deleted by the flow
    tests) and `guard_bot` (must survive every failure-path test) — plus a
    foreign tenant/bot for isolation checks."""
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    try:
        admin_role = session.execute(
            select(Role).where(Role.code == "tenant_admin")).scalar_one()
        user_role = session.execute(
            select(Role).where(Role.code == "tenant_user")).scalar_one()

        tenant = Tenant(
            id=new_id("tn"), name=f"Delete Test {suffix}", code=f"bdel_{suffix}",
            domain=f"bdel-{suffix}.example.test", status="active",
        )
        other_tenant = Tenant(
            id=new_id("tn"), name=f"Delete Foreign {suffix}", code=f"bdf_{suffix}",
            domain=f"bdf-{suffix}.example.test", status="active",
        )
        session.add_all([tenant, other_tenant])
        session.flush()

        admin = User(
            id=new_id("usr"), email=f"bdel.admin.{suffix}@example.test",
            name="Delete Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=tenant.id, status="active",
        )
        member = User(
            id=new_id("usr"), email=f"bdel.user.{suffix}@example.test",
            name="Delete Member", password_hash="x", role_id=user_role.id,
            tenant_id=tenant.id, status="active",
        )
        session.add_all([admin, member])
        session.flush()

        bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Delete Target {suffix}",
            use_case="Billing support", status="published", version="v1.2.0",
            live_version="v1.2.0", published_at=datetime(2026, 8, 1, 12, 0),
            health="good", owner_user_id=admin.id,
        )
        guard_bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Delete Guard {suffix}",
            status="published", owner_user_id=admin.id,
        )
        foreign_bot = VoiceBot(
            id=new_id("bot"), tenant_id=other_tenant.id,
            name=f"Delete Foreign Bot {suffix}", status="draft",
        )
        session.add_all([bot, guard_bot, foreign_bot])
        session.flush()

        number = f"+9177{suffix[:8]}"
        session.add_all([
            ChannelConfig(
                id=new_id("ch"), tenant_id=tenant.id, bot_id=bot.id,
                type="voice", status="live", enabled=True,
                config={"phoneNumber": number, "telephonyProvider": "vaani"},
            ),
            ChannelConfig(
                id=new_id("ch"), tenant_id=tenant.id, bot_id=bot.id,
                type="whatsapp", status="configured", enabled=True,
                config={"webhookSecretReference": "env:WA_SECRET"},
            ),
            PhoneNumber(
                id=new_id("pn"), number=number, country="IN",
                tenant_id=tenant.id, bot_id=bot.id, status="assigned",
            ),
            # Guard bot keeps its own live channel — must stay untouched.
            ChannelConfig(
                id=new_id("ch"), tenant_id=tenant.id, bot_id=guard_bot.id,
                type="voice", status="live", enabled=True,
                config={"phoneNumber": f"+9178{suffix[:8]}"},
            ),
            PhoneNumber(
                id=new_id("pn"), number=f"+9178{suffix[:8]}", country="IN",
                tenant_id=tenant.id, bot_id=guard_bot.id, status="assigned",
            ),
        ])

        prompt = Prompt(
            id=new_id("pr"), tenant_id=tenant.id, bot_id=bot.id, type="system",
            name="Core prompt", state="approved", active_version=1,
        )
        session.add(prompt)
        session.flush()
        session.add(PromptVersion(
            id=new_id("prv"), prompt_id=prompt.id, version=1,
            compiled_prompt="You are the delete-target bot.",
        ))

        shared_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=None,
            name=f"Shared CRM {suffix}", method="GET",
            url="https://crm.example.test/x", status="healthy",
        )
        bot_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=bot.id,
            name="Bot API", method="GET",
            url="https://billing.example.test/x", status="healthy",
        )
        entity = EntityDef(
            id=new_id("en"), tenant_id=tenant.id, name=f"order_id_{suffix}",
            kind="custom", data_type="text",
        )
        tenant_kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=None, scope="tenant",
            type="document", name=f"Tenant KB {suffix}", status="indexed",
        )
        bot_kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=bot.id, scope="bot",
            type="document", name=f"Bot KB {suffix}", status="indexed",
        )
        workflow = Workflow(
            id=new_id("wf"), tenant_id=tenant.id, bot_id=bot.id,
            name="Journey", version=1, status="approved",
            nodes=[{"id": "n1", "kind": "start"}], edges=[], issues=[],
        )
        intent = Intent(
            id=new_id("in"), tenant_id=tenant.id, bot_id=bot.id,
            name="Order status", samples=["where is my order"],
        )
        scenario = TestScenario(
            id=new_id("ts"), tenant_id=tenant.id, bot_id=bot.id,
            name="Happy path", steps=2, last_run={"pass": True},
        )
        schema = RuntimeContextSchema(
            id=new_id("rcs"), tenant_id=tenant.id, bot_id=bot.id,
            name="Customer details", source_mode="manual",
            fields=[{"key": "name", "type": "string"}],
        )
        record = RuntimeContextRecord(
            id=new_id("rcr"), tenant_id=tenant.id, bot_id=bot.id,
            phone="+919876500001", data={"name": "Asha"},
        )
        conversation = ConversationSession(
            id=new_id("cv"), tenant_id=tenant.id, bot_id=bot.id,
            started_at=datetime(2026, 8, 3, 10, 0), duration_sec=61,
            channel="voice", sentiment="neutral", intents=[], contained=True,
            status="completed",
        )
        session.add_all([shared_api, bot_api, entity, tenant_kb, bot_kb,
                         workflow, intent, scenario, schema, record,
                         conversation])
        session.commit()

        def bearer(u: User, role_code: str) -> dict:
            token = create_access_token(
                user_id=u.id, role=role_code, tenant_id=u.tenant_id)
            return {"Authorization": f"Bearer {token}"}

        yield {
            "suffix": suffix,
            "tenant_id": tenant.id,
            "other_tenant_id": other_tenant.id,
            "bot_id": bot.id,
            "bot_name": bot.name,
            "guard_bot_id": guard_bot.id,
            "foreign_bot_id": foreign_bot.id,
            "admin_id": admin.id,
            "admin": bearer(admin, "tenant_admin"),
            "member": bearer(member, "tenant_user"),
            "number": number,
            "prompt_id": prompt.id,
            "intent_id": intent.id,
            "workflow_id": workflow.id,
            "bot_kb_id": bot_kb.id,
            "tenant_kb_id": tenant_kb.id,
            "shared_api_id": shared_api.id,
            "bot_api_id": bot_api.id,
            "entity_id": entity.id,
            "scenario_id": scenario.id,
            "schema_id": schema.id,
            "record_id": record.id,
            "conversation_id": conversation.id,
        }
    finally:
        tenant_ids = [tenant.id, other_tenant.id]
        session.rollback()
        bot_ids = session.scalars(
            select(VoiceBot.id).where(VoiceBot.tenant_id.in_(tenant_ids))).all()
        prompt_ids = session.scalars(
            select(Prompt.id).where(Prompt.tenant_id.in_(tenant_ids))).all()
        if prompt_ids:
            session.execute(delete(PromptVersion).where(
                PromptVersion.prompt_id.in_(prompt_ids)))
        if bot_ids:
            session.execute(delete(BotLanguage).where(
                BotLanguage.bot_id.in_(bot_ids)))
            session.execute(delete(VoiceBotReadiness).where(
                VoiceBotReadiness.bot_id.in_(bot_ids)))
        for model in (RuntimeContextSchema, RuntimeContextRecord, Prompt,
                      Intent, EntityDef, ApiConnection, Workflow, TestScenario,
                      KnowledgeSource, ChannelConfig, PhoneNumber,
                      ConversationSession, VoiceBotSetting, AuditLog, VoiceBot,
                      User):
            session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
        session.close()


def _db():
    return get_sessionmaker()()


def _bot_state(bot_id):
    session = _db()
    try:
        bot = session.get(VoiceBot, bot_id)
        return {"is_deleted": bot.is_deleted, "status": bot.status,
                "deleted_by": bot.deleted_by}
    finally:
        session.close()


# ── Security & tenancy ────────────────────────────────────────────────────────


class TestDeleteAccess:
    def test_requires_authentication(self, client, workspace):
        response = client.delete(f"{API}/bots/{workspace['guard_bot_id']}")
        assert response.status_code == 401
        assert _bot_state(workspace["guard_bot_id"])["is_deleted"] is False

    def test_tenant_user_cannot_delete(self, client, workspace):
        response = client.delete(
            f"{API}/bots/{workspace['guard_bot_id']}", headers=workspace["member"])
        assert response.status_code == 403
        assert _bot_state(workspace["guard_bot_id"])["is_deleted"] is False

    def test_cross_tenant_delete_is_sanitized_404(self, client, workspace):
        response = client.delete(
            f"{API}/bots/{workspace['foreign_bot_id']}", headers=workspace["admin"])
        assert response.status_code == 404
        missing = client.delete(
            f"{API}/bots/bot_000000000000", headers=workspace["admin"])
        assert missing.status_code == 404
        # Indistinguishable from a bot that does not exist at all.
        assert response.json()["message"] == missing.json()["message"]
        assert _bot_state(workspace["foreign_bot_id"])["is_deleted"] is False

    def test_hard_delete_never_purges_the_row(self, client, workspace):
        """?hard=true is env-gated; even where the env allows it the platform
        only ever soft-deletes — the row must survive for audit/history."""
        from shared.config import get_settings

        created = client.post(
            f"{API}/bots", headers=workspace["admin"],
            json={"name": f"Hard Probe {workspace['suffix']}"},
        ).json()["data"]
        response = client.delete(
            f"{API}/bots/{created['id']}?hard=true", headers=workspace["admin"])
        session = _db()
        try:
            bot = session.get(VoiceBot, created["id"])
            assert bot is not None  # never physically removed
            if get_settings().allow_hard_delete:
                assert response.status_code == 200
                assert bot.is_deleted is True
            else:
                # Guard fires before any mutation.
                assert response.status_code == 403
                assert bot.is_deleted is False
        finally:
            session.close()


# ── Failure / rollback ────────────────────────────────────────────────────────


class TestDeleteFailure:
    def test_failure_rolls_back_channel_and_number_teardown(
        self, client, workspace, monkeypatch,
    ):
        import backend.routers.bots as bots_router

        # record_audit runs after the channel/number teardown but before the
        # commit — failing there proves the whole operation is atomic.
        def boom(*args, **kwargs):
            raise RuntimeError("simulated delete failure")

        monkeypatch.setattr(bots_router, "record_audit", boom)
        # The generic 500 handler re-raises through the TestClient transport.
        with pytest.raises(RuntimeError, match="simulated delete failure"):
            client.delete(
                f"{API}/bots/{workspace['guard_bot_id']}",
                headers=workspace["admin"])

        assert _bot_state(workspace["guard_bot_id"])["is_deleted"] is False
        session = _db()
        try:
            channel = session.scalar(select(ChannelConfig).where(
                ChannelConfig.bot_id == workspace["guard_bot_id"]))
            assert channel.enabled is True and channel.is_deleted is False
            phone = session.scalar(select(PhoneNumber).where(
                PhoneNumber.bot_id == workspace["guard_bot_id"]))
            assert phone is not None and phone.status == "assigned"
        finally:
            session.close()

    def test_cache_invalidation_called_after_delete(
        self, client, workspace, monkeypatch,
    ):
        import shared.bot_config as bot_config

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            bot_config, "invalidate_bot_config_sync",
            lambda tenant_id, bot_id: calls.append((tenant_id, bot_id)),
        )
        created = client.post(
            f"{API}/bots", headers=workspace["admin"],
            json={"name": f"Cache Probe {workspace['suffix']}"},
        ).json()["data"]
        response = client.delete(
            f"{API}/bots/{created['id']}", headers=workspace["admin"])
        assert response.status_code == 200
        assert (workspace["tenant_id"], created["id"]) in calls


# ── The delete itself ─────────────────────────────────────────────────────────


class TestDeleteFlow:
    @pytest.fixture(scope="class")
    def deleted(self, client, workspace):
        response = client.delete(
            f"{API}/bots/{workspace['bot_id']}", headers=workspace["admin"])
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def test_response_shape(self, deleted, workspace):
        assert deleted == {"archived": True, "id": workspace["bot_id"]}

    def test_bot_disappears_from_list_and_detail(self, client, deleted, workspace):
        listing = client.get(f"{API}/bots?pageSize=200", headers=workspace["admin"])
        ids = [b["id"] for b in listing.json()["data"]]
        assert workspace["bot_id"] not in ids
        assert workspace["guard_bot_id"] in ids  # the other bot is untouched
        detail = client.get(
            f"{API}/bots/{workspace['bot_id']}", headers=workspace["admin"])
        assert detail.status_code == 404

    def test_bot_row_soft_deleted(self, deleted, workspace):
        state = _bot_state(workspace["bot_id"])
        assert state["is_deleted"] is True
        assert state["status"] == "archived"
        assert state["deleted_by"] == workspace["admin_id"]

    def test_channels_archived_and_disabled(self, deleted, workspace):
        session = _db()
        try:
            rows = session.scalars(select(ChannelConfig).where(
                ChannelConfig.bot_id == workspace["bot_id"])).all()
            assert len(rows) == 2
            for row in rows:
                assert row.enabled is False
                assert row.is_deleted is True
                assert row.status == "archived"
        finally:
            session.close()

    def test_phone_number_released_to_pool(self, deleted, workspace):
        session = _db()
        try:
            row = session.scalar(select(PhoneNumber).where(
                PhoneNumber.number == workspace["number"]))
            assert row.bot_id is None
            assert row.status == "available"
            # Still the tenant's number — released, not confiscated.
            assert row.tenant_id == workspace["tenant_id"]
            assert row.is_deleted is False
        finally:
            session.close()

    def test_runtime_refuses_the_deleted_bot(self, deleted, workspace):
        from shared.bot_config import _load_config_sync

        with pytest.raises(NotFoundError):
            _load_config_sync(workspace["bot_id"], True)

    def test_configuration_children_retained(self, deleted, workspace):
        """The archive contract: configuration and history are RETAINED under
        the soft-deleted bot (not hard-wiped), just unreachable via the API."""
        session = _db()
        try:
            for model, row_id in (
                (Prompt, workspace["prompt_id"]),
                (Intent, workspace["intent_id"]),
                (Workflow, workspace["workflow_id"]),
                (KnowledgeSource, workspace["bot_kb_id"]),
                (TestScenario, workspace["scenario_id"]),
                (RuntimeContextSchema, workspace["schema_id"]),
                (RuntimeContextRecord, workspace["record_id"]),
                (ApiConnection, workspace["bot_api_id"]),
                (ConversationSession, workspace["conversation_id"]),
            ):
                row = session.get(model, row_id)
                assert row is not None, model.__name__
                if hasattr(row, "is_deleted"):
                    assert row.is_deleted is False, model.__name__
        finally:
            session.close()

    def test_tenant_shared_resources_untouched(self, deleted, workspace):
        session = _db()
        try:
            tenant_kb = session.get(KnowledgeSource, workspace["tenant_kb_id"])
            assert tenant_kb.is_deleted is False and tenant_kb.bot_id is None
            entity = session.get(EntityDef, workspace["entity_id"])
            assert entity.is_deleted is False
            shared_api = session.get(ApiConnection, workspace["shared_api_id"])
            assert shared_api.is_deleted is False and shared_api.bot_id is None
            guard_phone = session.scalar(select(PhoneNumber).where(
                PhoneNumber.bot_id == workspace["guard_bot_id"]))
            assert guard_phone is not None and guard_phone.status == "assigned"
            guard_channel = session.scalar(select(ChannelConfig).where(
                ChannelConfig.bot_id == workspace["guard_bot_id"]))
            assert guard_channel.enabled is True
            assert guard_channel.is_deleted is False
            foreign = session.get(VoiceBot, workspace["foreign_bot_id"])
            assert foreign.is_deleted is False
        finally:
            session.close()

    def test_audit_event_recorded(self, deleted, workspace):
        session = _db()
        try:
            row = session.scalar(select(AuditLog).where(
                AuditLog.action == "Archived VoiceBot",
                AuditLog.entity_id == workspace["bot_id"]))
            assert row is not None
            assert row.tenant_id == workspace["tenant_id"]
            assert row.new_value["channelsArchived"] == 2
            assert row.new_value["phoneNumbersReleased"] == 1
        finally:
            session.close()

    def test_second_delete_is_404(self, client, deleted, workspace):
        response = client.delete(
            f"{API}/bots/{workspace['bot_id']}", headers=workspace["admin"])
        assert response.status_code == 404

    def test_other_bot_mutations_still_work(self, client, deleted, workspace):
        """Delete must not break the surviving bots' edit flows."""
        response = client.patch(
            f"{API}/bots/{workspace['guard_bot_id']}", headers=workspace["admin"],
            json={"description": "still editable"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["description"] == "still editable"
