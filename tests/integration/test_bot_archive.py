"""Bot Archive / Restore lifecycle.

Archive is a reversible, visible management state: status="archived" with
is_deleted=0. The bot stays in the list (under the Archived filter), its whole
configuration is retained and readable, channels are deactivated (not
deleted), phone numbers are reserved for the bot, and every runtime entry
point refuses it — phone, WhatsApp, browser test sessions, testing tools,
publishing. Restore returns it to draft with the same configuration and number
relationship, still not live. Cross-tenant access is a sanitized 404.
"""

import uuid
from datetime import datetime, timedelta

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
    Intent,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    PromptVersion,
    Role,
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
    """One tenant with `bot` (published, fully loaded; archived + restored by
    the flow tests), `guard_bot` (published, must never change) and a foreign
    tenant/bot for isolation checks."""
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    try:
        admin_role = session.execute(
            select(Role).where(Role.code == "tenant_admin")).scalar_one()
        user_role = session.execute(
            select(Role).where(Role.code == "tenant_user")).scalar_one()

        tenant = Tenant(
            id=new_id("tn"), name=f"Archive Test {suffix}", code=f"barc_{suffix}",
            domain=f"barc-{suffix}.example.test", status="active",
        )
        other_tenant = Tenant(
            id=new_id("tn"), name=f"Archive Foreign {suffix}", code=f"barf_{suffix}",
            domain=f"barf-{suffix}.example.test", status="active",
        )
        session.add_all([tenant, other_tenant])
        session.flush()

        admin = User(
            id=new_id("usr"), email=f"barc.admin.{suffix}@example.test",
            name="Archive Admin", password_hash="x", role_id=admin_role.id,
            tenant_id=tenant.id, status="active",
        )
        member = User(
            id=new_id("usr"), email=f"barc.user.{suffix}@example.test",
            name="Archive Member", password_hash="x", role_id=user_role.id,
            tenant_id=tenant.id, status="active",
        )
        session.add_all([admin, member])
        session.flush()

        bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Archive Target {suffix}",
            use_case="Billing support", status="published", version="v1.2.0",
            live_version="v1.2.0", published_at=datetime(2026, 8, 1, 12, 0),
            health="good", owner_user_id=admin.id,
        )
        guard_bot = VoiceBot(
            id=new_id("bot"), tenant_id=tenant.id, name=f"Archive Guard {suffix}",
            status="published", live_version="v0.1.0", owner_user_id=admin.id,
        )
        foreign_bot = VoiceBot(
            id=new_id("bot"), tenant_id=other_tenant.id,
            name=f"Archive Foreign Bot {suffix}", status="draft",
        )
        session.add_all([bot, guard_bot, foreign_bot])
        session.flush()

        # E.164 digits only — the channel save validates the number format
        # before the reservation check, so hex suffixes would 422 too early.
        digits = str(int(suffix, 16))[-8:].rjust(8, "0")
        number = f"+9177{digits}"
        guard_number = f"+9178{digits}"
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
            ChannelConfig(
                id=new_id("ch"), tenant_id=tenant.id, bot_id=guard_bot.id,
                type="voice", status="live", enabled=True,
                config={"phoneNumber": guard_number, "telephonyProvider": "vaani"},
            ),
            PhoneNumber(
                id=new_id("pn"), number=guard_number, country="IN",
                tenant_id=tenant.id, bot_id=guard_bot.id, status="assigned",
            ),
        ])

        prompt = Prompt(
            id=new_id("pr"), tenant_id=tenant.id, bot_id=bot.id, type="system",
            name="Core prompt", state="approved", active_version=1,
            published_version=1,
        )
        session.add(prompt)
        session.flush()
        session.add(PromptVersion(
            id=new_id("prv"), prompt_id=prompt.id, version=1,
            compiled_prompt="You are the archive-target bot.",
        ))
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
        bot_api = ApiConnection(
            id=new_id("api"), tenant_id=tenant.id, bot_id=bot.id,
            name="Bot API", method="GET",
            url="https://billing.example.test/x", status="healthy",
        )
        conversation = ConversationSession(
            id=new_id("cv"), tenant_id=tenant.id, bot_id=bot.id,
            started_at=datetime(2026, 8, 3, 10, 0), duration_sec=61,
            channel="voice", sentiment="neutral", intents=[], contained=True,
            status="completed",
        )
        session.add_all([bot_kb, workflow, intent, scenario, schema, bot_api,
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
            "guard_number": guard_number,
            "digits": digits,
            "config_rows": (
                (Prompt, prompt.id), (Intent, intent.id), (Workflow, workflow.id),
                (KnowledgeSource, bot_kb.id), (TestScenario, scenario.id),
                (RuntimeContextSchema, schema.id), (ApiConnection, bot_api.id),
                (ConversationSession, conversation.id),
            ),
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
            session.execute(delete(BotLanguage).where(BotLanguage.bot_id.in_(bot_ids)))
            session.execute(delete(VoiceBotReadiness).where(
                VoiceBotReadiness.bot_id.in_(bot_ids)))
        for model in (RuntimeContextSchema, Prompt, Intent, ApiConnection, Workflow,
                      TestScenario, KnowledgeSource, ChannelConfig, PhoneNumber,
                      ConversationSession, VoiceBotSetting, AuditLog, VoiceBot, User):
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
                "live_version": bot.live_version, "published_at": bot.published_at,
                "deleted_at": bot.deleted_at}
    finally:
        session.close()


def _number(number):
    session = _db()
    try:
        row = session.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
        return {"status": row.status, "tenant_id": row.tenant_id,
                "bot_id": row.bot_id, "is_deleted": row.is_deleted}
    finally:
        session.close()


def _channels(bot_id):
    session = _db()
    try:
        rows = session.scalars(select(ChannelConfig).where(
            ChannelConfig.bot_id == bot_id).order_by(ChannelConfig.type)).all()
        return {r.type: {"enabled": r.enabled, "status": r.status,
                         "is_deleted": r.is_deleted} for r in rows}
    finally:
        session.close()


def _assert_config_retained(workspace):
    session = _db()
    try:
        for model, row_id in workspace["config_rows"]:
            row = session.get(model, row_id)
            assert row is not None, model.__name__
            assert row.is_deleted is False, model.__name__
    finally:
        session.close()


def _make_bot(client, workspace, name, *, status="draft"):
    created = client.post(
        f"{API}/bots", headers=workspace["admin"],
        json={"name": f"{name} {workspace['suffix']}"},
    ).json()["data"]
    if status != "draft":
        session = _db()
        try:
            session.get(VoiceBot, created["id"]).status = status
            session.commit()
        finally:
            session.close()
    return created["id"]


# ── Access & tenancy ──────────────────────────────────────────────────────────


class TestArchiveAccess:
    def test_requires_authentication(self, client, workspace):
        response = client.post(f"{API}/bots/{workspace['guard_bot_id']}/archive")
        assert response.status_code == 401
        assert _bot_state(workspace["guard_bot_id"])["status"] == "published"

    def test_tenant_user_cannot_archive(self, client, workspace):
        response = client.post(
            f"{API}/bots/{workspace['guard_bot_id']}/archive", headers=workspace["member"])
        assert response.status_code == 403
        assert _bot_state(workspace["guard_bot_id"])["status"] == "published"

    def test_cross_tenant_archive_and_restore_are_sanitized_404(self, client, workspace):
        for action in ("archive", "restore"):
            response = client.post(
                f"{API}/bots/{workspace['foreign_bot_id']}/{action}",
                headers=workspace["admin"])
            missing = client.post(
                f"{API}/bots/bot_000000000000/{action}", headers=workspace["admin"])
            assert response.status_code == 404
            assert missing.status_code == 404
            assert response.json()["message"] == missing.json()["message"]
        assert _bot_state(workspace["foreign_bot_id"])["status"] == "draft"

    def test_restore_of_a_live_bot_is_refused(self, client, workspace):
        response = client.post(
            f"{API}/bots/{workspace['guard_bot_id']}/restore", headers=workspace["admin"])
        assert response.status_code == 409
        assert _bot_state(workspace["guard_bot_id"])["status"] == "published"

    def test_cache_invalidated_on_archive_and_restore(self, client, workspace, monkeypatch):
        import shared.bot_config as bot_config

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            bot_config, "invalidate_bot_config_sync",
            lambda tenant_id, bot_id: calls.append((tenant_id, bot_id)),
        )
        bot_id = _make_bot(client, workspace, "Cache Probe")
        assert client.post(f"{API}/bots/{bot_id}/archive",
                           headers=workspace["admin"]).status_code == 200
        assert client.post(f"{API}/bots/{bot_id}/restore",
                           headers=workspace["admin"]).status_code == 200
        assert calls.count((workspace["tenant_id"], bot_id)) == 2


# ── Archive ───────────────────────────────────────────────────────────────────


class TestArchiveFlow:
    @pytest.fixture(scope="class")
    def archived(self, client, workspace):
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/archive", headers=workspace["admin"])
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def test_response_shape(self, archived, workspace):
        assert archived == {
            "archived": True, "id": workspace["bot_id"], "status": "archived",
            "channelsDisabled": 2, "phoneNumbersReserved": 1,
        }

    def test_bot_row_is_archived_not_deleted(self, archived, workspace):
        state = _bot_state(workspace["bot_id"])
        assert state["status"] == "archived"
        assert state["is_deleted"] is False
        assert state["deleted_at"] is None
        assert state["live_version"] is None      # nothing is live while parked
        assert state["published_at"] is not None  # history is kept

    def test_visible_in_lists_under_archived_only(self, client, archived, workspace):
        everything = client.get(f"{API}/bots?pageSize=200", headers=workspace["admin"])
        by_id = {b["id"]: b for b in everything.json()["data"]}
        assert by_id[workspace["bot_id"]]["status"] == "archived"
        assert by_id[workspace["guard_bot_id"]]["status"] == "published"

        archived_only = client.get(
            f"{API}/bots?status=archived&pageSize=200", headers=workspace["admin"])
        ids = [b["id"] for b in archived_only.json()["data"]]
        assert workspace["bot_id"] in ids
        assert workspace["guard_bot_id"] not in ids

        published_only = client.get(
            f"{API}/bots?status=published&pageSize=200", headers=workspace["admin"])
        ids = [b["id"] for b in published_only.json()["data"]]
        assert workspace["bot_id"] not in ids
        assert workspace["guard_bot_id"] in ids

    def test_detail_and_configuration_remain_readable(self, client, archived, workspace):
        bot_id = workspace["bot_id"]
        detail = client.get(f"{API}/bots/{bot_id}", headers=workspace["admin"])
        assert detail.status_code == 200
        assert detail.json()["data"]["status"] == "archived"
        for path in ("workflow", "channels", "prompts", "intents", "scenarios", "releases"):
            response = client.get(f"{API}/bots/{bot_id}/{path}", headers=workspace["admin"])
            assert response.status_code == 200, path
        # History stays reachable and still names the bot.
        conversations = client.get(
            f"{API}/conversations?pageSize=200", headers=workspace["admin"])
        names = {c["bot"] for c in conversations.json()["data"]}
        assert workspace["bot_name"] in names

    def test_channels_deactivated_not_deleted(self, archived, workspace):
        channels = _channels(workspace["bot_id"])
        assert channels["voice"] == {"enabled": False, "status": "configured",
                                     "is_deleted": False}
        assert channels["whatsapp"] == {"enabled": False, "status": "configured",
                                        "is_deleted": False}

    def test_phone_number_reserved_for_the_bot(self, archived, workspace):
        assert _number(workspace["number"]) == {
            "status": "reserved", "tenant_id": workspace["tenant_id"],
            "bot_id": workspace["bot_id"], "is_deleted": False,
        }

    def test_reserved_number_cannot_be_claimed_by_another_bot(self, client, archived, workspace):
        response = client.put(
            f"{API}/bots/{workspace['guard_bot_id']}/channels/voice",
            headers=workspace["admin"],
            json={"config": {"phoneNumber": workspace["number"],
                             "telephonyProvider": "vaani"}},
        )
        assert response.status_code == 409
        assert _number(workspace["number"])["bot_id"] == workspace["bot_id"]
        # The guard bot's own channel/number are untouched by the refused save.
        assert _number(workspace["guard_number"])["status"] == "assigned"

    def test_configuration_retained(self, archived, workspace):
        _assert_config_retained(workspace)

    def test_runtime_refuses_every_channel(self, archived, workspace):
        from shared.bot_config import (
            _bot_tenant_sync,
            _load_config_sync,
            _phone_assignment_sync,
        )

        bot_id = workspace["bot_id"]
        with pytest.raises(NotFoundError):          # phone / WhatsApp (published required)
            _load_config_sync(bot_id, True)
        with pytest.raises(NotFoundError):          # browser test session path
            _load_config_sync(bot_id, False)
        with pytest.raises(NotFoundError):          # inbound number no longer routes
            _phone_assignment_sync(workspace["number"])
        # Per-campaign routing: the bot still resolves to its tenant (it is not
        # deleted) but its configuration is refused above, so the dialer gets a
        # sanitized 404 rather than a call.
        assert _bot_tenant_sync(bot_id) == workspace["tenant_id"]

    def test_test_session_and_testing_tools_are_blocked(self, client, archived, workspace):
        bot_id = workspace["bot_id"]
        session = client.post(
            f"{API}/voice-sessions", headers=workspace["admin"],
            json={"botId": bot_id, "channel": "browser"})
        assert session.status_code == 409
        assert "archived" in session.json()["message"].lower()
        for path, body in (
            ("testing/chat", {"message": "hello"}),
            ("testing/simulate", {"message": "hello"}),
            ("scenarios/run", None),
        ):
            response = client.post(
                f"{API}/bots/{bot_id}/{path}", headers=workspace["admin"], json=body)
            assert response.status_code == 409, path

    def test_publishing_and_channel_activation_are_blocked(self, client, archived, workspace):
        bot_id = workspace["bot_id"]
        release = client.post(
            f"{API}/bots/{bot_id}/releases", headers=workspace["admin"],
            json={"version": "v2.0.0"})
        assert release.status_code == 409
        patch = client.patch(
            f"{API}/bots/{bot_id}", headers=workspace["admin"], json={"status": "published"})
        assert patch.status_code == 409
        for path in ("channels/voice/activate", "channels/voice/test"):
            response = client.post(
                f"{API}/bots/{bot_id}/{path}", headers=workspace["admin"])
            assert response.status_code == 409, path
        save = client.put(
            f"{API}/bots/{bot_id}/channels/voice", headers=workspace["admin"],
            json={"config": {"phoneNumber": workspace["number"],
                             "telephonyProvider": "vaani"}})
        assert save.status_code == 409
        assert _bot_state(bot_id)["status"] == "archived"
        assert _channels(bot_id)["voice"]["enabled"] is False

    def test_management_edits_still_work(self, client, archived, workspace):
        response = client.patch(
            f"{API}/bots/{workspace['bot_id']}", headers=workspace["admin"],
            json={"description": "parked for the season"})
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "archived"

    def test_second_archive_is_409(self, client, archived, workspace):
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/archive", headers=workspace["admin"])
        assert response.status_code == 409

    def test_guard_bot_untouched(self, archived, workspace):
        assert _bot_state(workspace["guard_bot_id"])["status"] == "published"
        assert _channels(workspace["guard_bot_id"])["voice"]["enabled"] is True
        assert _number(workspace["guard_number"])["status"] == "assigned"

    def test_audit_event_recorded(self, archived, workspace):
        session = _db()
        try:
            row = session.scalar(select(AuditLog).where(
                AuditLog.action == "Archived VoiceBot",
                AuditLog.entity_id == workspace["bot_id"]))
            assert row is not None
            assert row.previous_value["status"] == "published"
            assert row.new_value["channelsDisabled"] == 2
            assert row.new_value["phoneNumbersReserved"] == 1
        finally:
            session.close()


# ── Restore ───────────────────────────────────────────────────────────────────


class TestRestoreFlow:
    @pytest.fixture(scope="class")
    def restored(self, client, workspace):
        # Depends on the archive above having happened (module-ordered).
        assert _bot_state(workspace["bot_id"])["status"] == "archived"
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/restore", headers=workspace["admin"])
        assert response.status_code == 200, response.text
        return response.json()["data"]

    def test_response_shape(self, restored, workspace):
        assert restored == {"restored": True, "id": workspace["bot_id"],
                            "status": "draft", "phoneNumbersReassigned": 1}

    def test_bot_is_draft_and_not_live(self, restored, workspace):
        state = _bot_state(workspace["bot_id"])
        assert state["status"] == "draft"
        assert state["is_deleted"] is False
        assert state["live_version"] is None

    def test_number_relationship_preserved(self, restored, workspace):
        assert _number(workspace["number"]) == {
            "status": "assigned", "tenant_id": workspace["tenant_id"],
            "bot_id": workspace["bot_id"], "is_deleted": False,
        }

    def test_channels_stay_deactivated_until_retested(self, restored, workspace):
        channels = _channels(workspace["bot_id"])
        assert channels["voice"]["enabled"] is False
        assert channels["voice"]["is_deleted"] is False
        assert channels["whatsapp"]["enabled"] is False

    def test_configuration_retained(self, restored, workspace):
        _assert_config_retained(workspace)

    def test_lists_reflect_draft(self, client, restored, workspace):
        archived_only = client.get(
            f"{API}/bots?status=archived&pageSize=200", headers=workspace["admin"])
        assert workspace["bot_id"] not in [b["id"] for b in archived_only.json()["data"]]
        draft_only = client.get(
            f"{API}/bots?status=draft&pageSize=200", headers=workspace["admin"])
        assert workspace["bot_id"] in [b["id"] for b in draft_only.json()["data"]]

    def test_runtime_still_refuses_live_traffic(self, restored, workspace):
        from shared.bot_config import _load_config_sync

        with pytest.raises(NotFoundError):  # not published: no phone/WhatsApp
            _load_config_sync(workspace["bot_id"], True)
        # Browser testing of a draft works again — the archive block is lifted.
        config = _load_config_sync(workspace["bot_id"], False)
        assert config.bot_id == workspace["bot_id"]
        assert config.published is False

    def test_second_restore_is_409(self, client, restored, workspace):
        response = client.post(
            f"{API}/bots/{workspace['bot_id']}/restore", headers=workspace["admin"])
        assert response.status_code == 409

    def test_audit_event_recorded(self, restored, workspace):
        session = _db()
        try:
            row = session.scalar(select(AuditLog).where(
                AuditLog.action == "Restored VoiceBot",
                AuditLog.entity_id == workspace["bot_id"]))
            assert row is not None
            assert row.new_value["phoneNumbersReassigned"] == 1
        finally:
            session.close()


# ── PATCH status crosses the same boundary ────────────────────────────────────


class TestStatusPatchPath:
    def test_patch_to_archived_runs_the_archive(self, client, workspace):
        bot_id = _make_bot(client, workspace, "Patch Probe", status="published")
        number = f"+9179{workspace['digits']}"
        save = client.put(
            f"{API}/bots/{bot_id}/channels/voice", headers=workspace["admin"],
            json={"config": {"phoneNumber": number, "telephonyProvider": "vaani"}})
        assert save.status_code == 200, save.text
        assert _number(number)["status"] == "assigned"

        response = client.patch(
            f"{API}/bots/{bot_id}", headers=workspace["admin"], json={"status": "archived"})
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "archived"
        assert _number(number)["status"] == "reserved"
        assert _channels(bot_id)["voice"]["enabled"] is False
        session = _db()
        try:
            assert session.scalar(select(AuditLog).where(
                AuditLog.action == "Archived VoiceBot",
                AuditLog.entity_id == bot_id)) is not None
        finally:
            session.close()

        for blocked in ("published", "in_review", "approved", "rolled_back"):
            refused = client.patch(
                f"{API}/bots/{bot_id}", headers=workspace["admin"], json={"status": blocked})
            assert refused.status_code == 409, blocked
        restore = client.patch(
            f"{API}/bots/{bot_id}", headers=workspace["admin"], json={"status": "draft"})
        assert restore.status_code == 200
        assert restore.json()["data"]["status"] == "draft"
        assert _number(number)["status"] == "assigned"
        assert _number(number)["bot_id"] == bot_id


# ── Legacy rows (archived via the old soft delete) ────────────────────────────


class TestLegacyConversion:
    def test_only_evidenced_legacy_archives_are_converted(self, workspace):
        from backend.scripts.convert_legacy_archived_bots import apply, plan

        suffix = workspace["suffix"]
        tid = workspace["tenant_id"]
        when = datetime(2026, 9, 1, 6, 34, 16)
        session = _db()
        try:
            legacy = VoiceBot(
                id=new_id("bot"), tenant_id=tid, name=f"Legacy Archived {suffix}",
                status="archived", is_deleted=True, deleted_at=when,
                deleted_by=workspace["admin_id"], live_version="v0.1.0",
            )
            deleted = VoiceBot(
                id=new_id("bot"), tenant_id=tid, name=f"Legacy Deleted {suffix}",
                status="archived", is_deleted=True, deleted_at=when,
            )
            unknown = VoiceBot(
                id=new_id("bot"), tenant_id=tid, name=f"Legacy Unknown {suffix}",
                status="archived", is_deleted=True, deleted_at=when,
            )
            session.add_all([legacy, deleted, unknown])
            session.flush()
            session.add_all([
                ChannelConfig(  # archived by the same legacy action
                    id=new_id("ch"), tenant_id=tid, bot_id=legacy.id, type="voice",
                    status="archived", enabled=False, is_deleted=True,
                    deleted_at=when + timedelta(seconds=1), config={"phoneNumber": "+910"},
                ),
                ChannelConfig(  # archived long before — a deliberate channel removal
                    id=new_id("ch"), tenant_id=tid, bot_id=legacy.id, type="whatsapp",
                    status="archived", enabled=False, is_deleted=True,
                    deleted_at=when - timedelta(days=3), config={},
                ),
                AuditLog(id=new_id("au"), tenant_id=tid, actor_name="t",
                         action="Archived VoiceBot", entity_type="voice_bot",
                         entity_id=legacy.id),
                AuditLog(id=new_id("au"), tenant_id=tid, actor_name="t",
                         action="Archived VoiceBot", entity_type="voice_bot",
                         entity_id=deleted.id),
                AuditLog(id=new_id("au"), tenant_id=tid, actor_name="t",
                         action="Deleted VoiceBot", entity_type="voice_bot",
                         entity_id=deleted.id),
            ])
            session.commit()

            candidates = plan(session, tenant_id=tid)
            decisions = {c.bot_id: c for c in candidates}
            assert set(decisions) == {legacy.id, deleted.id, unknown.id}
            assert decisions[legacy.id].convert is True
            assert decisions[legacy.id].channel_ids  # only the same-action channel
            assert len(decisions[legacy.id].channel_ids) == 1
            assert decisions[deleted.id].convert is False
            assert decisions[unknown.id].convert is False

            assert apply(session, candidates) == 1
            session.commit()
            session.expire_all()

            assert legacy.is_deleted is False and legacy.status == "archived"
            assert legacy.deleted_at is None and legacy.live_version is None
            assert deleted.is_deleted is True and unknown.is_deleted is True
            channels = {c.type: c for c in session.scalars(
                select(ChannelConfig).where(ChannelConfig.bot_id == legacy.id))}
            assert channels["voice"].is_deleted is False
            assert channels["voice"].enabled is False
            assert channels["voice"].status == "configured"
            assert channels["whatsapp"].is_deleted is True
            audit = session.scalar(select(AuditLog).where(
                AuditLog.action == "Converted legacy archived VoiceBot",
                AuditLog.entity_id == legacy.id))
            assert audit is not None
            assert audit.previous_value["liveVersion"] == "v0.1.0"
            # Idempotent: a second plan finds nothing left to convert for it.
            assert legacy.id not in {c.bot_id for c in plan(session, tenant_id=tid)}
        finally:
            session.close()
