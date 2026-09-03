"""Release pipeline: create → review → approve → publish, the checklist gate,
the super-admin override, and the readiness flush fix (r6 after a channel is
archived and re-created).

Runs against the live app + local databases. A dedicated throwaway tn-001 bot
is created per module and every row it touches is removed in teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]


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


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


def _error(response, status: int) -> str:
    assert response.status_code == status, response.text
    body = response.json()
    assert body.get("success") is False, body
    return body["message"]


@pytest.fixture(scope="module")
def super_admin():
    return _bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


def _enabled_language() -> str:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import SupportedLanguage

    session = get_sessionmaker()()
    try:
        codes = session.scalars(
            select(SupportedLanguage.code).where(SupportedLanguage.enabled.is_(True))
        ).all()
        for preferred in ("en-US", "en-IN", "hi-IN"):
            if preferred in codes:
                return preferred
        assert codes, "no enabled platform language to create a test bot with"
        return codes[0]
    finally:
        session.close()


@pytest.fixture(scope="module")
def test_bot(client, tenant_admin):
    """A fresh draft bot: no releases, no scenarios, no channels."""
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"Release Test Bot {_SUFFIX}", "useCase": "releases",
        "languages": [_enabled_language()],
    }))
    yield created
    _purge_bot(created["id"])


def _purge_bot(bot_id: str) -> None:
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "DELETE FROM audit_logs WHERE entity_id = :b OR entity_id IN "
            "(SELECT id FROM releases WHERE bot_id = :b) OR entity_id IN "
            "(SELECT id FROM test_scenarios WHERE bot_id = :b) OR entity_id IN "
            "(SELECT id FROM channel_configs WHERE bot_id = :b)"), {"b": bot_id})
        for table in ("releases", "test_scenarios", "channel_configs", "voice_bot_settings",
                      "voice_bot_readiness", "bot_languages", "workflows", "prompts"):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE bot_id = :b"), {"b": bot_id})
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


def _readiness(client, headers, bot_id: str) -> dict[str, bool]:
    bot = _data(client.get(f"{API}/bots/{bot_id}", headers=headers))
    return {r["id"]: r["done"] for r in bot["readiness"]}


def _releases(client, headers, bot_id: str) -> list[dict]:
    return _data(client.get(f"{API}/bots/{bot_id}/releases", headers=headers))


# ── Create ───────────────────────────────────────────────────────────────────


def test_new_bot_has_no_releases(client, tenant_admin, test_bot):
    assert _releases(client, tenant_admin, test_bot["id"]) == []
    assert test_bot["status"] == "draft"


def test_create_release_starts_in_review_with_live_checklist(client, tenant_admin, test_bot):
    rel = _data(client.post(f"{API}/bots/{test_bot['id']}/releases", headers=tenant_admin,
                            json={"version": "v0.1.0", "notes": "first cut"}))
    assert rel["stage"] == "review"
    assert rel["version"] == "v0.1.0"
    assert rel["requestedBy"]
    labels = {c["id"]: c for c in rel["checklist"]}
    assert set(labels) == {"c1", "c2", "c3", "c4", "c5", "c6"}
    # no scenarios yet → regression gate fails with an explanatory detail
    assert labels["c1"]["ok"] is False
    assert labels["c1"]["detail"] == "No scenarios defined"
    # no channel yet → channel gate fails
    assert labels["c5"]["ok"] is False


def test_second_open_release_is_rejected(client, tenant_admin, test_bot):
    msg = _error(client.post(f"{API}/bots/{test_bot['id']}/releases", headers=tenant_admin,
                             json={"version": "v0.1.1"}), 409)
    assert "v0.1.0" in msg and "review" in msg
    assert len(_releases(client, tenant_admin, test_bot["id"])) == 1


# ── Stage transitions & the publish gate ─────────────────────────────────────


def test_approve_records_approver_and_refreshes_checklist(client, tenant_admin, test_bot):
    rel = _releases(client, tenant_admin, test_bot["id"])[0]
    out = _data(client.patch(f"{API}/releases/{rel['id']}", headers=tenant_admin,
                             json={"stage": "approved", "note": "looks good"}))
    assert out["stage"] == "approved"
    assert out["approvedBy"]
    assert len(out["checklist"]) == 6


def test_tenant_admin_publish_is_blocked_by_checklist(client, tenant_admin, test_bot):
    rel = _releases(client, tenant_admin, test_bot["id"])[0]
    msg = _error(client.patch(f"{API}/releases/{rel['id']}", headers=tenant_admin,
                              json={"stage": "published"}), 422)
    assert msg.startswith("Publish blocked")
    assert "All regression tests passing" in msg
    # the override field is a super-admin lever only — ignored for tenant admins
    msg = _error(client.patch(f"{API}/releases/{rel['id']}", headers=tenant_admin,
                              json={"stage": "published",
                                    "overrideReason": "customer approved supervised pilot"}), 422)
    assert msg.startswith("Publish blocked")
    assert "overrideReason" not in msg
    bot = _data(client.get(f"{API}/bots/{test_bot['id']}", headers=tenant_admin))
    assert bot["status"] == "draft"


def test_super_admin_override_needs_a_justification(client, super_admin, test_bot):
    rel = _releases(client, super_admin, test_bot["id"])[0]
    msg = _error(client.patch(f"{API}/releases/{rel['id']}", headers=super_admin,
                              json={"stage": "published"}), 422)
    assert "overrideReason" in msg
    msg = _error(client.patch(f"{API}/releases/{rel['id']}", headers=super_admin,
                              json={"stage": "published", "overrideReason": "short"}), 422)
    assert "overrideReason" in msg


def test_super_admin_override_publishes_and_is_audited(client, super_admin, tenant_admin, test_bot):
    rel = _releases(client, super_admin, test_bot["id"])[0]
    out = _data(client.patch(f"{API}/releases/{rel['id']}", headers=super_admin,
                             json={"stage": "published",
                                   "overrideReason": "Imported tenant; supervised pilot approved."}))
    assert out["stage"] == "published"
    assert out["publishedAt"]

    bot = _data(client.get(f"{API}/bots/{test_bot['id']}", headers=tenant_admin))
    assert bot["status"] == "published"
    assert bot["liveVersion"] == "v0.1.0"

    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import AuditLog

    session = get_sessionmaker()()
    try:
        row = session.scalar(
            select(AuditLog)
            .where(AuditLog.entity_id == rel["id"])
            .order_by(AuditLog.created_at.desc())
        )
        assert row is not None
        assert row.action == "Published release with checklist override"
        assert row.new_value["override"]["reason"].startswith("Imported tenant")
        assert "All regression tests passing" in row.new_value["override"]["failedChecks"]
    finally:
        session.close()


def test_published_release_cannot_be_republished(client, tenant_admin, test_bot):
    rel = _releases(client, tenant_admin, test_bot["id"])[0]
    assert rel["stage"] == "published"
    msg = _error(client.patch(f"{API}/releases/{rel['id']}", headers=tenant_admin,
                              json={"stage": "approved"}), 422)
    assert "cannot move" in msg


# ── Readiness: r6 flush fix and r7 via scenarios ─────────────────────────────


def test_channel_recreate_after_archive_keeps_r6_true(client, tenant_admin, test_bot):
    bot_id = test_bot["id"]
    web = {"config": {"allowedOrigins": ["https://app.example.com"], "widgetColor": "#1A73E8"}}
    assert _readiness(client, tenant_admin, bot_id)["r6"] is False

    _data(client.put(f"{API}/bots/{bot_id}/channels/web", headers=tenant_admin, json=web))
    assert _readiness(client, tenant_admin, bot_id)["r6"] is True

    _data(client.delete(f"{API}/bots/{bot_id}/channels/web", headers=tenant_admin))
    assert _readiness(client, tenant_admin, bot_id)["r6"] is False

    # Restoring the archived row used to evaluate r6 before the un-delete was
    # flushed (autoflush=False), leaving the flag stuck at False.
    _data(client.put(f"{API}/bots/{bot_id}/channels/web", headers=tenant_admin, json=web))
    assert _readiness(client, tenant_admin, bot_id)["r6"] is True


def test_scenario_create_and_run_flips_r7_and_next_release_checklist(client, tenant_admin, test_bot):
    bot_id = test_bot["id"]
    assert _readiness(client, tenant_admin, bot_id)["r7"] is False

    sc = _data(client.post(f"{API}/bots/{bot_id}/scenarios", headers=tenant_admin,
                           json={"name": f"Greeting smoke {_SUFFIX}", "suite": "Smoke", "steps": 2}))
    assert sc["lastRun"] is None
    # created but never run → still not passing
    assert _readiness(client, tenant_admin, bot_id)["r7"] is False

    run = _data(client.post(f"{API}/bots/{bot_id}/scenarios/run", headers=tenant_admin))
    assert run == {"passed": 1, "failed": 0, "total": 1, "at": run["at"]}
    assert _readiness(client, tenant_admin, bot_id)["r7"] is True

    # A follow-up release is allowed once the previous one is published, and
    # its checklist reflects the now-passing suite and configured channel.
    rel = _data(client.post(f"{API}/bots/{bot_id}/releases", headers=tenant_admin,
                            json={"version": "v0.1.1"}))
    checks = {c["id"]: c["ok"] for c in rel["checklist"]}
    assert checks["c1"] is True
    assert checks["c5"] is True
