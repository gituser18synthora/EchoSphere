"""Bot Clone: POST /bots/{id}/clone.

The clone is a full CONFIGURATION copy (languages, voice settings, prompts +
versions, intents, bot-owned API connections, workflows, scenario definitions,
bot-scoped knowledge, runtime-context schema) under fresh ids, created as a
Draft that is not callable. Operational/customer data (conversations, channel
configs, phone numbers, releases, context records, test results) is never
copied. Tenancy is derived from the source bot; cross-tenant access is a
sanitized 404.
"""

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
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
    Release,
    Role,
    RuntimeContextRecord,
    RuntimeContextSchema,
    SupportedLanguage,
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

READINESS = [
    ("r1", "Knowledge sources indexed", "knowledge"),
    ("r2", "Voice selected & tuned", "voice"),
    ("r3", "Core prompts approved", "prompts"),
    ("r4", "Intents validated", "intents"),
    ("r5", "Workflow published", "workflows"),
    ("r6", "Channel connected", "channels"),
    ("r7", "Regression suite passing", "testing"),
]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def workspace():
    """A published source bot with every kind of child record, plus a second
    tenant with a foreign bot for isolation checks."""
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    try:
        admin_role = session.execute(
            select(Role).where(Role.code == "tenant_admin")).scalar_one()
        user_role = session.execute(
            select(Role).where(Role.code == "tenant_user")).scalar_one()

        tenant = Tenant(
            id=new_id("tn"), name=f"Clone Test {suffix}", code=f"clone_{suffix}",
            domain=f"clone-{suffix}.example.test", status="active",
        )
        other_tenant = Tenant(
            id=new_id("tn"), name=f"Clone Foreign {suffix}", code=f"clfor_{suffix}",
            domain=f"clfor-{suffix}.example.test", status="active",
        )
        session.add_all([tenant, other_tenant])
        session.flush()

        admin = User(
            id=new_id("usr"), email=f"clone.admin.{suffix}@example.test",
            name="Clone Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=tenant.id, status="active",
        )
        member = User(
            id=new_id("usr"), email=f"clone.user.{suffix}@example.test",
            name="Clone Member", password_hash="x", role_id=user_role.id,
            tenant_id=tenant.id, status="active",
        )
        session.add_all([admin, member])
        session.flush()

        languages = session.scalars(
            select(SupportedLanguage.code)
            .where(SupportedLanguage.enabled.is_(True))
            .order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
            .limit(2)
        ).all()
        assert languages, "seeded platform languages are required"

        bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Clone Source {suffix}",
            use_case="Billing support", description="The template bot",
            status="published", version="v2.3.0", live_version="v2.3.0",
            published_at=datetime(2026, 8, 1, 12, 0), health="healthy",
            containment=71.5, csat=4.4, owner_user_id=admin.id,
        )
        foreign_bot = VoiceBot(
            id=new_id("bot"), tenant_id=other_tenant.id,
            name=f"Clone Foreign Bot {suffix}", status="draft",
        )
        session.add_all([bot, foreign_bot])
        session.flush()

        for i, (key, label, tab) in enumerate(READINESS):
            session.add(VoiceBotReadiness(
                id=new_id("rd"), bot_id=bot.id, item_key=key, label=label,
                done=True, studio_tab=tab, sort_order=i,
            ))
        for code in languages:
            session.add(BotLanguage(bot_id=bot.id, language_code=code))

        settings_row = VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot.id, tenant_id=tenant.id,
            speed=1.15, pause_ms=250, empathy=70, energy=40,
            language_voice_map={"default": languages[0],
                                languages[0]: {"provider": "sarvam",
                                               "model": "bulbul:v2",
                                               "voice": "anushka"}},
            stt_provider="sarvam", stt_model="saarika:v2",
            stt_settings={"vad_sensitivity": 0.4},
            tts_provider="sarvam", tts_model="bulbul:v2", tts_voice="anushka",
            tts_settings={"loudness": 1.1},
            llm_provider="openai", llm_model="gpt-4o-mini",
            llm_settings={"max_output_characters": 360},
            goal_policy={"role": "Billing assistant", "goals": ["resolve billing"]},
        )
        session.add(settings_row)

        # Bot-owned API connection + a tenant-wide (shared) one.
        bot_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=bot.id,
            name="Fetch invoice", method="GET",
            url="https://billing.example.test/invoice/{{id}}",
            status="healthy", last_tested_at=datetime(2026, 8, 2, 9, 0),
            last_latency_ms=210, version=3,
        )
        shared_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=None,
            name="Shared CRM lookup", method="GET",
            url="https://crm.example.test/customer/{{id}}", status="healthy",
        )
        session.add_all([bot_api, shared_api])

        # Bot-scoped + tenant-scoped knowledge sources.
        bot_kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=bot.id, scope="bot",
            type="document", name=f"Billing FAQ {suffix}", status="indexed",
            chunks=12, size_kb=64, quality=88, usage_30d=41,
        )
        tenant_kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=None, scope="tenant",
            type="document", name=f"Tenant Handbook {suffix}", status="indexed",
        )
        session.add_all([bot_kb, tenant_kb])
        session.flush()

        workflow = Workflow(
            id=new_id("wf"), tenant_id=tenant.id, bot_id=bot.id,
            name="Billing journey", version=2, status="approved",
            nodes=[
                {"id": "n1", "kind": "start", "label": "Call starts"},
                {"id": "n2", "kind": "api", "label": "Fetch invoice",
                 "config": {"connectionId": bot_api.id}},
                {"id": "n3", "kind": "end", "label": "End call"},
            ],
            edges=[{"id": "e1", "from": "n1", "to": "n2"},
                   {"id": "e2", "from": "n2", "to": "n3"}],
            issues=[],
        )
        session.add(workflow)
        session.flush()

        prompt = Prompt(
            id=new_id("pr"), tenant_id=tenant.id, bot_id=bot.id, type="system",
            name="Core system prompt", state="approved", active_version=2,
            variables=["customer_name"],
        )
        session.add(prompt)
        session.flush()
        session.add_all([
            PromptVersion(
                id=new_id("prv"), prompt_id=prompt.id, version=1,
                note="first draft", variants=[{"language": languages[0],
                                               "content": "You are v1."}],
                compiled_prompt="You are v1.",
            ),
            PromptVersion(
                id=new_id("prv"), prompt_id=prompt.id, version=2,
                note="tightened", variants=[{"language": languages[0],
                                             "content": "You are v2."}],
                compiled_prompt="You are v2.",
            ),
        ])

        entity = EntityDef(
            id=new_id("en"), tenant_id=tenant.id,
            name=f"invoice_number_{suffix}", kind="custom", data_type="text",
        )
        session.add(entity)

        intent = Intent(
            id=new_id("in"), tenant_id=tenant.id, bot_id=bot.id,
            name="Invoice status", samples=["where is my invoice"],
            entities=[entity.name], workflow_id=workflow.id,
            api_connection_id=bot_api.id, kb_ids=[bot_kb.id, tenant_kb.id],
            avg_confidence_30d=0.91, test_pass=8, test_total=9,
        )
        shared_intent = Intent(
            id=new_id("in"), tenant_id=tenant.id, bot_id=bot.id,
            name="Talk to CRM", samples=["check my account"],
            api_connection_id=shared_api.id,
        )
        session.add_all([intent, shared_intent])
        session.flush()

        bot_api.allowed_intents = [intent.id]
        bot_api.allowed_workflows = [workflow.id]

        scenario = TestScenario(
            id=new_id("ts"), tenant_id=tenant.id, bot_id=bot.id,
            name="Happy path", suite="Regression", steps=4,
            last_run={"pass": True, "at": "2026-08-02T10:00:00Z"},
        )
        session.add(scenario)

        context_schema = RuntimeContextSchema(
            id=new_id("rcs"), tenant_id=tenant.id, bot_id=bot.id,
            name="Customer details", source_mode="api",
            api_connection_id=bot_api.id,
            fields=[{"key": "name", "type": "string"},
                    {"key": "amount_due", "type": "number", "sensitive": True}],
            test_payload={"name": "Asha", "amount_due": 1200},
        )
        context_record = RuntimeContextRecord(
            id=new_id("rcr"), tenant_id=tenant.id, bot_id=bot.id,
            phone="+919876500000", data={"name": "Asha"},
        )
        session.add_all([context_schema, context_record])

        # Operational rows that must never be copied.
        session.add_all([
            ChannelConfig(
                id=new_id("ch"), tenant_id=tenant.id, bot_id=bot.id,
                type="voice", status="live", detail="FreeSWITCH trunk",
                config={"gateway": "vaani"},
            ),
            PhoneNumber(
                id=new_id("pn"), number=f"+9198{suffix[:8]}", country="IN",
                tenant_id=tenant.id, bot_id=bot.id, status="assigned",
            ),
            Release(
                id=new_id("rl"), tenant_id=tenant.id, bot_id=bot.id,
                version="v2.3.0", stage="published",
            ),
            ConversationSession(
                id=new_id("cv"), tenant_id=tenant.id, bot_id=bot.id,
                started_at=datetime(2026, 8, 3, 10, 0), duration_sec=93,
                channel="voice", sentiment="neutral", intents=[],
                contained=True, status="completed",
            ),
        ])
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
            "foreign_bot_id": foreign_bot.id,
            "admin_id": admin.id,
            "admin": bearer(admin, "tenant_admin"),
            "member": bearer(member, "tenant_user"),
            "languages": languages,
            "workflow_id": workflow.id,
            "prompt_id": prompt.id,
            "intent_id": intent.id,
            "shared_intent_id": shared_intent.id,
            "bot_api_id": bot_api.id,
            "shared_api_id": shared_api.id,
            "bot_kb_id": bot_kb.id,
            "tenant_kb_id": tenant_kb.id,
            "entity_id": entity.id,
            "scenario_id": scenario.id,
            "schema_id": context_schema.id,
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
                      Intent, EntityDef, ApiConnection, Workflow,
                      TestScenario, KnowledgeSource,
                      ChannelConfig, PhoneNumber, Release,
                      ConversationSession, VoiceBotSetting, AuditLog, VoiceBot,
                      User):
            session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
        session.close()


def _clone(client, workspace, headers_key="admin"):
    response = client.post(
        f"{API}/bots/{workspace['bot_id']}/clone",
        headers=workspace[headers_key],
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def _db():
    return get_sessionmaker()()


# ── Security & tenancy ────────────────────────────────────────────────────────


class TestAccessControl:
    def test_requires_authentication(self, client, workspace):
        response = client.post(f"{API}/bots/{workspace['bot_id']}/clone")
        assert response.status_code == 401

    def test_tenant_user_cannot_clone(self, client, workspace):
        session = _db()
        try:
            before = len(session.scalars(select(VoiceBot.id).where(
                VoiceBot.tenant_id == workspace["tenant_id"])).all())
        finally:
            session.close()
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/clone", headers=workspace["member"])
        assert response.status_code == 403
        session = _db()
        try:
            after = len(session.scalars(select(VoiceBot.id).where(
                VoiceBot.tenant_id == workspace["tenant_id"])).all())
            assert after == before
        finally:
            session.close()

    def test_cross_tenant_clone_is_sanitized_404(self, client, workspace):
        response = client.post(
            f"{API}/bots/{workspace['foreign_bot_id']}/clone",
            headers=workspace["admin"])
        assert response.status_code == 404
        # Indistinguishable from a bot that does not exist at all.
        missing = client.post(
            f"{API}/bots/bot_000000000000/clone", headers=workspace["admin"])
        assert missing.status_code == 404
        assert response.json()["message"] == missing.json()["message"]


# ── The clone itself ──────────────────────────────────────────────────────────


class TestCloneContents:
    @pytest.fixture(scope="class")
    def cloned(self, client, workspace):
        return _clone(client, workspace)

    def test_new_id_draft_status_and_reset_state(self, cloned, workspace):
        assert cloned["id"] != workspace["bot_id"]
        assert cloned["status"] == "draft"
        assert cloned["liveVersion"] is None
        assert cloned["publishedAt"] is None
        assert cloned["version"] == "v0.1.0"
        assert cloned["health"] == "neutral"
        assert cloned["containment"] == 0
        assert cloned["csat"] == 0
        assert cloned["callsMonth"] == 0

    def test_copy_name_and_basics(self, cloned, workspace):
        assert cloned["name"] == f"{workspace['bot_name']} (copy)"
        assert cloned["useCase"] == "Billing support"
        assert cloned["description"] == "The template bot"
        assert sorted(cloned["languages"]) == sorted(workspace["languages"])

    def test_channels_not_copied(self, cloned):
        assert cloned["channels"] == []
        session = _db()
        try:
            assert session.scalar(select(ChannelConfig.id).where(
                ChannelConfig.bot_id == cloned["id"])) is None
        finally:
            session.close()

    def test_readiness_rederived_not_copied(self, cloned):
        readiness = {r["id"]: r["done"] for r in cloned["readiness"]}
        assert set(readiness) == {k for k, _, _ in READINESS}
        # Source had every flag done; the clone has no channel and no test
        # runs, so r6/r7 must be freshly derived as false.
        assert readiness["r6"] is False
        assert readiness["r7"] is False
        # Derived true from the cloned configuration itself.
        assert readiness["r1"] is True   # cloned + tenant knowledge indexed
        assert readiness["r2"] is True   # tts_voice configured
        assert readiness["r3"] is True   # approved system prompt
        assert readiness["r4"] is True   # active intents with samples
        assert readiness["r5"] is True   # approved workflow with nodes

    def test_voice_settings_copied(self, cloned, workspace):
        session = _db()
        try:
            row = session.scalar(select(VoiceBotSetting).where(
                VoiceBotSetting.bot_id == cloned["id"]))
            src = session.scalar(select(VoiceBotSetting).where(
                VoiceBotSetting.bot_id == workspace["bot_id"]))
            assert row is not None and row.id != src.id
            assert row.tenant_id == workspace["tenant_id"]
            assert row.speed == src.speed and row.pause_ms == src.pause_ms
            assert row.tts_voice == src.tts_voice
            assert row.language_voice_map == src.language_voice_map
            assert row.stt_settings == src.stt_settings
            assert row.goal_policy == src.goal_policy
        finally:
            session.close()

    def test_prompts_and_versions_copied(self, cloned, workspace):
        session = _db()
        try:
            prompts = session.scalars(select(Prompt).where(
                Prompt.bot_id == cloned["id"],
                Prompt.is_deleted.is_(False))).all()
            assert len(prompts) == 1
            clone_prompt = prompts[0]
            assert clone_prompt.id != workspace["prompt_id"]
            assert clone_prompt.type == "system"
            assert clone_prompt.state == "approved"
            assert clone_prompt.active_version == 2
            versions = {v.version: v for v in clone_prompt.versions}
            assert set(versions) == {1, 2}
            assert versions[2].compiled_prompt == "You are v2."
            src_version_ids = set(session.scalars(select(PromptVersion.id).where(
                PromptVersion.prompt_id == workspace["prompt_id"])).all())
            assert not src_version_ids.intersection(v.id for v in clone_prompt.versions)
        finally:
            session.close()

    def test_intents_copied_with_remapped_references(self, cloned, workspace):
        session = _db()
        try:
            intents = {i.name: i for i in session.scalars(select(Intent).where(
                Intent.bot_id == cloned["id"], Intent.is_deleted.is_(False)))}
            assert set(intents) == {"Invoice status", "Talk to CRM"}
            main = intents["Invoice status"]
            assert main.id != workspace["intent_id"]
            assert main.samples == ["where is my invoice"]
            # Test/analytics counters are execution data — reset.
            assert main.avg_confidence_30d == 0
            assert main.test_pass == 0 and main.test_total == 0

            clone_wf = session.scalar(select(Workflow).where(
                Workflow.bot_id == cloned["id"], Workflow.is_deleted.is_(False)))
            clone_api = session.scalar(select(ApiConnection).where(
                ApiConnection.bot_id == cloned["id"],
                ApiConnection.is_deleted.is_(False)))
            clone_kb = session.scalar(select(KnowledgeSource).where(
                KnowledgeSource.bot_id == cloned["id"],
                KnowledgeSource.is_deleted.is_(False)))
            # Bot-owned references point at the CLONED records…
            assert main.workflow_id == clone_wf.id != workspace["workflow_id"]
            assert main.api_connection_id == clone_api.id != workspace["bot_api_id"]
            assert main.kb_ids == [clone_kb.id, workspace["tenant_kb_id"]]
            # …while shared tenant resources stay associations.
            assert intents["Talk to CRM"].api_connection_id == workspace["shared_api_id"]
            # Entity names (tenant-shared) carry over untouched.
            assert main.entities and main.entities[0].startswith("invoice_number_")
        finally:
            session.close()

    def test_entities_are_shared_not_duplicated(self, cloned, workspace):
        session = _db()
        try:
            count = len(session.scalars(select(EntityDef.id).where(
                EntityDef.tenant_id == workspace["tenant_id"],
                EntityDef.is_deleted.is_(False))).all())
            assert count == 1
        finally:
            session.close()

    def test_workflow_copied_with_node_ids_remapped(self, cloned, workspace):
        session = _db()
        try:
            wf = session.scalar(select(Workflow).where(
                Workflow.bot_id == cloned["id"], Workflow.is_deleted.is_(False)))
            clone_api = session.scalar(select(ApiConnection).where(
                ApiConnection.bot_id == cloned["id"],
                ApiConnection.is_deleted.is_(False)))
            assert wf.id != workspace["workflow_id"]
            assert wf.name == "Billing journey"
            assert wf.status == "approved" and wf.version == 2
            api_node = next(n for n in wf.nodes if n["kind"] == "api")
            assert api_node["config"]["connectionId"] == clone_api.id
            assert workspace["bot_api_id"] not in str(wf.nodes)
            assert len(wf.edges) == 2
        finally:
            session.close()

    def test_api_connection_copied_with_reset_test_state(self, cloned, workspace):
        session = _db()
        try:
            row = session.scalar(select(ApiConnection).where(
                ApiConnection.bot_id == cloned["id"],
                ApiConnection.is_deleted.is_(False)))
            clone_intents = {i.name: i.id for i in session.scalars(select(Intent).where(
                Intent.bot_id == cloned["id"], Intent.is_deleted.is_(False)))}
            clone_wf_id = session.scalar(select(Workflow.id).where(
                Workflow.bot_id == cloned["id"], Workflow.is_deleted.is_(False)))
            assert row.id != workspace["bot_api_id"]
            assert row.name == "Fetch invoice"  # same name: bot-scoped resolution
            assert row.url == "https://billing.example.test/invoice/{{id}}"
            assert row.status == "untested"
            assert row.last_tested_at is None and row.last_latency_ms == 0
            assert row.allowed_intents == [clone_intents["Invoice status"]]
            assert row.allowed_workflows == [clone_wf_id]
        finally:
            session.close()

    def test_bot_scoped_knowledge_cloned_tenant_kb_shared(self, cloned, workspace):
        session = _db()
        try:
            rows = session.scalars(select(KnowledgeSource).where(
                KnowledgeSource.bot_id == cloned["id"],
                KnowledgeSource.is_deleted.is_(False))).all()
            assert len(rows) == 1
            kb = rows[0]
            assert kb.id != workspace["bot_kb_id"]
            assert kb.scope == "bot" and kb.status == "indexed"
            assert kb.chunks == 12 and kb.size_kb == 64
            assert kb.usage_30d == 0  # analytics reset
            tenant_kb = session.get(KnowledgeSource, workspace["tenant_kb_id"])
            assert tenant_kb.bot_id is None  # untouched shared source
        finally:
            session.close()

    def test_scenarios_copied_without_run_results(self, cloned, workspace):
        session = _db()
        try:
            rows = session.scalars(select(TestScenario).where(
                TestScenario.bot_id == cloned["id"],
                TestScenario.is_deleted.is_(False))).all()
            assert len(rows) == 1
            assert rows[0].id != workspace["scenario_id"]
            assert rows[0].name == "Happy path" and rows[0].steps == 4
            assert rows[0].last_run is None
        finally:
            session.close()

    def test_runtime_context_schema_copied_records_not(self, cloned, workspace):
        session = _db()
        try:
            schema = session.scalar(select(RuntimeContextSchema).where(
                RuntimeContextSchema.bot_id == cloned["id"],
                RuntimeContextSchema.is_deleted.is_(False)))
            clone_api_id = session.scalar(select(ApiConnection.id).where(
                ApiConnection.bot_id == cloned["id"],
                ApiConnection.is_deleted.is_(False)))
            assert schema is not None and schema.id != workspace["schema_id"]
            assert schema.api_connection_id == clone_api_id
            assert schema.fields == [
                {"key": "name", "type": "string"},
                {"key": "amount_due", "type": "number", "sensitive": True},
            ]
            assert schema.test_payload == {"name": "Asha", "amount_due": 1200}
            records = session.scalars(select(RuntimeContextRecord.id).where(
                RuntimeContextRecord.bot_id == cloned["id"])).all()
            assert records == []
        finally:
            session.close()

    def test_operational_data_not_copied(self, cloned):
        session = _db()
        try:
            for model in (ConversationSession, Release, PhoneNumber):
                assert session.scalar(select(model.id).where(
                    model.bot_id == cloned["id"])) is None, model.__name__
        finally:
            session.close()

    def test_source_bot_unchanged(self, cloned, workspace):
        session = _db()
        try:
            src = session.get(VoiceBot, workspace["bot_id"])
            assert src.name == workspace["bot_name"]
            assert src.status == "published"
            assert src.live_version == "v2.3.0"
            assert src.health == "healthy"
            src_wf = session.get(Workflow, workspace["workflow_id"])
            api_node = next(n for n in src_wf.nodes if n["kind"] == "api")
            assert api_node["config"]["connectionId"] == workspace["bot_api_id"]
            src_intent = session.get(Intent, workspace["intent_id"])
            assert src_intent.kb_ids == [workspace["bot_kb_id"],
                                         workspace["tenant_kb_id"]]
            assert src_intent.test_pass == 8
            phone = session.scalar(select(PhoneNumber).where(
                PhoneNumber.bot_id == workspace["bot_id"]))
            assert phone is not None and phone.status == "assigned"
        finally:
            session.close()

    def test_audit_event_links_source_and_clone(self, cloned, workspace):
        session = _db()
        try:
            row = session.scalar(select(AuditLog).where(
                AuditLog.action == "Cloned VoiceBot",
                AuditLog.entity_id == cloned["id"]))
            assert row is not None
            assert row.tenant_id == workspace["tenant_id"]
            assert row.new_value["sourceBotId"] == workspace["bot_id"]
            assert row.new_value["clonedBotId"] == cloned["id"]
        finally:
            session.close()

    def test_clone_appears_in_bot_list(self, client, cloned, workspace):
        response = client.get(f"{API}/bots?pageSize=200", headers=workspace["admin"])
        assert response.status_code == 200
        ids = [b["id"] for b in response.json()["data"]]
        assert cloned["id"] in ids


# ── Naming, repetition, integrity ────────────────────────────────────────────


class TestNamingAndIntegrity:
    def test_clone_lands_in_source_tenant_not_a_client_supplied_one(
        self, client, workspace,
    ):
        # tenantId in query/body must have no effect — tenancy comes from the
        # authenticated user and the source bot.
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/clone"
            f"?tenantId={workspace['other_tenant_id']}",
            headers=workspace["admin"],
        )
        assert response.status_code == 201
        data = response.json()["data"]
        assert data["tenantId"] == workspace["tenant_id"]

    def test_repeated_clones_get_numbered_names(self, client, workspace):
        session = _db()
        try:
            existing = set(session.scalars(select(VoiceBot.name).where(
                VoiceBot.tenant_id == workspace["tenant_id"],
                VoiceBot.is_deleted.is_(False))).all())
        finally:
            session.close()
        first = _clone(client, workspace)
        second = _clone(client, workspace)
        assert first["name"] not in existing
        assert second["name"] not in existing | {first["name"]}
        base = workspace["bot_name"]
        for name in (first["name"], second["name"]):
            assert name.startswith(f"{base} (copy")
        session = _db()
        try:
            names = session.scalars(select(VoiceBot.name).where(
                VoiceBot.tenant_id == workspace["tenant_id"],
                VoiceBot.is_deleted.is_(False))).all()
            assert len(names) == len(set(names))  # unique within the tenant
        finally:
            session.close()

    def test_child_failure_rolls_back_everything(
        self, client, workspace, monkeypatch,
    ):
        session = _db()
        try:
            bots_before = set(session.scalars(select(VoiceBot.id).where(
                VoiceBot.tenant_id == workspace["tenant_id"])).all())
            prompts_before = len(session.scalars(select(Prompt.id).where(
                Prompt.tenant_id == workspace["tenant_id"])).all())
            intents_before = len(session.scalars(select(Intent.id).where(
                Intent.tenant_id == workspace["tenant_id"])).all())
        finally:
            session.close()

        import backend.core.bot_clone as bot_clone

        # remap_ids runs after every child record has been created — failing
        # here proves the whole transaction (bot + all children) rolls back.
        def boom(*args, **kwargs):
            raise RuntimeError("simulated child clone failure")

        monkeypatch.setattr(bot_clone, "remap_ids", boom)
        # The generic 500 handler re-raises through the TestClient transport;
        # the caller-facing behavior is a 500 with the standard envelope.
        with pytest.raises(RuntimeError, match="simulated child clone failure"):
            client.post(
                f"{API}/bots/{workspace['bot_id']}/clone",
                headers=workspace["admin"],
            )

        session = _db()
        try:
            bots_after = set(session.scalars(select(VoiceBot.id).where(
                VoiceBot.tenant_id == workspace["tenant_id"])).all())
            assert bots_after == bots_before
            assert len(session.scalars(select(Prompt.id).where(
                Prompt.tenant_id == workspace["tenant_id"])).all()) == prompts_before
            assert len(session.scalars(select(Intent.id).where(
                Intent.tenant_id == workspace["tenant_id"])).all()) == intents_before
            orphan_bot_ids = bots_after | {"none"}
            orphans = session.scalars(select(Intent.id).where(
                Intent.tenant_id == workspace["tenant_id"],
                Intent.bot_id.notin_(orphan_bot_ids))).all()
            assert orphans == []
        finally:
            session.close()

    def test_existing_create_flow_unchanged(self, client, workspace):
        response = client.post(
            f"{API}/bots", headers=workspace["admin"],
            json={"name": f"Fresh Bot {workspace['suffix']}",
                  "useCase": "FAQ & information"},
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert data["status"] == "draft"
        assert data["name"] == f"Fresh Bot {workspace['suffix']}"


# ── PostgreSQL knowledge plane ───────────────────────────────────────────────


class TestKnowledgePlaneCopy:
    async def test_documents_and_chunks_copied_under_new_kb_id(
        self, client, workspace,
    ):
        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

        marker = f"clone-doc-{workspace['suffix']}.pdf"
        doc_id = new_id("kdoc")
        async with get_pg_sessionmaker()() as pg:
            pg.add(KnowledgeDocument(
                id=doc_id, tenant_id=workspace["tenant_id"],
                kb_id=workspace["bot_kb_id"], file_name=marker, file_ext="pdf",
                mime_type="application/pdf", size_bytes=1024,
                content_hash=f"hash-{workspace['suffix']}",
                storage_path=f"kb/{marker}", status="ready", chunk_count=1,
            ))
            await pg.flush()  # the chunk references the document
            pg.add(KnowledgeChunk(
                id=new_id("chk"), tenant_id=workspace["tenant_id"],
                kb_id=workspace["bot_kb_id"], document_id=doc_id,
                chunk_index=0, content="Grace period is 30 days.",
                content_hash=f"chunkhash-{workspace['suffix']}",
                token_count=7, status="active",
            ))
            await pg.commit()

        try:
            cloned = _clone(client, workspace)
            session = _db()
            try:
                new_kb_id = session.scalar(select(KnowledgeSource.id).where(
                    KnowledgeSource.bot_id == cloned["id"],
                    KnowledgeSource.is_deleted.is_(False)))
            finally:
                session.close()

            async with get_pg_sessionmaker()() as pg:
                docs = (await pg.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.kb_id == new_kb_id)
                )).scalars().all()
                assert len(docs) == 1
                copied = docs[0]
                assert copied.id != doc_id
                assert copied.file_name == marker
                assert copied.status == "ready"
                chunks = (await pg.execute(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.kb_id == new_kb_id)
                )).scalars().all()
                assert len(chunks) == 1
                assert chunks[0].document_id == copied.id
                assert chunks[0].content == "Grace period is 30 days."
        finally:
            async with get_pg_sessionmaker()() as pg:
                doc_ids = (await pg.execute(
                    select(KnowledgeDocument.id).where(
                        KnowledgeDocument.file_name == marker)
                )).scalars().all()
                if doc_ids:
                    await pg.execute(delete(KnowledgeChunk).where(
                        KnowledgeChunk.document_id.in_(doc_ids)))
                    await pg.execute(delete(KnowledgeDocument).where(
                        KnowledgeDocument.id.in_(doc_ids)))
                await pg.commit()
