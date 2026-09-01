"""Tenant Copy/Paste deployment: GET /tenants/{id}/export + POST /tenants/import.

The exported package carries the tenant's complete configuration plane and the
import recreates it PRESERVING every id, so a tenant built locally can be
deployed to live and keep the same tenant_id / bot_id / workflow_id / ….
Verified here:

1. first import (after the source rows are gone) creates everything with the
   original ids,
2. a second import updates in place — no duplicate bots/resources,
3. an id owned by a different tenant is rejected (409) and nothing is written,
4. references (intent → workflow/API/KB, schema → API connection, guardrail
   profile assignment, prompt versions, knowledge chunks) stay valid,
5. secrets are only ever env:/secret:// references — packages carrying raw
   secrets are rejected.
"""

import copy
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
    CompliancePolicy,
    ComplianceWording,
    EntityDef,
    Guardrail,
    GuardrailProfile,
    GuardrailProfileRule,
    Intent,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    PromptVersion,
    PronunciationDictionary,
    Role,
    RuntimeContextSchema,
    SupportedLanguage,
    Tenant,
    TenantSetting,
    TestScenario,
    User,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    VoiceProfile,
    Workflow,
)

pytestmark = pytest.mark.integration

API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _db():
    return get_sessionmaker()()


def _bearer(email: str = "admin@aurexion.com") -> dict:
    session = _db()
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


def _purge_tenant_graph(tenant_ids: list[str]) -> None:
    """Hard-delete every MySQL row of the given tenants (test rows only)."""
    session = _db()
    try:
        bot_ids = session.scalars(
            select(VoiceBot.id).where(VoiceBot.tenant_id.in_(tenant_ids))).all()
        prompt_ids = session.scalars(
            select(Prompt.id).where(Prompt.tenant_id.in_(tenant_ids))).all()
        policy_ids = session.scalars(
            select(CompliancePolicy.id).where(
                CompliancePolicy.tenant_id.in_(tenant_ids))).all()
        if prompt_ids:
            session.execute(delete(PromptVersion).where(
                PromptVersion.prompt_id.in_(prompt_ids)))
        if policy_ids:
            session.execute(delete(ComplianceWording).where(
                ComplianceWording.policy_id.in_(policy_ids)))
        if bot_ids:
            session.execute(delete(BotLanguage).where(BotLanguage.bot_id.in_(bot_ids)))
            session.execute(delete(VoiceBotReadiness).where(
                VoiceBotReadiness.bot_id.in_(bot_ids)))
        for model in (RuntimeContextSchema, Prompt, Intent, EntityDef,
                      ApiConnection, Workflow, TestScenario, KnowledgeSource,
                      ChannelConfig, VoiceBotSetting, PronunciationDictionary,
                      CompliancePolicy, VoiceProfile, PhoneNumber, AuditLog,
                      VoiceBot, TenantSetting, User):
            session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
    finally:
        session.close()


async def _purge_knowledge_plane(kb_ids: list[str]) -> None:
    from sqlalchemy import delete as sa_delete

    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import (
        IngestionJob, KnowledgeChunk, KnowledgeDocument,
    )

    async with get_pg_sessionmaker()() as pg:
        doc_ids = (await pg.execute(
            select(KnowledgeDocument.id).where(KnowledgeDocument.kb_id.in_(kb_ids))
        )).scalars().all()
        if doc_ids:
            await pg.execute(sa_delete(IngestionJob).where(
                IngestionJob.document_id.in_(doc_ids)))
            await pg.execute(sa_delete(KnowledgeChunk).where(
                KnowledgeChunk.document_id.in_(doc_ids)))
            await pg.execute(sa_delete(KnowledgeDocument).where(
                KnowledgeDocument.id.in_(doc_ids)))
        await pg.commit()


@pytest.fixture(scope="module")
async def workspace():
    """A fully configured source tenant (the 'local' side), a foreign tenant
    for collision checks, and the shared platform rows the tenant references."""
    suffix = uuid.uuid4().hex[:10]
    session = _db()
    try:
        admin_role = session.execute(
            select(Role).where(Role.code == "tenant_admin")).scalar_one()

        guardrail = Guardrail(
            id=new_id("gr"), code=f"gtest_{suffix}", name=f"No profanity {suffix}",
            category="conduct", enforcement="block", enabled=True,
        )
        session.add(guardrail)
        session.flush()
        profile = GuardrailProfile(
            id=new_id("gp"), code=f"gp_{suffix}", name=f"Transfer profile {suffix}",
            status="active", version=3,
        )
        session.add(profile)
        session.flush()
        session.add(GuardrailProfileRule(
            id=new_id("gpr"), profile_id=profile.id, guardrail_id=guardrail.id))

        platform_voice = VoiceProfile(
            id=new_id("vp"), tenant_id=None, source="platform",
            name=f"Transfer Platform Voice {suffix}", gender="female",
            provider="sarvam", provider_voice_id="anushka", status="active",
        )
        session.add(platform_voice)

        tenant = Tenant(
            id=new_id("tn"), name=f"Transfer Source {suffix}",
            code=f"tr_{suffix}", domain=f"transfer-{suffix}.example.test",
            status="active", guardrail_profile_id=profile.id,
            admin_email=f"ops.{suffix}@example.test",
        )
        foreign_tenant = Tenant(
            id=new_id("tn"), name=f"Transfer Foreign {suffix}",
            code=f"trf_{suffix}", domain=f"transfer-f-{suffix}.example.test",
            status="active",
        )
        session.add_all([tenant, foreign_tenant])
        session.flush()

        session.add(TenantSetting(
            id=new_id("tset"), tenant_id=tenant.id, timezone="Asia/Kolkata",
            default_languages=["hi-IN"], branding={"logo": "acme.png"},
        ))

        admin = User(
            id=new_id("usr"), email=f"transfer.admin.{suffix}@example.test",
            name="Transfer Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=tenant.id, status="active",
        )
        session.add(admin)
        session.flush()

        cloned_voice = VoiceProfile(
            id=new_id("vp"), tenant_id=tenant.id, source="cloned",
            name=f"Cloned Brand Voice {suffix}", gender="male",
            provider="elevenlabs", provider_voice_id="abc123", status="active",
        )
        session.add(cloned_voice)

        languages = session.scalars(
            select(SupportedLanguage.code)
            .where(SupportedLanguage.enabled.is_(True))
            .order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
            .limit(2)
        ).all()
        assert languages, "seeded platform languages are required"

        bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Transfer Bot {suffix}",
            use_case="Order support", status="published", version="v1.2.0",
            live_version="v1.2.0", published_at=datetime(2026, 8, 20, 10, 0),
            voice_id=platform_voice.id, guardrail_profile_id=profile.id,
            owner_user_id=admin.id,
        )
        foreign_bot = VoiceBot(
            id=new_id("bot"), tenant_id=foreign_tenant.id,
            name=f"Transfer Foreign Bot {suffix}", status="draft",
        )
        session.add_all([bot, foreign_bot])
        session.flush()

        for code in languages:
            session.add(BotLanguage(bot_id=bot.id, language_code=code))
        session.add(VoiceBotReadiness(
            id=new_id("rd"), bot_id=bot.id, item_key="r1",
            label="Knowledge sources indexed", done=True, studio_tab="knowledge",
            sort_order=0,
        ))

        settings_row = VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot.id, tenant_id=tenant.id,
            voice_id=cloned_voice.id, speed=1.1, pause_ms=300,
            language_voice_map={"default": languages[0],
                                languages[0]: cloned_voice.id},
            stt_provider="sarvam", tts_provider="elevenlabs",
            llm_provider="openai", llm_model="gpt-4o-mini",
            goal_policy={"role": "Order assistant"},
        )
        session.add(settings_row)

        bot_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=bot.id,
            name="Fetch order", method="GET",
            url="https://orders.example.test/{{id}}",
            auth_type="api_key", secret_ref="secret://orders-api-key",
        )
        shared_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=None,
            name="Shared CRM", method="GET",
            url="https://crm.example.test/{{id}}",
        )
        session.add_all([bot_api, shared_api])

        kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=bot.id, scope="bot",
            type="document", name=f"Order FAQ {suffix}", status="indexed",
            chunks=1, size_kb=4,
        )
        session.add(kb)
        session.flush()

        workflow = Workflow(
            id=new_id("wf"), tenant_id=tenant.id, bot_id=bot.id,
            name="Order journey", version=2, status="approved",
            nodes=[{"id": "n1", "kind": "start"},
                   {"id": "n2", "kind": "api", "config": {"connectionId": bot_api.id}},
                   {"id": "n3", "kind": "end"}],
            edges=[{"id": "e1", "from": "n1", "to": "n2"},
                   {"id": "e2", "from": "n2", "to": "n3"}],
            issues=[],
        )
        session.add(workflow)

        prompt = Prompt(
            id=new_id("pr"), tenant_id=tenant.id, bot_id=bot.id, type="system",
            name="Core prompt", state="published", active_version=2,
            published_version=2,
        )
        session.add(prompt)
        session.flush()
        session.add_all([
            PromptVersion(id=new_id("prv"), prompt_id=prompt.id, version=1,
                          compiled_prompt="You are v1."),
            PromptVersion(id=new_id("prv"), prompt_id=prompt.id, version=2,
                          compiled_prompt="You are v2."),
        ])

        entity = EntityDef(
            id=new_id("en"), tenant_id=tenant.id, name=f"order_id_{suffix}",
            kind="custom", data_type="text",
        )
        session.add(entity)

        intent = Intent(
            id=new_id("in"), tenant_id=tenant.id, bot_id=bot.id,
            name="Order status", samples=["where is my order"],
            entities=[entity.name], workflow_id=workflow.id,
            api_connection_id=bot_api.id, kb_ids=[kb.id],
        )
        session.add(intent)
        session.flush()
        bot_api.allowed_intents = [intent.id]
        bot_api.allowed_workflows = [workflow.id]

        session.add(TestScenario(
            id=new_id("ts"), tenant_id=tenant.id, bot_id=bot.id,
            name="Happy path", suite="Regression", steps=3,
            last_run={"pass": True},
        ))

        schema = RuntimeContextSchema(
            id=new_id("rcs"), tenant_id=tenant.id, bot_id=bot.id,
            name="Customer details", source_mode="api",
            api_connection_id=bot_api.id,
            fields=[{"key": "name", "type": "string"}],
        )
        session.add(schema)

        voice_channel = ChannelConfig(
            id=new_id("ch"), tenant_id=tenant.id, bot_id=bot.id, type="voice",
            status="configured", enabled=True,
            config={"phoneNumber": "+14155550119", "telephonyProvider": "twilio",
                    "publicWsBase": "wss://media.example.test",
                    "authTokenReference": "env:TWILIO_AUTH_TOKEN"},
        )
        wa_channel = ChannelConfig(
            id=new_id("ch"), tenant_id=tenant.id, bot_id=bot.id, type="whatsapp",
            status="configured", enabled=True,
            config={"whatsappNumber": "+14155550118", "provider": "meta",
                    "phoneNumberId": "1234567890",
                    "apiKeyReference": "env:WA_API_KEY",
                    "webhookSecretReference": "env:WA_WEBHOOK_SECRET"},
        )
        session.add_all([voice_channel, wa_channel])

        policy = CompliancePolicy(
            id=new_id("cp"), tenant_id=tenant.id, code=f"pol_{suffix}",
            version=1, name="Calling windows", status="active",
            timezone="Asia/Kolkata",
            calling_windows=[{"days": [0, 1, 2, 3, 4], "start": "08:00",
                              "end": "19:00"}],
        )
        session.add(policy)
        session.flush()
        wording = ComplianceWording(
            id=new_id("cw"), policy_id=policy.id, code="mini_miranda",
            language="en", version=1, text="This is an attempt to collect a debt.",
        )
        session.add(wording)

        pron = PronunciationDictionary(
            id=new_id("pd"), tenant_id=tenant.id, provider="sarvam",
            provider_dict_id=f"dict_{suffix}", name="Brand names",
        )
        session.add(pron)
        session.commit()

        # PostgreSQL knowledge plane: one document with one chunk.
        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

        doc_id = new_id("kdoc")
        chunk_id = new_id("chk")
        async with get_pg_sessionmaker()() as pg:
            pg.add(KnowledgeDocument(
                id=doc_id, tenant_id=tenant.id, kb_id=kb.id,
                file_name="faq.txt", file_ext="txt", mime_type="text/plain",
                size_bytes=64, content_hash="h" * 64, status="completed",
                chunk_count=1,
            ))
            await pg.flush()
            pg.add(KnowledgeChunk(
                id=chunk_id, tenant_id=tenant.id, kb_id=kb.id,
                document_id=doc_id, chunk_index=0,
                content="Orders ship within 2 business days.",
                content_hash="c" * 64, embedding=[0.25] * 1536,
                embedding_model="mock", embedding_dimension=1536,
            ))
            await pg.commit()

        def bearer(u: User, role_code: str) -> dict:
            token = create_access_token(
                user_id=u.id, role=role_code, tenant_id=u.tenant_id)
            return {"Authorization": f"Bearer {token}"}

        yield {
            "suffix": suffix,
            "tenant_id": tenant.id,
            "foreign_tenant_id": foreign_tenant.id,
            "foreign_bot_id": foreign_bot.id,
            "bot_id": bot.id,
            "bot_name": bot.name,
            "admin_id": admin.id,
            "tenant_admin": bearer(admin, "tenant_admin"),
            "languages": languages,
            "profile_id": profile.id,
            "guardrail_id": guardrail.id,
            "platform_voice_id": platform_voice.id,
            "cloned_voice_id": cloned_voice.id,
            "settings_id": settings_row.id,
            "workflow_id": workflow.id,
            "prompt_id": prompt.id,
            "intent_id": intent.id,
            "entity_id": entity.id,
            "bot_api_id": bot_api.id,
            "shared_api_id": shared_api.id,
            "kb_id": kb.id,
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "schema_id": schema.id,
            "voice_channel_id": voice_channel.id,
            "policy_id": policy.id,
            "wording_id": wording.id,
        }
    finally:
        session.rollback()
        session.close()
        tenant_ids = [tenant.id, foreign_tenant.id]
        await _purge_knowledge_plane([kb.id])
        _purge_tenant_graph(tenant_ids)
        cleanup = _db()
        try:
            cleanup.execute(delete(GuardrailProfileRule).where(
                GuardrailProfileRule.profile_id == profile.id))
            cleanup.execute(delete(GuardrailProfile).where(
                GuardrailProfile.id == profile.id))
            cleanup.execute(delete(Guardrail).where(Guardrail.id == guardrail.id))
            cleanup.execute(delete(VoiceProfile).where(
                VoiceProfile.id == platform_voice.id))
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture(scope="module")
def package(client, workspace, super_admin):
    """The exported package — the JSON the operator copies from local."""
    return _data(client.get(
        f"{API}/tenants/{workspace['tenant_id']}/export", headers=super_admin))


# ── Access control ────────────────────────────────────────────────────────────


class TestAccessControl:
    def test_export_requires_authentication(self, client, workspace):
        response = client.get(f"{API}/tenants/{workspace['tenant_id']}/export")
        assert response.status_code == 401

    def test_export_requires_super_admin(self, client, workspace):
        response = client.get(
            f"{API}/tenants/{workspace['tenant_id']}/export",
            headers=workspace["tenant_admin"])
        assert response.status_code == 403

    def test_import_requires_super_admin(self, client, workspace):
        response = client.post(
            f"{API}/tenants/import", headers=workspace["tenant_admin"], json={})
        assert response.status_code == 403


# ── Export package ────────────────────────────────────────────────────────────


class TestExport:
    def test_package_shape_and_ids(self, package, workspace):
        assert package["kind"] == "echosphere.tenant.export"
        assert package["schema_version"] == 1
        resources = package["resources"]
        assert resources["tenant"]["id"] == workspace["tenant_id"]
        assert [b["id"] for b in resources["bots"]] == [workspace["bot_id"]]
        assert [w["id"] for w in resources["workflows"]] == [workspace["workflow_id"]]
        bot = resources["bots"][0]
        assert sorted(bot["languages"]) == sorted(workspace["languages"])
        prompt = resources["prompts"][0]
        assert prompt["id"] == workspace["prompt_id"]
        assert {v["version"] for v in prompt["versions"]} == {1, 2}
        assert {c["type"] for c in resources["channel_configs"]} == {"voice", "whatsapp"}
        policy = resources["compliance_policies"][0]
        assert policy["wordings"][0]["id"] == workspace["wording_id"]

    def test_shared_platform_resources_are_separated(self, package, workspace):
        shared = package["shared"]
        assert [p["id"] for p in shared["guardrail_profiles"]] == [workspace["profile_id"]]
        assert [g["id"] for g in shared["guardrails"]] == [workspace["guardrail_id"]]
        assert [v["id"] for v in shared["voice_profiles"]] == [workspace["platform_voice_id"]]
        # The tenant's own cloned voice travels in the tenant section.
        assert [v["id"] for v in package["resources"]["voice_profiles"]] == [
            workspace["cloned_voice_id"]]

    def test_knowledge_plane_is_included(self, package, workspace):
        docs = package["knowledge_plane"]["documents"]
        assert [d["id"] for d in docs] == [workspace["doc_id"]]
        assert docs[0]["chunks"][0]["id"] == workspace["chunk_id"]
        assert len(docs[0]["chunks"][0]["embedding"]) == 1536

    def test_secrets_are_references_only_and_no_operational_data(
            self, package, workspace):
        resources = package["resources"]
        by_id = {a["id"]: a for a in resources["api_connections"]}
        assert by_id[workspace["bot_api_id"]]["secret_ref"] == "secret://orders-api-key"
        voice_cfg = next(c for c in resources["channel_configs"] if c["type"] == "voice")
        assert voice_cfg["config"]["authTokenReference"] == "env:TWILIO_AUTH_TOKEN"
        # No users, phone numbers, conversations or audit history travel along.
        for absent in ("users", "phone_numbers", "conversations", "audit_logs"):
            assert absent not in resources
        # No environment-local state leaks into the package.
        assert "last_test" not in voice_cfg
        assert "password_hash" not in str(package)


# ── Import: create → update → collide ────────────────────────────────────────


class TestImportLifecycle:
    def test_first_import_creates_with_original_ids(
            self, client, workspace, package, super_admin):
        # Simulate the live environment: the tenant graph does not exist yet
        # (the shared platform rows — guardrails, profile, platform voice —
        # remain, as they would on a real live install).
        _purge_tenant_graph([workspace["tenant_id"]])

        report = _data(client.post(
            f"{API}/tenants/import", headers=super_admin, json=package))
        assert report["tenantId"] == workspace["tenant_id"]
        assert report["created"]["bot"] == 1
        assert report["created"]["workflow"] == 1
        assert report["reused"]["guardrail_profile"] == 1
        assert report["reused"]["platform_voice_profile"] == 1
        # The bot's owner user does not exist on live — cleared, not invented.
        assert any("owner" in w for w in report["warnings"])

        session = _db()
        try:
            tenant = session.get(Tenant, workspace["tenant_id"])
            assert tenant is not None and not tenant.is_deleted
            assert tenant.guardrail_profile_id == workspace["profile_id"]

            bot = session.get(VoiceBot, workspace["bot_id"])
            assert bot is not None and bot.name == workspace["bot_name"]
            assert bot.voice_id == workspace["platform_voice_id"]
            assert bot.owner_user_id is None
            langs = set(session.scalars(select(BotLanguage.language_code).where(
                BotLanguage.bot_id == bot.id)))
            assert langs == set(workspace["languages"])

            workflow = session.get(Workflow, workspace["workflow_id"])
            assert workflow.bot_id == workspace["bot_id"]
            assert workflow.nodes[1]["config"]["connectionId"] == workspace["bot_api_id"]

            intent = session.get(Intent, workspace["intent_id"])
            assert intent.workflow_id == workspace["workflow_id"]
            assert intent.api_connection_id == workspace["bot_api_id"]
            assert intent.kb_ids == [workspace["kb_id"]]

            api = session.get(ApiConnection, workspace["bot_api_id"])
            assert api.allowed_intents == [workspace["intent_id"]]
            assert api.secret_ref == "secret://orders-api-key"
            assert api.status == "untested"  # live state is never imported

            versions = session.scalars(select(PromptVersion).where(
                PromptVersion.prompt_id == workspace["prompt_id"])).all()
            assert {v.version for v in versions} == {1, 2}

            schema = session.get(RuntimeContextSchema, workspace["schema_id"])
            assert schema.api_connection_id == workspace["bot_api_id"]

            settings_row = session.get(VoiceBotSetting, workspace["settings_id"])
            assert settings_row.voice_id == workspace["cloned_voice_id"]
            assert settings_row.language_voice_map[workspace["languages"][0]] == (
                workspace["cloned_voice_id"])

            channel = session.get(ChannelConfig, workspace["voice_channel_id"])
            assert channel.config["authTokenReference"] == "env:TWILIO_AUTH_TOKEN"

            wording = session.get(ComplianceWording, workspace["wording_id"])
            assert wording.policy_id == workspace["policy_id"]
        finally:
            session.close()

    async def test_first_import_recreated_knowledge_plane(self, workspace, package):
        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

        async with get_pg_sessionmaker()() as pg:
            doc = await pg.get(KnowledgeDocument, workspace["doc_id"])
            assert doc is not None and doc.kb_id == workspace["kb_id"]
            chunk = await pg.get(KnowledgeChunk, workspace["chunk_id"])
            assert chunk is not None
            assert chunk.content == "Orders ship within 2 business days."
            assert chunk.embedding is not None and len(chunk.embedding) == 1536

    def test_second_import_updates_without_duplicates(
            self, client, workspace, package, super_admin):
        # Drift the live copy, then re-import: the package wins, no duplicates.
        session = _db()
        try:
            bot = session.get(VoiceBot, workspace["bot_id"])
            bot.name = "Renamed on live"
            workflow = session.get(Workflow, workspace["workflow_id"])
            workflow.nodes = [{"id": "n1", "kind": "start"}]
            session.commit()
        finally:
            session.close()

        report = _data(client.post(
            f"{API}/tenants/import", headers=super_admin, json=package))
        assert report["updated"]["bot"] == 1
        assert "bot" not in report["created"]

        session = _db()
        try:
            bots = session.scalars(select(VoiceBot).where(
                VoiceBot.tenant_id == workspace["tenant_id"])).all()
            assert [b.id for b in bots] == [workspace["bot_id"]]
            assert bots[0].name == workspace["bot_name"]
            workflows = session.scalars(select(Workflow).where(
                Workflow.tenant_id == workspace["tenant_id"])).all()
            assert [w.id for w in workflows] == [workspace["workflow_id"]]
            assert len(workflows[0].nodes) == 3
            versions = session.scalars(select(PromptVersion).where(
                PromptVersion.prompt_id == workspace["prompt_id"])).all()
            assert len(versions) == 2
            channels = session.scalars(select(ChannelConfig).where(
                ChannelConfig.tenant_id == workspace["tenant_id"])).all()
            assert len(channels) == 2
            intents = session.scalars(select(Intent).where(
                Intent.tenant_id == workspace["tenant_id"])).all()
            assert len(intents) == 1
        finally:
            session.close()

    def test_foreign_id_collision_is_rejected_and_nothing_written(
            self, client, workspace, package, super_admin):
        # A different tenant claiming ids that live already owns must fail.
        suffix = workspace["suffix"]
        hijack_tid = f"tn_hijack_{suffix}"
        hijacked = copy.deepcopy(package)
        # Re-tenant the package but keep the bot/workflow/… ids, which on
        # live belong to the original tenant.
        text_swap = [
            (workspace["tenant_id"], hijack_tid),
            (f"transfer-{suffix}.example.test", f"hijack-{suffix}.example.test"),
            (f"tr_{suffix}", f"hj_{suffix}"),
        ]

        def swap(value):
            if isinstance(value, str):
                for old, new in text_swap:
                    if value == old:
                        return new
                return value
            if isinstance(value, list):
                return [swap(v) for v in value]
            if isinstance(value, dict):
                return {k: swap(v) for k, v in value.items()}
            return value

        hijacked = swap(hijacked)
        hijacked["resources"]["tenant"]["name"] = f"Hijack {suffix}"

        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=hijacked)
        assert response.status_code == 409, response.text
        assert "collision" in response.json()["message"].lower()

        session = _db()
        try:
            # Transactional: the hijacking tenant was rolled back entirely.
            assert session.get(Tenant, hijack_tid) is None
            bot = session.get(VoiceBot, workspace["bot_id"])
            assert bot.tenant_id == workspace["tenant_id"]
        finally:
            session.close()

    def test_same_ids_under_different_existing_tenant_are_rejected(
            self, client, workspace, package, super_admin):
        # Even a package naming an EXISTING other tenant cannot steal rows.
        stolen = copy.deepcopy(package)
        bot_row = stolen["resources"]["bots"][0]
        bot_row["id"] = workspace["foreign_bot_id"]
        for section in ("voice_bot_settings", "prompts", "workflows", "intents",
                        "test_scenarios", "runtime_context_schemas",
                        "channel_configs", "api_connections", "knowledge_sources"):
            for row in stolen["resources"].get(section) or []:
                if row.get("bot_id") == workspace["bot_id"]:
                    row["bot_id"] = workspace["foreign_bot_id"]
        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=stolen)
        assert response.status_code == 409, response.text

        session = _db()
        try:
            foreign_bot = session.get(VoiceBot, workspace["foreign_bot_id"])
            assert foreign_bot.tenant_id == workspace["foreign_tenant_id"]
            assert foreign_bot.name == f"Transfer Foreign Bot {workspace['suffix']}"
        finally:
            session.close()


# ── Guardrail provisioning on import ─────────────────────────────────────────


def _purge_shared_guardrails(workspace) -> None:
    """Simulate a live environment that has never seen this guardrail set."""
    session = _db()
    try:
        session.execute(delete(GuardrailProfileRule).where(
            GuardrailProfileRule.profile_id == workspace["profile_id"]))
        session.execute(delete(GuardrailProfile).where(
            GuardrailProfile.id == workspace["profile_id"]))
        session.execute(delete(Guardrail).where(
            Guardrail.id == workspace["guardrail_id"]))
        session.commit()
    finally:
        session.close()


class TestGuardrailProvisioning:
    def test_missing_guardrail_is_created_with_same_id_rules_and_assignment(
            self, client, workspace, package, super_admin):
        _purge_shared_guardrails(workspace)

        report = _data(client.post(
            f"{API}/tenants/import", headers=super_admin, json=package))
        assert report["created"]["guardrail"] == 1
        assert report["created"]["guardrail_profile"] == 1
        assert report["remappedIds"] == {}  # exported ids used verbatim

        session = _db()
        try:
            suffix = workspace["suffix"]
            # The rule row is recreated with its exact id and configuration.
            guardrail = session.get(Guardrail, workspace["guardrail_id"])
            assert guardrail is not None and not guardrail.is_deleted
            assert guardrail.code == f"gtest_{suffix}"
            assert guardrail.enforcement == "block" and guardrail.enabled

            # The profile keeps its id, metadata and rule membership.
            profile = session.get(GuardrailProfile, workspace["profile_id"])
            assert profile is not None and not profile.is_deleted
            assert profile.code == f"gp_{suffix}"
            assert profile.status == "active" and profile.version == 3
            rules = session.scalars(select(GuardrailProfileRule).where(
                GuardrailProfileRule.profile_id == profile.id)).all()
            assert [r.guardrail_id for r in rules] == [workspace["guardrail_id"]]

            # And it is assigned back exactly as on local: tenant AND bot.
            tenant = session.get(Tenant, workspace["tenant_id"])
            assert tenant.guardrail_profile_id == workspace["profile_id"]
            bot = session.get(VoiceBot, workspace["bot_id"])
            assert bot.guardrail_profile_id == workspace["profile_id"]
        finally:
            session.close()

    def test_repeated_import_reuses_the_created_guardrails_without_duplicates(
            self, client, workspace, package, super_admin):
        report = _data(client.post(
            f"{API}/tenants/import", headers=super_admin, json=package))
        assert report["reused"]["guardrail"] == 1
        assert report["reused"]["guardrail_profile"] == 1
        assert "guardrail" not in report["created"]
        assert "guardrail_profile" not in report["created"]

        session = _db()
        try:
            suffix = workspace["suffix"]
            profiles = session.scalars(select(GuardrailProfile).where(
                GuardrailProfile.code == f"gp_{suffix}")).all()
            assert [p.id for p in profiles] == [workspace["profile_id"]]
            guardrails = session.scalars(select(Guardrail).where(
                Guardrail.code == f"gtest_{suffix}")).all()
            assert [g.id for g in guardrails] == [workspace["guardrail_id"]]
            rules = session.scalars(select(GuardrailProfileRule).where(
                GuardrailProfileRule.profile_id == workspace["profile_id"])).all()
            assert len(rules) == 1
        finally:
            session.close()

    def test_conflicting_profile_id_is_rejected_without_partial_changes(
            self, client, workspace, package, super_admin):
        # Live's profile keeps the imported id but now represents a DIFFERENT
        # profile (different code) — the import must not adopt or overwrite it.
        suffix = workspace["suffix"]
        session = _db()
        try:
            session.get(GuardrailProfile, workspace["profile_id"]).code = f"other_{suffix}"
            session.commit()
        finally:
            session.close()

        # Force the package to also CREATE a brand-new guardrail first, to
        # prove the rollback discards everything written before the collision.
        conflicted = copy.deepcopy(package)
        fresh_guardrail_id = f"gr_fresh_{suffix}"
        rule_row = conflicted["shared"]["guardrails"][0]
        rule_row["id"] = fresh_guardrail_id
        rule_row["code"] = f"gfresh_{suffix}"
        rule_row["name"] = f"Fresh rule {suffix}"
        for profile_row in conflicted["shared"]["guardrail_profiles"]:
            for rule in profile_row["rules"]:
                rule["guardrail_id"] = fresh_guardrail_id

        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=conflicted)
        assert response.status_code == 409, response.text
        assert "guardrail profile" in response.json()["message"]

        session = _db()
        try:
            # Rolled back: the pre-collision guardrail insert did not survive,
            # and the live profile was not overwritten.
            assert session.get(Guardrail, fresh_guardrail_id) is None
            profile = session.get(GuardrailProfile, workspace["profile_id"])
            assert profile.code == f"other_{suffix}"
            profile.code = f"gp_{suffix}"  # restore for the remaining tests
            session.commit()
        finally:
            session.close()

    def test_conflicting_guardrail_id_is_rejected(
            self, client, workspace, package, super_admin):
        # Same id, but on live it is a logically different rule.
        suffix = workspace["suffix"]
        session = _db()
        try:
            live = session.get(Guardrail, workspace["guardrail_id"])
            live.code = f"gother_{suffix}"
            session.commit()
        finally:
            session.close()

        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=package)
        assert response.status_code == 409, response.text
        assert workspace["guardrail_id"] in response.json()["message"]

        session = _db()
        try:
            live = session.get(Guardrail, workspace["guardrail_id"])
            assert live.code == f"gother_{suffix}"  # untouched
            live.code = f"gtest_{suffix}"  # restore for the remaining tests
            session.commit()
        finally:
            session.close()


# ── Secret hygiene on import ──────────────────────────────────────────────────


class TestSecretHygiene:
    def test_raw_api_secret_is_rejected(self, client, package, super_admin):
        tampered = copy.deepcopy(package)
        tampered["resources"]["api_connections"][0]["secret_ref"] = "sk-live-RAW"
        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=tampered)
        assert response.status_code == 422, response.text
        assert "secret" in response.json()["message"].lower()

    def test_raw_channel_credential_is_rejected(self, client, package, super_admin):
        tampered = copy.deepcopy(package)
        channel = next(c for c in tampered["resources"]["channel_configs"]
                       if c["type"] == "voice")
        channel["config"]["authTokenReference"] = "raw-twilio-token"
        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=tampered)
        assert response.status_code == 422, response.text

    def test_unsupported_schema_version_is_rejected(self, client, package, super_admin):
        future = copy.deepcopy(package)
        future["schema_version"] = 99
        response = client.post(
            f"{API}/tenants/import", headers=super_admin, json=future)
        assert response.status_code == 422, response.text
        assert "schema_version" in response.json()["message"]
