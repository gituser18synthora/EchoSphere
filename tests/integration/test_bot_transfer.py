"""Bot Export / Import: GET /bots/{id}/export, POST /bots/import/preview,
POST /bots/import.

The package moves ONE bot between environments preserving tenant_id + bot_id.
Verified here against the real local databases (own uniquely-named rows only):

1. export shape — identity, sections, shared/environment separation, no
   secrets, no environment-local metrics, integrity seal;
2. new-bot import creates the bot with the exported ids and the exact same
   configuration (re-export equals the package);
3. existing-bot import updates in place, removes stale bot-owned rows, never
   duplicates the bot;
4. wrong tenant / tampered / corrupt packages are rejected and nothing changes;
5. environment-specific values: a live channel + phone number are preserved,
   local URLs never overwrite live URLs, unresolvable secrets are reported;
6. shared resources are reused or created, never modified; a preview writes
   nothing.
"""

import copy
import json
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
    EntityDef,
    Guardrail,
    GuardrailProfile,
    GuardrailProfileRule,
    Intent,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    PromptVersion,
    Release,
    Role,
    RuntimeContextSchema,
    SupportedLanguage,
    Tenant,
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


def _bearer_for(email: str) -> dict:
    session = _db()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


@pytest.fixture(scope="module")
def super_admin():
    return _bearer_for("admin@aurexion.com")


def _data(response, expected: int = 200):
    assert response.status_code == expected, response.text
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def _error(response, expected: int) -> str:
    assert response.status_code == expected, response.text
    body = response.json()
    assert body.get("success") is False, body
    return body.get("message") or ""


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _reseal(package: dict) -> dict:
    """Tests that legitimately restructure a package re-seal it the way a
    fresh export would; tampering tests leave the stale seal in place."""
    from backend.core.bot_transfer import seal_package

    return seal_package(package)


def _purge_bot_graph(bot_ids: list[str]) -> None:
    """Hard-delete a bot's MySQL rows (test rows only) — the 'live has no such
    bot yet' starting point."""
    session = _db()
    try:
        prompt_ids = session.scalars(
            select(Prompt.id).where(Prompt.bot_id.in_(bot_ids))).all()
        if prompt_ids:
            session.execute(delete(PromptVersion).where(
                PromptVersion.prompt_id.in_(prompt_ids)))
        session.execute(delete(BotLanguage).where(BotLanguage.bot_id.in_(bot_ids)))
        session.execute(delete(VoiceBotReadiness).where(
            VoiceBotReadiness.bot_id.in_(bot_ids)))
        session.execute(delete(PhoneNumber).where(PhoneNumber.bot_id.in_(bot_ids)))
        for model in (RuntimeContextSchema, Prompt, Intent, ApiConnection, Workflow,
                      TestScenario, KnowledgeSource, ChannelConfig, VoiceBotSetting,
                      Release):
            session.execute(delete(model).where(model.bot_id.in_(bot_ids)))
        session.execute(delete(VoiceBot).where(VoiceBot.id.in_(bot_ids)))
        session.commit()
    finally:
        session.close()


def _purge_tenant_graph(tenant_ids: list[str]) -> None:
    session = _db()
    try:
        bot_ids = session.scalars(
            select(VoiceBot.id).where(VoiceBot.tenant_id.in_(tenant_ids))).all()
        session.close()
        if bot_ids:
            _purge_bot_graph(bot_ids)
        session = _db()
        for model in (EntityDef, ApiConnection, VoiceProfile, PhoneNumber, AuditLog,
                      User, KnowledgeSource):
            session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
    finally:
        session.close()


async def _purge_knowledge_plane(kb_ids: list[str]) -> None:
    from sqlalchemy import delete as sa_delete

    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

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
    """Source tenant ('local') with one fully configured bot, a foreign tenant
    with its own admin, and the shared platform rows the bot references."""
    suffix = uuid.uuid4().hex[:10]
    session = _db()
    try:
        admin_role = session.execute(
            select(Role).where(Role.code == "tenant_admin")).scalar_one()

        guardrail = Guardrail(
            id=new_id("gr"), code=f"gbot_{suffix}", name=f"No profanity bot {suffix}",
            category="conduct", enforcement="block", enabled=True,
        )
        session.add(guardrail)
        session.flush()
        profile = GuardrailProfile(
            id=new_id("gp"), code=f"gpb_{suffix}", name=f"Bot transfer profile {suffix}",
            status="active", version=1,
        )
        session.add(profile)
        session.flush()
        session.add(GuardrailProfileRule(
            id=new_id("gpr"), profile_id=profile.id, guardrail_id=guardrail.id))
        platform_voice = VoiceProfile(
            id=new_id("vp"), tenant_id=None, source="platform",
            name=f"Bot Transfer Platform Voice {suffix}", gender="female",
            provider="sarvam", provider_voice_id="anushka", status="active",
        )
        session.add(platform_voice)

        tenant = Tenant(
            id=new_id("tn"), name=f"Bot Transfer Source {suffix}",
            code=f"bt_{suffix}", domain=f"bot-transfer-{suffix}.example.test",
            status="active", guardrail_profile_id=profile.id,
        )
        foreign_tenant = Tenant(
            id=new_id("tn"), name=f"Bot Transfer Foreign {suffix}",
            code=f"btf_{suffix}", domain=f"bot-transfer-f-{suffix}.example.test",
            status="active",
        )
        session.add_all([tenant, foreign_tenant])
        session.flush()

        admin = User(
            id=new_id("usr"), email=f"bt.admin.{suffix}@example.test",
            name="Bot Transfer Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=tenant.id, status="active",
        )
        foreign_admin = User(
            id=new_id("usr"), email=f"bt.foreign.{suffix}@example.test",
            name="Foreign Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=foreign_tenant.id, status="active",
        )
        session.add_all([admin, foreign_admin])
        session.flush()

        cloned_voice = VoiceProfile(
            id=new_id("vp"), tenant_id=tenant.id, source="cloned",
            name=f"Bot Brand Voice {suffix}", gender="male",
            provider="elevenlabs", provider_voice_id=f"clone_{suffix}", status="active",
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
            live_version="v1.2.0", published_at=datetime(2026, 9, 1, 10, 0),
            voice_id=platform_voice.id, guardrail_profile_id=profile.id,
            owner_user_id=admin.id, description="Handles order questions",
        )
        other_bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Sibling Bot {suffix}",
            status="draft",
        )
        session.add_all([bot, other_bot])
        session.flush()
        for code in languages:
            session.add(BotLanguage(bot_id=bot.id, language_code=code))
        session.add(VoiceBotReadiness(
            id=new_id("rd"), bot_id=bot.id, item_key="r1",
            label="Knowledge sources indexed", done=True, studio_tab="knowledge",
            sort_order=0,
        ))
        session.add(VoiceBotReadiness(
            id=new_id("rd"), bot_id=bot.id, item_key="r6",
            label="Channel connected", done=True, studio_tab="channels", sort_order=5,
        ))

        settings_row = VoiceBotSetting(
            id=new_id("vbs"), bot_id=bot.id, tenant_id=tenant.id,
            voice_id=cloned_voice.id, speed=1.1, pause_ms=300,
            language_voice_map={"default": languages[0], languages[0]: cloned_voice.id},
            stt_provider="sarvam", tts_provider="elevenlabs",
            llm_provider="openai", llm_model="gpt-4o-mini",
            goal_policy={"role": "Order assistant", "summaryFields": [{"key": "resolved"}]},
            human_speech={"fillerWords": False},
        )
        session.add(settings_row)

        bot_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=bot.id,
            name="Fetch order", method="GET",
            url="https://orders.example.test/{{id}}",
            auth_type="api_key", secret_ref="secret://bt-orders-api-key",
            status="healthy", last_latency_ms=120,
        )
        shared_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=None,
            name=f"Shared CRM {suffix}", method="GET",
            url="https://crm.example.test/{{id}}",
        )
        unrelated_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=None,
            name=f"Unrelated tenant tool {suffix}", method="GET",
            url="https://other.example.test/",
        )
        session.add_all([bot_api, shared_api, unrelated_api])

        kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=bot.id, scope="bot",
            type="document", name=f"Order FAQ {suffix}", status="indexed",
            chunks=1, size_kb=4,
        )
        tenant_kb = KnowledgeSource(
            id=new_id("ks"), tenant_id=tenant.id, bot_id=None, scope="tenant",
            type="document", name=f"Tenant policies {suffix}", status="indexed",
        )
        session.add_all([kb, tenant_kb])
        session.flush()

        workflow = Workflow(
            id=new_id("wf"), tenant_id=tenant.id, bot_id=bot.id,
            name="Order journey", version=2, status="approved",
            nodes=[{"id": "n1", "kind": "start"},
                   {"id": "n2", "kind": "api", "config": {"connectionId": bot_api.id}},
                   {"id": "n3", "kind": "api", "config": {"connectionId": shared_api.id}},
                   {"id": "n4", "kind": "end"}],
            edges=[{"id": "e1", "from": "n1", "to": "n2"},
                   {"id": "e2", "from": "n2", "to": "n3"},
                   {"id": "e3", "from": "n3", "to": "n4"}],
            issues=[],
        )
        session.add(workflow)

        prompt = Prompt(
            id=new_id("pr"), tenant_id=tenant.id, bot_id=bot.id, type="system",
            name="Core prompt", state="published", active_version=2, published_version=2,
        )
        greeting = Prompt(
            id=new_id("pr"), tenant_id=tenant.id, bot_id=bot.id, type="greeting",
            name="Greeting", state="published", active_version=1, published_version=1,
        )
        session.add_all([prompt, greeting])
        session.flush()
        session.add_all([
            PromptVersion(id=new_id("prv"), prompt_id=prompt.id, version=1,
                          compiled_prompt="You are v1.", prompt_mode="full",
                          full_prompt="You are v1."),
            PromptVersion(id=new_id("prv"), prompt_id=prompt.id, version=2,
                          compiled_prompt="You are v2.", prompt_mode="full",
                          full_prompt="You are v2."),
            PromptVersion(id=new_id("prv"), prompt_id=greeting.id, version=1,
                          compiled_prompt="Namaste!",
                          variants=[{"language": "hi-IN", "content": "नमस्ते"}]),
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
            route=f"workflow:{workflow.id}",
            api_connection_id=shared_api.id, kb_ids=[kb.id, tenant_kb.id],
            test_pass=7, test_total=9, avg_confidence_30d=0.88,
        )
        session.add(intent)
        session.flush()
        bot_api.allowed_intents = [intent.id]
        bot_api.allowed_workflows = [workflow.id]

        scenario = TestScenario(
            id=new_id("ts"), tenant_id=tenant.id, bot_id=bot.id,
            name="Happy path", suite="Regression", steps=3, last_run={"pass": True},
        )
        session.add(scenario)

        schema = RuntimeContextSchema(
            id=new_id("rcs"), tenant_id=tenant.id, bot_id=bot.id,
            name="Customer details", source_mode="api", api_connection_id=bot_api.id,
            fields=[{"key": "name", "type": "string"}],
        )
        session.add(schema)

        release = Release(
            id=new_id("rel"), tenant_id=tenant.id, bot_id=bot.id, version="v1.2.0",
            stage="published", notes="Initial", requested_by="Bot Transfer Admin",
            approved_by="Bot Transfer Admin", published_at=datetime(2026, 9, 1, 10, 0),
            checklist=[{"key": "r1", "done": True}],
        )
        session.add(release)

        voice_channel = ChannelConfig(
            id=new_id("ch"), tenant_id=tenant.id, bot_id=bot.id, type="voice",
            status="live", enabled=True, workflow_name="Order journey",
            config={"phoneNumber": "+14155550101", "telephonyProvider": "twilio",
                    "publicWsBase": "wss://media.local.example.test",
                    "authTokenReference": "env:BT_TWILIO_TOKEN"},
            last_test={"ok": True},
        )
        session.add(voice_channel)
        number = PhoneNumber(
            id=new_id("pn"), number="+14155550101", country="US", tenant_id=tenant.id,
            bot_id=bot.id, provider="twilio", status="assigned",
        )
        session.add(number)
        session.commit()

        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

        doc_id = new_id("kdoc")
        chunk_id = new_id("chk")
        async with get_pg_sessionmaker()() as pg:
            pg.add(KnowledgeDocument(
                id=doc_id, tenant_id=tenant.id, kb_id=kb.id, file_name="faq.txt",
                file_ext="txt", mime_type="text/plain", size_bytes=64,
                content_hash="h" * 64, status="completed", chunk_count=1,
            ))
            await pg.flush()
            pg.add(KnowledgeChunk(
                id=chunk_id, tenant_id=tenant.id, kb_id=kb.id, document_id=doc_id,
                chunk_index=0, content="Orders ship within 2 business days.",
                content_hash="c" * 64, embedding=[0.25] * 1536,
                embedding_model="mock", embedding_dimension=1536,
            ))
            await pg.commit()

        def bearer(u: User) -> dict:
            return {"Authorization": f"Bearer {create_access_token(user_id=u.id, role='tenant_admin', tenant_id=u.tenant_id)}"}

        yield {
            "suffix": suffix,
            "tenant_id": tenant.id,
            "foreign_tenant_id": foreign_tenant.id,
            "bot_id": bot.id,
            "bot_name": bot.name,
            "other_bot_id": other_bot.id,
            "admin_id": admin.id,
            "tenant_admin": bearer(admin),
            "foreign_admin": bearer(foreign_admin),
            "languages": languages,
            "profile_id": profile.id,
            "guardrail_id": guardrail.id,
            "platform_voice_id": platform_voice.id,
            "cloned_voice_id": cloned_voice.id,
            "settings_id": settings_row.id,
            "workflow_id": workflow.id,
            "prompt_id": prompt.id,
            "greeting_id": greeting.id,
            "intent_id": intent.id,
            "entity_id": entity.id,
            "bot_api_id": bot_api.id,
            "shared_api_id": shared_api.id,
            "shared_api_name": shared_api.name,
            "unrelated_api_id": unrelated_api.id,
            "kb_id": kb.id,
            "tenant_kb_id": tenant_kb.id,
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "schema_id": schema.id,
            "scenario_id": scenario.id,
            "release_id": release.id,
            "voice_channel_id": voice_channel.id,
            "number_id": number.id,
        }
    finally:
        session.rollback()
        session.close()
        await _purge_knowledge_plane([kb.id])
        _purge_tenant_graph([tenant.id, foreign_tenant.id])
        cleanup = _db()
        try:
            cleanup.execute(delete(PhoneNumber).where(
                PhoneNumber.number.in_(["+14155550101", "+14155550199", "+14155550177"])))
            cleanup.execute(delete(GuardrailProfileRule).where(
                GuardrailProfileRule.profile_id == profile.id))
            cleanup.execute(delete(GuardrailProfile).where(GuardrailProfile.id == profile.id))
            cleanup.execute(delete(Guardrail).where(Guardrail.id == guardrail.id))
            cleanup.execute(delete(VoiceProfile).where(VoiceProfile.id == platform_voice.id))
            cleanup.commit()
        finally:
            cleanup.close()


@pytest.fixture(scope="module")
def package(client, workspace):
    """The package exported by the bot's own tenant admin — the file copied
    from local to live."""
    return _data(client.get(f"{API}/bots/{workspace['bot_id']}/export",
                            headers=workspace["tenant_admin"]))


def _import(client, headers, pkg, expected=200, **params):
    query = "&".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                     for k, v in params.items())
    response = client.post(f"{API}/bots/import{'?' + query if query else ''}",
                           json=pkg, headers=headers)
    return _data(response, expected) if expected == 200 else _error(response, expected)


def _preview(client, headers, pkg, expected=200, **params):
    query = "&".join(f"{k}={str(v).lower() if isinstance(v, bool) else v}"
                     for k, v in params.items())
    response = client.post(f"{API}/bots/import/preview{'?' + query if query else ''}",
                           json=pkg, headers=headers)
    return _data(response, expected) if expected == 200 else _error(response, expected)


def _restore(client, workspace, package) -> dict:
    """Bring the bot back to exactly the exported state (import is idempotent)."""
    return _import(client, workspace["tenant_admin"], copy.deepcopy(package))


def _snapshot(bot_id: str) -> dict:
    """Canonical view of a bot's configuration plane for before/after checks."""
    session = _db()
    try:
        bot = session.get(VoiceBot, bot_id)
        return {
            "bot": (bot.name, bot.status, bot.live_version, bot.description) if bot else None,
            "workflows": sorted((w.id, w.name, w.version) for w in session.scalars(
                select(Workflow).where(Workflow.bot_id == bot_id, Workflow.is_deleted.is_(False)))),
            "prompts": sorted((p.id, p.state) for p in session.scalars(
                select(Prompt).where(Prompt.bot_id == bot_id, Prompt.is_deleted.is_(False)))),
            "intents": sorted((i.id, i.name) for i in session.scalars(
                select(Intent).where(Intent.bot_id == bot_id, Intent.is_deleted.is_(False)))),
            "channels": sorted((c.type, _canonical(c.config), c.enabled) for c in session.scalars(
                select(ChannelConfig).where(ChannelConfig.bot_id == bot_id))),
            "numbers": sorted((n.number, n.status) for n in session.scalars(
                select(PhoneNumber).where(PhoneNumber.bot_id == bot_id))),
        }
    finally:
        session.close()


# ── Access control ────────────────────────────────────────────────────────────


class TestAccessControl:
    def test_export_requires_authentication(self, client, workspace):
        assert client.get(f"{API}/bots/{workspace['bot_id']}/export").status_code == 401

    def test_export_is_tenant_scoped(self, client, workspace):
        # Another tenant's admin sees a 404 — never a package, never a hint.
        response = client.get(f"{API}/bots/{workspace['bot_id']}/export",
                              headers=workspace["foreign_admin"])
        assert response.status_code == 404, response.text

    def test_super_admin_can_export(self, client, workspace, super_admin):
        pkg = _data(client.get(f"{API}/bots/{workspace['bot_id']}/export",
                               headers=super_admin))
        assert pkg["bot_id"] == workspace["bot_id"]

    def test_import_requires_authentication(self, client, package):
        assert client.post(f"{API}/bots/import", json=package).status_code == 401


# ── Export ────────────────────────────────────────────────────────────────────


class TestExport:
    def test_identity_and_sections(self, package, workspace):
        assert package["kind"] == "echosphere.bot.export"
        assert package["schema_version"] == 1
        assert package["tenant_id"] == workspace["tenant_id"]
        assert package["bot_id"] == workspace["bot_id"]
        assert package["bot"]["id"] == workspace["bot_id"]
        assert package["bot"]["tenant_id"] == workspace["tenant_id"]
        assert package["bot"]["name"] == workspace["bot_name"]
        assert package["bot"]["languages"] == sorted(workspace["languages"])
        assert {r["item_key"] for r in package["bot"]["readiness"]} == {"r1", "r6"}
        assert package["integrity"].startswith("sha256:")
        assert package["source"]["bot_name"] == workspace["bot_name"]

        resources = package["resources"]
        assert resources["voice_bot_settings"]["id"] == workspace["settings_id"]
        assert resources["voice_bot_settings"]["human_speech"] == {"fillerWords": False}
        assert {p["id"] for p in resources["prompts"]} == {
            workspace["prompt_id"], workspace["greeting_id"]}
        core = next(p for p in resources["prompts"] if p["id"] == workspace["prompt_id"])
        assert sorted(v["version"] for v in core["versions"]) == [1, 2]
        assert [w["id"] for w in resources["workflows"]] == [workspace["workflow_id"]]
        assert [i["id"] for i in resources["intents"]] == [workspace["intent_id"]]
        assert [a["id"] for a in resources["api_connections"]] == [workspace["bot_api_id"]]
        assert [k["id"] for k in resources["knowledge_sources"]] == [workspace["kb_id"]]
        assert [t["id"] for t in resources["test_scenarios"]] == [workspace["scenario_id"]]
        assert resources["runtime_context_schema"]["id"] == workspace["schema_id"]
        assert [r["id"] for r in resources["releases"]] == [workspace["release_id"]]
        assert resources["releases"][0]["stage"] == "published"

    def test_shared_resources_are_separated_and_reference_only_for_tenant_kbs(
        self, package, workspace,
    ):
        shared = package["shared"]
        assert [p["id"] for p in shared["guardrail_profiles"]] == [workspace["profile_id"]]
        assert [g["id"] for g in shared["guardrails"]] == [workspace["guardrail_id"]]
        assert [v["id"] for v in shared["voice_profiles"]] == [workspace["platform_voice_id"]]
        assert [v["id"] for v in shared["tenant_voice_profiles"]] == [workspace["cloned_voice_id"]]
        assert [e["id"] for e in shared["entity_defs"]] == [workspace["entity_id"]]
        # Only the tenant-wide tool the bot references; not every tenant tool.
        assert [a["id"] for a in shared["api_connections"]] == [workspace["shared_api_id"]]
        assert workspace["unrelated_api_id"] not in _canonical(package)
        assert shared["knowledge_sources"] == [{
            "id": workspace["tenant_kb_id"], "tenant_id": workspace["tenant_id"],
            "name": f"Tenant policies {workspace['suffix']}", "scope": "tenant",
            "reference_only": True,
        }]

    def test_environment_section_and_knowledge_plane(self, package, workspace):
        env = package["environment"]
        assert [c["id"] for c in env["channel_configs"]] == [workspace["voice_channel_id"]]
        assert env["channel_configs"][0]["config"]["phoneNumber"] == "+14155550101"
        assert env["phone_numbers"] == [{"number": "+14155550101", "country": "US",
                                         "provider": "twilio", "status": "assigned"}]
        docs = package["knowledge_plane"]["documents"]
        assert [d["id"] for d in docs] == [workspace["doc_id"]]
        assert [c["id"] for c in docs[0]["chunks"]] == [workspace["chunk_id"]]
        assert len(docs[0]["chunks"][0]["embedding"]) == 1536

    def test_no_secrets_and_no_environment_local_metrics(self, package, workspace):
        text = _canonical(package)
        assert "secret://bt-orders-api-key" in text
        assert "env:BT_TWILIO_TOKEN" in text
        bot_api = package["resources"]["api_connections"][0]
        for key in ("status", "last_tested_at", "last_latency_ms"):
            assert key not in bot_api
        intent = package["resources"]["intents"][0]
        for key in ("test_pass", "test_total", "avg_confidence_30d"):
            assert key not in intent
        for key in ("health", "containment", "csat", "avg_cost_per_call",
                    "created_at", "updated_at", "is_deleted"):
            assert key not in package["bot"]
        assert "last_test" not in package["environment"]["channel_configs"][0]
        assert "last_run" not in package["resources"]["test_scenarios"][0]
        assert "done" not in package["bot"]["readiness"][0]

    def test_export_without_knowledge(self, client, workspace):
        pkg = _data(client.get(
            f"{API}/bots/{workspace['bot_id']}/export?includeKnowledge=false",
            headers=workspace["tenant_admin"]))
        assert pkg["knowledge_plane"] is None
        assert pkg["integrity"].startswith("sha256:")


# ── Import: create / update ───────────────────────────────────────────────────


class TestImportCreate:
    async def test_new_bot_import_recreates_everything_with_the_same_ids(
        self, client, workspace, package,
    ):
        # "Live" does not have the bot yet: remove the bot graph (tenant,
        # shared tools, entity, voices stay — they are tenant-level).
        await _purge_knowledge_plane([workspace["kb_id"]])
        _purge_bot_graph([workspace["bot_id"]])
        session = _db()
        try:
            assert session.get(VoiceBot, workspace["bot_id"]) is None
        finally:
            session.close()

        preview = _preview(client, workspace["tenant_admin"], copy.deepcopy(package))
        assert preview["existing"] is False and preview["action"] == "create"
        assert preview["dryRun"] is True
        assert preview["created"]["bot"] == 1
        session = _db()
        try:
            assert session.get(VoiceBot, workspace["bot_id"]) is None  # preview wrote nothing
        finally:
            session.close()

        report = _import(client, workspace["tenant_admin"], copy.deepcopy(package))
        assert report["botId"] == workspace["bot_id"]
        assert report["tenantId"] == workspace["tenant_id"]
        assert report["action"] == "create" and report["existing"] is False
        assert report["created"]["bot"] == 1
        assert report["created"]["workflow"] == 1
        assert report["created"]["prompt"] == 2
        assert report["created"]["prompt_version"] == 3
        assert report["created"]["intent"] == 1
        assert report["created"]["api_connection"] == 1
        assert report["created"]["knowledge_source"] == 1
        assert report["created"]["test_scenario"] == 1
        assert report["created"]["release"] == 1
        assert report["created"]["voice_bot_settings"] == 1
        assert report["created"]["runtime_context_schema"] == 1
        assert report["created"]["channel_config"] == 1
        assert "phone_number" not in report["created"]
        assert report["knowledgeDocuments"] == 1
        # Shared rows still exist on this environment → reused, not created.
        assert report["reused"]["guardrail_profile"] == 1
        assert report["reused"]["tenant_voice_profile"] == 1
        assert report["reused"]["entity_def"] == 1
        assert report["reused"]["tenant_api_connection"] == 1
        assert report["reused"]["knowledge_source"] == 1

        session = _db()
        try:
            bot = session.get(VoiceBot, workspace["bot_id"])
            assert bot is not None and not bot.is_deleted
            assert bot.tenant_id == workspace["tenant_id"]
            assert bot.name == workspace["bot_name"]
            assert bot.status == "published" and bot.live_version == "v1.2.0"
            assert bot.owner_user_id == workspace["admin_id"]
            assert sorted(l.language_code for l in bot.languages) == sorted(workspace["languages"])
            assert {r.item_key for r in bot.readiness_items} == {"r1", "r6"}
            assert session.get(Workflow, workspace["workflow_id"]).bot_id == workspace["bot_id"]
            assert session.get(Prompt, workspace["prompt_id"]).published_version == 2
            versions = session.scalars(select(PromptVersion).where(
                PromptVersion.prompt_id == workspace["prompt_id"])).all()
            assert sorted(v.version for v in versions) == [1, 2]
            intent = session.get(Intent, workspace["intent_id"])
            assert intent.workflow_id == workspace["workflow_id"]
            assert intent.api_connection_id == workspace["shared_api_id"]
            assert intent.kb_ids == [workspace["kb_id"], workspace["tenant_kb_id"]]
            assert intent.test_pass == 0 and intent.test_total == 0  # local state not copied
            api = session.get(ApiConnection, workspace["bot_api_id"])
            assert api.status == "untested" and api.secret_ref == "secret://bt-orders-api-key"
            assert session.get(VoiceBotSetting, workspace["settings_id"]).speed == 1.1
            assert session.get(RuntimeContextSchema, workspace["schema_id"]).api_connection_id == workspace["bot_api_id"]
            assert session.get(Release, workspace["release_id"]).stage == "published"
            assert session.get(TestScenario, workspace["scenario_id"]).last_run is None
            channel = session.get(ChannelConfig, workspace["voice_channel_id"])
            # Environment section: created but disabled, number NOT claimed.
            assert channel.enabled is False and channel.status == "configured"
            assert channel.config["phoneNumber"] == "+14155550101"
            assert session.scalar(select(PhoneNumber).where(
                PhoneNumber.number == "+14155550101")) is None
        finally:
            session.close()
        assert any("imported DISABLED" in w for w in report["warnings"])
        assert any(s["reference"] == "secret://bt-orders-api-key" for s in report["secretsMissing"])
        assert any(s["reference"] == "env:BT_TWILIO_TOKEN" for s in report["secretsMissing"])

        from shared.db.postgres import get_pg_sessionmaker
        from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

        async with get_pg_sessionmaker()() as pg:
            doc = await pg.get(KnowledgeDocument, workspace["doc_id"])
            assert doc is not None and doc.kb_id == workspace["kb_id"]
            chunk = await pg.get(KnowledgeChunk, workspace["chunk_id"])
            assert chunk is not None and chunk.content.startswith("Orders ship")

        # Re-exporting the imported bot yields the same configuration plane.
        again = _data(client.get(f"{API}/bots/{workspace['bot_id']}/export",
                                 headers=workspace["tenant_admin"]))
        assert _canonical(again["bot"]) == _canonical(package["bot"])
        assert _canonical(again["resources"]) == _canonical(package["resources"])
        assert _canonical(again["shared"]) == _canonical(package["shared"])
        assert _canonical(again["knowledge_plane"]) == _canonical(package["knowledge_plane"])
        # Only the environment section differs (channel disabled, no number).
        assert again["environment"]["phone_numbers"] == []

    def test_reimport_is_idempotent(self, client, workspace, package):
        report = _restore(client, workspace, package)
        assert report["action"] == "update" and report["existing"] is True
        assert report["updated"]["bot"] == 1
        assert not report["removed"]
        session = _db()
        try:
            bots = session.scalars(select(VoiceBot).where(
                VoiceBot.tenant_id == workspace["tenant_id"],
                VoiceBot.name == workspace["bot_name"])).all()
            assert len(bots) == 1
        finally:
            session.close()


class TestImportUpdate:
    def test_existing_bot_is_updated_and_stale_rows_reconciled(
        self, client, workspace, package,
    ):
        _restore(client, workspace, package)
        pkg = copy.deepcopy(package)
        pkg["bot"]["description"] = "Handles order AND refund questions"
        pkg["resources"]["workflows"][0]["name"] = "Order journey v3"
        pkg["resources"]["workflows"][0]["version"] = 3
        pkg["resources"]["workflows"][0]["nodes"].append({"id": "n9", "kind": "say"})
        # Prompt deleted locally → must disappear on live.
        pkg["resources"]["prompts"] = [
            p for p in pkg["resources"]["prompts"] if p["id"] != workspace["greeting_id"]]
        # New prompt version + republish.
        core = pkg["resources"]["prompts"][0]
        new_version_id = new_id("prv")
        core["versions"].append({"id": new_version_id, "prompt_id": core["id"], "version": 3,
                                 "compiled_prompt": "You are v3.", "prompt_mode": "full",
                                 "full_prompt": "You are v3."})
        core["active_version"] = 3
        core["published_version"] = 3
        # New intent + changed settings + changed schema + new release.
        new_intent_id = new_id("in")
        pkg["resources"]["intents"].append({
            "id": new_intent_id, "tenant_id": workspace["tenant_id"],
            "bot_id": workspace["bot_id"], "name": "Refund status",
            "samples": ["refund kab aayega"], "confidence_threshold": 0.7,
            "priority": 100, "handoff_enabled": False, "status": "active", "version": 1,
        })
        pkg["resources"]["voice_bot_settings"]["speed"] = 0.95
        pkg["resources"]["voice_bot_settings"]["llm_model"] = "gpt-4.1-mini"
        pkg["resources"]["runtime_context_schema"]["fields"] = [
            {"key": "name", "type": "string"}, {"key": "order_id", "type": "string"}]
        new_release_id = new_id("rel")
        pkg["resources"]["releases"].append({
            "id": new_release_id, "tenant_id": workspace["tenant_id"],
            "bot_id": workspace["bot_id"], "version": "v1.3.0", "stage": "published",
            "notes": "Refunds", "published_at": "2026-09-10T09:00:00",
        })
        pkg["bot"]["version"] = "v1.3.0"
        pkg["bot"]["live_version"] = "v1.3.0"
        _reseal(pkg)

        preview = _preview(client, workspace["tenant_admin"], copy.deepcopy(pkg))
        assert preview["action"] == "update" and preview["existing"] is True
        assert preview["removed"]["prompt"] == 1
        assert preview["created"]["intent"] == 1

        report = _import(client, workspace["tenant_admin"], pkg)
        assert report["action"] == "update"
        assert report["updated"]["bot"] == 1
        assert report["removed"]["prompt"] == 1
        assert report["created"]["intent"] == 1
        assert report["created"]["prompt_version"] == 1
        assert report["created"]["release"] == 1

        session = _db()
        try:
            bots = session.scalars(select(VoiceBot).where(
                VoiceBot.tenant_id == workspace["tenant_id"],
                VoiceBot.is_deleted.is_(False))).all()
            assert sorted(b.id for b in bots) == sorted([workspace["bot_id"],
                                                         workspace["other_bot_id"]])
            bot = session.get(VoiceBot, workspace["bot_id"])
            assert bot.description == "Handles order AND refund questions"
            assert bot.live_version == "v1.3.0"
            wf = session.get(Workflow, workspace["workflow_id"])
            assert wf.name == "Order journey v3" and wf.version == 3
            assert len(wf.nodes) == 5
            greeting = session.get(Prompt, workspace["greeting_id"])
            assert greeting.is_deleted is True  # retired, not purged
            versions = session.scalars(select(PromptVersion).where(
                PromptVersion.prompt_id == workspace["prompt_id"])).all()
            assert sorted(v.version for v in versions) == [1, 2, 3]
            assert session.get(Prompt, workspace["prompt_id"]).published_version == 3
            assert session.get(Intent, new_intent_id).name == "Refund status"
            assert session.get(VoiceBotSetting, workspace["settings_id"]).speed == 0.95
            assert len(session.get(RuntimeContextSchema, workspace["schema_id"]).fields) == 2
            assert session.get(Release, new_release_id).version == "v1.3.0"
            # Sibling bot untouched.
            assert session.get(VoiceBot, workspace["other_bot_id"]).status == "draft"
        finally:
            session.close()

        # Back to the original package: the extra rows are retired again.
        report = _restore(client, workspace, package)
        assert report["removed"]["intent"] == 1
        assert report["removed"]["release"] == 1
        assert report["removed"]["prompt_version"] == 1
        assert report["updated"]["prompt"] == 2  # greeting revived
        session = _db()
        try:
            assert session.get(Prompt, workspace["greeting_id"]).is_deleted is False
            assert session.get(Intent, new_intent_id).is_deleted is True
        finally:
            session.close()

    def test_intent_recreated_locally_under_same_name_replaces_the_old_row(
        self, client, workspace, package,
    ):
        _restore(client, workspace, package)
        pkg = copy.deepcopy(package)
        replacement_id = new_id("in")
        pkg["resources"]["intents"][0]["id"] = replacement_id
        pkg["resources"]["api_connections"][0]["allowed_intents"] = [replacement_id]
        _reseal(pkg)
        report = _import(client, workspace["tenant_admin"], pkg)
        assert report["created"]["intent"] == 1
        assert any("replaced" in w for w in report["warnings"])
        session = _db()
        try:
            live = session.scalars(select(Intent).where(
                Intent.bot_id == workspace["bot_id"], Intent.name == "Order status")).all()
            assert [i.id for i in live] == [replacement_id]
        finally:
            session.close()
        _restore(client, workspace, package)

    def test_deleted_bot_is_restored_by_import(self, client, workspace, package):
        _restore(client, workspace, package)
        _data(client.delete(f"{API}/bots/{workspace['bot_id']}",
                            headers=workspace["tenant_admin"]))
        session = _db()
        try:
            assert session.get(VoiceBot, workspace["bot_id"]).is_deleted is True
            assert session.get(Workflow, workspace["workflow_id"]).is_deleted is True
        finally:
            session.close()
        report = _restore(client, workspace, package)
        assert any("was deleted on this environment" in w for w in report["warnings"])
        session = _db()
        try:
            bot = session.get(VoiceBot, workspace["bot_id"])
            assert bot.is_deleted is False and bot.status == "published"
            assert session.get(Workflow, workspace["workflow_id"]).is_deleted is False
            assert session.get(Intent, workspace["intent_id"]).is_deleted is False
        finally:
            session.close()


# ── Rejections: wrong tenant, tampering, corrupt packages ─────────────────────


class TestRejections:
    def test_wrong_tenant_admin_is_rejected_and_nothing_changes(
        self, client, workspace, package,
    ):
        _restore(client, workspace, package)
        before = _snapshot(workspace["bot_id"])
        pkg = copy.deepcopy(package)
        pkg["resources"]["workflows"][0]["name"] = "Should never land"
        _reseal(pkg)
        message = _import(client, workspace["foreign_admin"], pkg, expected=409)
        assert "belongs to tenant" in message and workspace["foreign_tenant_id"] in message
        assert _snapshot(workspace["bot_id"]) == before
        session = _db()
        try:
            assert session.scalar(select(VoiceBot).where(
                VoiceBot.tenant_id == workspace["foreign_tenant_id"])) is None
        finally:
            session.close()

    def test_super_admin_selecting_another_tenant_is_rejected(
        self, client, workspace, package, super_admin,
    ):
        before = _snapshot(workspace["bot_id"])
        message = _import(client, super_admin, copy.deepcopy(package), expected=409,
                          tenantId=workspace["foreign_tenant_id"])
        assert "destination tenant" in message
        assert _snapshot(workspace["bot_id"]) == before
        # Selecting the right tenant works (and defaults to the package tenant).
        report = _import(client, super_admin, copy.deepcopy(package),
                         tenantId=workspace["tenant_id"])
        assert report["action"] == "update"
        report = _import(client, super_admin, copy.deepcopy(package))
        assert report["tenantId"] == workspace["tenant_id"]

    def test_editing_tenant_id_in_the_file_is_detected(self, client, workspace, package):
        before = _snapshot(workspace["bot_id"])
        pkg = copy.deepcopy(package)
        tid = workspace["foreign_tenant_id"]
        pkg["tenant_id"] = tid
        pkg["bot"]["tenant_id"] = tid
        for section in pkg["resources"].values():
            rows = section if isinstance(section, list) else ([section] if section else [])
            for row in rows:
                row["tenant_id"] = tid
        for section in pkg["shared"].values():
            for row in section:
                if row.get("tenant_id") == workspace["tenant_id"]:
                    row["tenant_id"] = tid
        for row in pkg["environment"]["channel_configs"]:
            row["tenant_id"] = tid
        for doc in pkg["knowledge_plane"]["documents"]:
            doc["tenant_id"] = tid
            for chunk in doc["chunks"]:
                chunk["tenant_id"] = tid
        message = _import(client, workspace["foreign_admin"], pkg, expected=422)
        assert "modified after export" in message
        assert _snapshot(workspace["bot_id"]) == before
        session = _db()
        try:
            assert session.scalar(select(VoiceBot).where(VoiceBot.tenant_id == tid)) is None
        finally:
            session.close()

    def test_inconsistent_tenant_inside_rows_is_rejected(self, client, workspace, package):
        pkg = copy.deepcopy(package)
        pkg["resources"]["workflows"][0]["tenant_id"] = workspace["foreign_tenant_id"]
        message = _import(client, workspace["tenant_admin"], pkg, expected=422)
        assert "belongs to tenant" in message or "modified after export" in message

    def test_editing_bot_id_is_detected(self, client, workspace, package):
        pkg = copy.deepcopy(package)
        pkg["bot_id"] = new_id("bot")
        message = _import(client, workspace["tenant_admin"], pkg, expected=422)
        assert "does not match" in message or "modified after export" in message

    def test_bot_id_owned_by_another_tenant_is_a_collision(
        self, client, workspace, package, super_admin,
    ):
        # A foreign bot whose id equals the package bot id can only be
        # simulated by exporting a bot of the foreign tenant … and pointing a
        # package at it. Instead: bot-owned row id collisions.
        pkg = copy.deepcopy(package)
        session = _db()
        try:
            other_wf = Workflow(
                id=new_id("wf"), tenant_id=workspace["tenant_id"],
                bot_id=workspace["other_bot_id"], name="Sibling flow", nodes=[], edges=[],
            )
            session.add(other_wf)
            session.commit()
            other_wf_id = other_wf.id
        finally:
            session.close()
        try:
            pkg["resources"]["workflows"][0]["id"] = other_wf_id
            _reseal(pkg)
            before = _snapshot(workspace["bot_id"])
            message = _import(client, workspace["tenant_admin"], pkg, expected=409)
            assert "belongs to bot" in message
            assert _snapshot(workspace["bot_id"]) == before
        finally:
            session = _db()
            session.execute(delete(Workflow).where(Workflow.id == other_wf_id))
            session.commit()
            session.close()

    @pytest.mark.parametrize("mutate, fragment", [
        (lambda p: p.update(kind="echosphere.tenant.export"), "kind must be"),
        (lambda p: p.update(schema_version=99), "unsupported schema_version"),
        (lambda p: p.pop("bot"), "bot section"),
        (lambda p: p.pop("resources"), "resources"),
        (lambda p: p.pop("integrity"), "integrity"),
        (lambda p: p["resources"].update(mystery=[{"id": "x"}]), "unsupported resources"),
        (lambda p: p["resources"]["api_connections"][0].update(secret_ref="sk-live-raw"),
         "secret://"),
        (lambda p: p["environment"]["channel_configs"][0]["config"].update(
            authTokenReference="raw-token-value"), "env:VAR_NAME"),
    ])
    def test_invalid_packages_are_rejected_without_changes(
        self, client, workspace, package, mutate, fragment,
    ):
        before = _snapshot(workspace["bot_id"])
        pkg = copy.deepcopy(package)
        mutate(pkg)
        message = _import(client, workspace["tenant_admin"], pkg, expected=422)
        assert fragment in message, message
        assert _snapshot(workspace["bot_id"]) == before

    def test_non_object_body_is_rejected(self, client, workspace):
        response = client.post(f"{API}/bots/import", json=["not", "a", "package"],
                               headers=workspace["tenant_admin"])
        assert response.status_code in (400, 422), response.text

    def test_missing_destination_tenant_is_rejected(self, client, workspace, package):
        pkg = copy.deepcopy(package)
        session = _db()
        try:
            ghost = Tenant(id=new_id("tn"), name="Ghost", domain=f"ghost-{uuid.uuid4().hex[:8]}.example.test",
                           status="active")
            session.add(ghost)
            session.commit()
            ghost_id = ghost.id
        finally:
            session.close()
        try:
            # Export a bot that belongs to a tenant which is then gone on "live".
            session = _db()
            bot = VoiceBot(id=new_id("bot"), tenant_id=ghost_id, name="Ghost bot", status="draft")
            session.add(bot)
            session.commit()
            ghost_bot_id = bot.id
            session.close()
            ghost_pkg = _data(client.get(f"{API}/bots/{ghost_bot_id}/export",
                                         headers=_bearer_for("admin@aurexion.com")))
            _purge_tenant_graph([ghost_id])
            message = _import(client, _bearer_for("admin@aurexion.com"), ghost_pkg,
                              expected=422, tenantId=ghost_id)
            assert "does not exist on this environment" in message
        finally:
            _purge_tenant_graph([ghost_id])
        assert pkg  # unchanged reference package


# ── Environment-specific values ───────────────────────────────────────────────


class TestEnvironmentReconciliation:
    def test_live_channel_and_number_are_preserved_by_default(
        self, client, workspace, package,
    ):
        _restore(client, workspace, package)
        # Give "live" its own channel config + assigned number.
        session = _db()
        try:
            channel = session.get(ChannelConfig, workspace["voice_channel_id"])
            channel.config = {"phoneNumber": "+14155550199", "telephonyProvider": "twilio",
                              "publicWsBase": "wss://media.live.example.test",
                              "authTokenReference": "env:BT_TWILIO_TOKEN"}
            channel.enabled = True
            channel.status = "live"
            session.add(PhoneNumber(
                id=new_id("pn"), number="+14155550199", country="US",
                tenant_id=workspace["tenant_id"], bot_id=workspace["bot_id"],
                provider="twilio", status="assigned",
            ))
            session.commit()
        finally:
            session.close()

        report = _restore(client, workspace, package)  # package has +14155550101 / local ws
        session = _db()
        try:
            channel = session.get(ChannelConfig, workspace["voice_channel_id"])
            assert channel.config["phoneNumber"] == "+14155550199"
            assert channel.config["publicWsBase"] == "wss://media.live.example.test"
            assert channel.enabled is True and channel.status == "live"
            live_number = session.scalar(select(PhoneNumber).where(
                PhoneNumber.number == "+14155550199"))
            assert live_number.bot_id == workspace["bot_id"] and live_number.status == "assigned"
            assert session.scalar(select(PhoneNumber).where(
                PhoneNumber.number == "+14155550101")) is None
        finally:
            session.close()
        preserved = [p for p in report["preserved"] if p["kind"] == "channel"]
        assert preserved and preserved[0]["label"] == "voice"
        assert set(preserved[0]["differs"]) == {"phoneNumber", "publicWsBase"}
        assert any("kept its configuration" in w for w in report["warnings"])
        assert any(p["kind"] == "phone_number" and "+14155550199" in p["label"]
                   for p in report["preserved"])

    def test_apply_environment_applies_config_but_never_steals_a_number(
        self, client, workspace, package,
    ):
        # +14155550101 (package number) is held by ANOTHER bot on live.
        session = _db()
        try:
            session.add(PhoneNumber(
                id=new_id("pn"), number="+14155550101", country="US",
                tenant_id=workspace["tenant_id"], bot_id=workspace["other_bot_id"],
                provider="twilio", status="assigned",
            ))
            session.commit()
        finally:
            session.close()
        report = _import(client, workspace["tenant_admin"], copy.deepcopy(package),
                         applyEnvironment=True)
        session = _db()
        try:
            channel = session.get(ChannelConfig, workspace["voice_channel_id"])
            assert channel.config["publicWsBase"] == "wss://media.local.example.test"  # applied
            assert channel.config["phoneNumber"] == "+14155550199"  # live number kept
            held = session.scalar(select(PhoneNumber).where(PhoneNumber.number == "+14155550101"))
            assert held.bot_id == workspace["other_bot_id"]  # not stolen
            mine = session.scalar(select(PhoneNumber).where(PhoneNumber.number == "+14155550199"))
            assert mine.bot_id == workspace["bot_id"]
        finally:
            session.close()
        assert any("assigned to another channel" in w for w in report["warnings"])

        # Free the package number → explicit apply now claims it and releases the old one.
        session = _db()
        try:
            session.execute(delete(PhoneNumber).where(PhoneNumber.number == "+14155550101"))
            session.commit()
        finally:
            session.close()
        report = _import(client, workspace["tenant_admin"], copy.deepcopy(package),
                         applyEnvironment=True)
        session = _db()
        try:
            channel = session.get(ChannelConfig, workspace["voice_channel_id"])
            assert channel.config["phoneNumber"] == "+14155550101"
            claimed = session.scalar(select(PhoneNumber).where(PhoneNumber.number == "+14155550101"))
            assert claimed.bot_id == workspace["bot_id"] and claimed.status == "assigned"
            released = session.scalar(select(PhoneNumber).where(PhoneNumber.number == "+14155550199"))
            assert released.bot_id is None and released.status == "available"
        finally:
            session.close()
        assert report["created"]["phone_number"] == 1

    def test_local_api_url_never_overwrites_a_live_url(self, client, workspace, package):
        _restore(client, workspace, package)
        pkg = copy.deepcopy(package)
        pkg["resources"]["api_connections"][0]["url"] = "http://localhost:9022/orders/{{id}}"
        _reseal(pkg)
        report = _import(client, workspace["tenant_admin"], pkg)
        session = _db()
        try:
            api = session.get(ApiConnection, workspace["bot_api_id"])
            assert api.url == "https://orders.example.test/{{id}}"
        finally:
            session.close()
        assert any(p["kind"] == "api_connection_url" for p in report["preserved"])
        assert any("kept its URL" in w for w in report["warnings"])

        report = _import(client, workspace["tenant_admin"], copy.deepcopy(pkg),
                         applyEnvironment=True)
        session = _db()
        try:
            assert session.get(ApiConnection, workspace["bot_api_id"]).url \
                == "http://localhost:9022/orders/{{id}}"
        finally:
            session.close()
        _restore(client, workspace, package)

    def test_unresolvable_secret_references_are_reported(self, client, workspace, package):
        report = _restore(client, workspace, package)
        refs = {s["reference"] for s in report["secretsMissing"]}
        assert "secret://bt-orders-api-key" in refs
        assert "env:BT_TWILIO_TOKEN" in refs
        owners = {s["owner"] for s in report["secretsMissing"]}
        assert any("Fetch order" in o for o in owners)


# ── Shared resources ──────────────────────────────────────────────────────────


class TestSharedResources:
    def test_missing_shared_tool_and_entity_are_created_with_their_ids(
        self, client, workspace, package,
    ):
        session = _db()
        try:
            session.execute(delete(ApiConnection).where(
                ApiConnection.id == workspace["shared_api_id"]))
            session.execute(delete(EntityDef).where(EntityDef.id == workspace["entity_id"]))
            session.commit()
        finally:
            session.close()
        report = _restore(client, workspace, package)
        assert report["created"]["tenant_api_connection"] == 1
        assert report["created"]["entity_def"] == 1
        session = _db()
        try:
            shared = session.get(ApiConnection, workspace["shared_api_id"])
            assert shared.bot_id is None and shared.tenant_id == workspace["tenant_id"]
            assert session.get(EntityDef, workspace["entity_id"]).name == f"order_id_{workspace['suffix']}"
        finally:
            session.close()

    def test_existing_shared_tool_is_reused_and_never_modified(
        self, client, workspace, package,
    ):
        session = _db()
        try:
            shared = session.get(ApiConnection, workspace["shared_api_id"])
            shared.url = "https://crm.live.example.test/{{id}}"
            session.commit()
        finally:
            session.close()
        report = _restore(client, workspace, package)
        assert report["reused"]["tenant_api_connection"] == 1
        assert "tenant_api_connection" not in report["updated"]
        session = _db()
        try:
            assert session.get(ApiConnection, workspace["shared_api_id"]).url \
                == "https://crm.live.example.test/{{id}}"
        finally:
            session.close()

    def test_shared_tool_matched_by_name_is_remapped(self, client, workspace, package):
        # Live has the same tenant tool under a DIFFERENT id: the bot's
        # references are remapped to it instead of forking a duplicate.
        live_id = new_id("api")
        session = _db()
        try:
            session.execute(delete(ApiConnection).where(
                ApiConnection.id == workspace["shared_api_id"]))
            session.add(ApiConnection(
                id=live_id, tenant_id=workspace["tenant_id"], bot_id=None,
                name=workspace["shared_api_name"], method="GET",
                url="https://crm.live.example.test/{{id}}",
            ))
            session.commit()
        finally:
            session.close()
        try:
            report = _restore(client, workspace, package)
            assert report["remappedIds"][workspace["shared_api_id"]] == live_id
            session = _db()
            try:
                intent = session.get(Intent, workspace["intent_id"])
                assert intent.api_connection_id == live_id
                wf = session.get(Workflow, workspace["workflow_id"])
                assert wf.nodes[2]["config"]["connectionId"] == live_id
                assert session.get(ApiConnection, workspace["shared_api_id"]) is None
            finally:
                session.close()
        finally:
            session = _db()
            session.execute(delete(ApiConnection).where(ApiConnection.id == live_id))
            session.commit()
            session.close()
            _restore(client, workspace, package)  # re-creates the shared tool

    def test_missing_tenant_knowledge_source_is_a_warning_not_an_empty_kb(
        self, client, workspace, package,
    ):
        session = _db()
        try:
            session.execute(delete(KnowledgeSource).where(
                KnowledgeSource.id == workspace["tenant_kb_id"]))
            session.commit()
        finally:
            session.close()
        report = _restore(client, workspace, package)
        assert any(workspace["tenant_kb_id"] in w and "does not exist" in w
                   for w in report["warnings"])
        session = _db()
        try:
            assert session.get(KnowledgeSource, workspace["tenant_kb_id"]) is None
            # The reference is kept for when the KB is created here.
            assert workspace["tenant_kb_id"] in session.get(Intent, workspace["intent_id"]).kb_ids
        finally:
            session.close()

    def test_dangling_guardrail_profile_is_rejected(self, client, workspace, package):
        pkg = copy.deepcopy(package)
        pkg["shared"]["guardrail_profiles"] = []
        pkg["shared"]["guardrails"] = []
        pkg["bot"]["guardrail_profile_id"] = new_id("gp")
        _reseal(pkg)
        message = _import(client, workspace["tenant_admin"], pkg, expected=422)
        assert "guardrail profile" in message
