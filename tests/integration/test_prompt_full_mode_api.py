"""Full/unified prompt mode — API lifecycle, rendering, isolation.

Runs against the live app + local databases; a throwaway bot per module,
every touched row removed in teardown. Covers: creation/editing of full
prompts, preview + rendered-with-test-data preview, missing-variable
warnings, versioning, the draft→approval→publish lifecycle, structured
prompts remaining untouched, and cross-tenant isolation.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]

FULL_PROMPT = """# Role and identity
You are Kaya, a recovery specialist calling on behalf of {lender_name}.

# Objective
Recover the overdue amount of {overdue_amount} from {customer_name}.

# Conversation flow
1. Confirm identity. 2. State the overdue amount. 3. Agree a payment.

# Compliance rules
Never threaten. Follow RBI guidelines. Speak Hindi or English as the caller does.

# Closing
Thank the customer and close politely.
"""


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _bearer(email: str) -> dict:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response, expect=200):
    assert response.status_code == expect, response.text
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


@pytest.fixture(scope="module")
def other_tenant_admin():
    """An admin of a DIFFERENT tenant, for isolation checks."""
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import Role, User

    session = get_sessionmaker()()
    try:
        rows = session.execute(
            select(User).join(Role, User.role_id == Role.id).where(
                Role.code.in_(("tenant_admin", "tenant_owner")),
                User.tenant_id.isnot(None),
            )
        ).scalars().all()
        primary_tenant = next(
            u.tenant_id for u in rows
            if u.email == "priya.sharma@meridianhealth.com"
        )
        other = next((u for u in rows if u.tenant_id != primary_tenant), None)
        if other is None:
            pytest.skip("no second tenant admin in the dev DB")
        token = create_access_token(user_id=other.id, role=other.role.code,
                                    tenant_id=other.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _enabled_language() -> str:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import SupportedLanguage

    session = get_sessionmaker()()
    try:
        codes = session.scalars(
            select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))
        ).all()
        for preferred in ("hi-IN", "en-IN", "en-US"):
            if preferred in codes:
                return preferred
        assert codes
        return codes[0]
    finally:
        session.close()


@pytest.fixture(scope="module")
def test_bot(client, tenant_admin):
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"FullPrompt Bot {_SUFFIX}", "useCase": "collections",
        "languages": [_enabled_language()],
    }), expect=201)
    bot_id = created["id"]
    yield {"id": bot_id}

    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "DELETE pv FROM prompt_versions pv JOIN prompts p ON pv.prompt_id = p.id "
            "WHERE p.bot_id = :b"), {"b": bot_id})
        for table in ("prompts", "voice_bot_readiness", "bot_languages",
                      "voice_bot_settings", "workflows", "intents", "audit_logs"):
            column = "entity_id" if table == "audit_logs" else "bot_id"
            try:
                conn.execute(sa_text(f"DELETE FROM {table} WHERE {column} = :b"),
                             {"b": bot_id})
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


@pytest.fixture(scope="module")
def full_prompt(client, tenant_admin, test_bot):
    return _data(client.post(
        f"{API}/bots/{test_bot['id']}/prompts", headers=tenant_admin,
        json={"type": "system", "promptMode": "full",
              "name": f"Unified prompt {_SUFFIX}", "fullPrompt": FULL_PROMPT},
    ), expect=201)


class TestFullPromptAuthoring:
    def test_created_as_full_mode_draft(self, full_prompt):
        assert full_prompt["state"] == "draft"
        version = full_prompt["versions"][0]
        assert version["promptMode"] == "full"
        assert version["fullPrompt"].startswith("# Role and identity")
        # The full prompt IS the compiled prompt — not forced into sections.
        assert version["compiledPrompt"] == FULL_PROMPT.strip()
        assert version["structuredConfig"] is None
        # Variables were extracted from the text.
        assert set(full_prompt["variables"]) >= {
            "lender_name", "overdue_amount", "customer_name",
        }

    def test_empty_full_prompt_rejected(self, client, tenant_admin, test_bot):
        response = client.post(
            f"{API}/bots/{test_bot['id']}/prompts", headers=tenant_admin,
            json={"type": "system", "promptMode": "full",
                  "name": f"Empty {_SUFFIX}", "fullPrompt": "   "},
        )
        assert response.status_code == 422

    def test_new_version_and_rendered_preview(self, client, tenant_admin, full_prompt):
        updated = _data(client.post(
            f"{API}/prompts/{full_prompt['id']}/versions", headers=tenant_admin,
            json={"promptMode": "full",
                  "fullPrompt": FULL_PROMPT + "\n# Objection handling\nAcknowledge {objection_type} calmly.",
                  "note": "objections", "submitForApproval": False},
        ), expect=201)
        assert updated["versions"][0]["version"] == 2
        assert updated["state"] == "draft"

        render = _data(client.post(
            f"{API}/prompts/{full_prompt['id']}/render-preview", headers=tenant_admin,
            json={"version": 2, "testContext": {
                "customer_name": "Rahul Sharma", "lender_name": "Example Finance",
                "overdue_amount": 12500, "extra": "unused",
            }},
        ))
        assert render["promptMode"] == "full"
        assert "Rahul Sharma" in render["rendered"]
        assert "Example Finance" in render["rendered"]
        assert "12500" in render["rendered"]
        # Missing-variable warnings: objection_type has no test value.
        assert "objection_type" in render["missing"]
        assert "extra" in render["unusedTestKeys"]

    def test_compile_preview_stateless_full(self, client, tenant_admin):
        result = _data(client.post(
            f"{API}/prompts/compile-preview", headers=tenant_admin,
            json={"promptMode": "full", "fullPrompt": "Hi {name}!",
                  "testContext": {"name": "Asha"}},
        ))
        assert result["valid"] is True
        assert result["compiled"] == "Hi {name}!"
        assert result["variables"] == ["name"]
        assert result["render"]["rendered"] == "Hi Asha!"

    def test_structured_compile_preview_unchanged(self, client, tenant_admin):
        result = _data(client.post(
            f"{API}/prompts/compile-preview", headers=tenant_admin,
            json={"structuredConfig": {"identity": {"botName": "Ava", "role": "helper"}}},
        ))
        assert result["valid"] is True
        assert result["compiled"].startswith("# Identity\nYou are Ava, a helper.")


class TestLifecycle:
    def test_draft_to_published_and_traceable(self, client, tenant_admin, full_prompt):
        prompt_id = full_prompt["id"]
        for state in ("pending_approval", "approved", "published"):
            updated = _data(client.patch(
                f"{API}/prompts/{prompt_id}", headers=tenant_admin,
                json={"state": state},
            ))
            assert updated["state"] == state
        assert updated["publishedVersion"] == updated["activeVersion"]
        assert updated["publishedAt"]

    async def test_published_version_reaches_runtime_config(self, client, tenant_admin,
                                                            test_bot, full_prompt):
        """resolve_bot_config picks the published full prompt + provenance."""
        from shared.bot_config import resolve_bot_config

        config = await resolve_bot_config(
            test_bot["id"], require_published=False, use_cache=False,
        )
        assert config.prompt_id == full_prompt["id"]
        assert config.prompt_mode == "full"
        assert config.prompt_version == 2
        assert config.system_prompt.startswith("# Role and identity")

    async def test_new_draft_does_not_hide_published_runtime_version(
        self, client, tenant_admin, test_bot, full_prompt,
    ):
        """A draft may be tested while the published pointer stays live."""
        draft = _data(client.post(
            f"{API}/prompts/{full_prompt['id']}/versions", headers=tenant_admin,
            json={
                "promptMode": "full",
                "fullPrompt": FULL_PROMPT + "\n# Draft-only marker\nDo not publish yet.",
                "note": "draft alongside published version",
                "submitForApproval": False,
            },
        ), expect=201)
        assert draft["state"] == "draft"
        assert draft["activeVersion"] == 3
        assert draft["publishedVersion"] == 2

        from shared.bot_config import resolve_bot_config

        config = await resolve_bot_config(
            test_bot["id"], require_published=False, use_cache=False,
        )
        assert config.prompt_version == 2
        assert "Draft-only marker" not in config.system_prompt

    async def test_new_draft_does_not_replace_published_greeting(
        self, test_bot,
    ):
        """Greeting resolution obeys the same published pointer as system prompts."""
        from shared.db.mysql import get_sessionmaker
        from shared.ids import new_id
        from shared.models import Prompt, PromptVersion, VoiceBot

        session = get_sessionmaker()()
        try:
            bot = session.get(VoiceBot, test_bot["id"])
            assert bot is not None
            prompt_id = new_id("pr")
            session.add(Prompt(
                id=prompt_id, tenant_id=bot.tenant_id, bot_id=bot.id,
                type="greeting", name=f"Published greeting {_SUFFIX}",
                state="draft", active_version=2, published_version=1,
            ))
            session.add_all([
                PromptVersion(
                    id=new_id("prv"), prompt_id=prompt_id, version=1,
                    variants=[{"language": "en-IN", "content": "Published hello"}],
                    prompt_mode="structured",
                ),
                PromptVersion(
                    id=new_id("prv"), prompt_id=prompt_id, version=2,
                    variants=[{"language": "en-IN", "content": "Draft hello"}],
                    prompt_mode="structured",
                ),
            ])
            session.commit()
        finally:
            session.close()

        from shared.bot_config import resolve_bot_config

        config = await resolve_bot_config(
            test_bot["id"], require_published=False, use_cache=False,
        )
        assert config.greeting == "Published hello"


class TestIsolation:
    def test_other_tenant_cannot_see_or_edit(self, client, other_tenant_admin,
                                             test_bot, full_prompt):
        assert client.get(
            f"{API}/bots/{test_bot['id']}/prompts", headers=other_tenant_admin,
        ).status_code in (403, 404)
        assert client.post(
            f"{API}/prompts/{full_prompt['id']}/versions", headers=other_tenant_admin,
            json={"promptMode": "full", "fullPrompt": "hijack"},
        ).status_code in (403, 404)
        assert client.post(
            f"{API}/prompts/{full_prompt['id']}/render-preview",
            headers=other_tenant_admin, json={"testContext": {}},
        ).status_code in (403, 404)
