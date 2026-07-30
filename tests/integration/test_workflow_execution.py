"""Saved workflows end to end: persistence API validation, and the chat-test
endpoint executing the SAME saved definition through the runtime router +
workflow engine (multi-turn state, branching, slots, tenant isolation).

Live-app harness; all rows uniquely suffixed and removed in teardown.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_created: list[tuple[str, str]] = []


def _session():
    from shared.db.mysql import get_sessionmaker

    return get_sessionmaker()()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    engine = get_engine()
    for table, row_id in reversed(_created):
        try:
            with engine.begin() as conn:
                conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


@pytest.fixture(scope="module")
def env(client):
    """Tenant + admin user + bot + intent routed to a saved workflow."""
    from shared.ids import new_id
    from shared.models import Intent, Role, Tenant, User, VoiceBot

    session = _session()
    try:
        role = session.execute(select(Role).where(Role.code == "tenant_admin")).scalar_one()
        tenant = Tenant(id=new_id("tn"), name=f"WF Test {_SUFFIX}", code=f"wft_{_SUFFIX}",
                        domain=f"wf-{_SUFFIX}.example.test", status="active")
        session.add(tenant)
        session.flush()
        user = User(id=new_id("usr"), email=f"wf.admin.{_SUFFIX}@example.test",
                    name="WF Admin", password_hash="x", role_id=role.id,
                    tenant_id=tenant.id, status="active")
        bot = VoiceBot(id=new_id("bot"), tenant_id=tenant.id, name=f"WF Bot {_SUFFIX}")
        intent = Intent(
            id=new_id("in"), tenant_id=tenant.id, bot_id=bot.id,
            name="setup_plan", code=f"setup_plan_{_SUFFIX}",
            samples=["set up a payment plan", "i need a plan", "plan banao"],
            confidence_threshold=0.05, route="workflow:collections_plan",
        )
        session.add_all([user, bot, intent])
        session.commit()
        _created.extend([
            ("tenants", tenant.id), ("voice_bots", bot.id),
            ("users", user.id), ("intents", intent.id),
        ])
        token = create_access_token(user_id=user.id, role="tenant_admin", tenant_id=tenant.id)
        return {
            "tenant_id": tenant.id, "bot_id": bot.id,
            "headers": {"Authorization": f"Bearer {token}"},
        }
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success"), body
    return body["data"]


WORKFLOW_DOC = {
    "name": "Collections plan",
    "nodes": [
        {"id": "n1", "kind": "start", "label": "Call starts", "x": 40, "y": 40},
        {"id": "n2", "kind": "message", "label": "Greeting", "x": 40, "y": 150,
         "config": {"text": "I can set up a payment plan for you."}},
        {"id": "n3", "kind": "ask", "label": "Ask amount", "x": 40, "y": 260,
         "config": {"question": "How much can you pay per month?",
                    "variable": "amount", "entityType": "number"}},
        {"id": "n4", "kind": "condition", "label": "Check amount", "x": 260, "y": 260,
         "config": {"variable": "amount", "operator": "gte", "value": 1000}},
        {"id": "n5", "kind": "end", "label": "End call", "x": 480, "y": 200,
         "config": {"text": "Your plan is registered. Goodbye!"}},
        {"id": "n6", "kind": "handover", "label": "Agent", "x": 480, "y": 330,
         "config": {"text": "An agent needs to approve that amount."}},
    ],
    "edges": [
        {"id": "e1", "from": "n1", "to": "n2"},
        {"id": "e2", "from": "n2", "to": "n3"},
        {"id": "e3", "from": "n3", "to": "n4"},
        {"id": "e4", "from": "n4", "to": "n5", "label": "true"},
        {"id": "e5", "from": "n4", "to": "n6", "label": "false"},
    ],
}


# ── persistence + validation ──────────────────────────────────────────────────


def test_save_and_reload_workflow(client, env):
    saved = _data(client.put(f"{API}/bots/{env['bot_id']}/workflow",
                             headers=env["headers"], json=WORKFLOW_DOC))
    _created.append(("workflows", saved["id"]))
    assert saved["name"] == "Collections plan"
    assert len(saved["nodes"]) == 6 and len(saved["edges"]) == 5
    assert saved["issues"] == []  # structurally clean

    reloaded = _data(client.get(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"]))
    assert reloaded["id"] == saved["id"]
    assert [n["id"] for n in reloaded["nodes"]] == ["n1", "n2", "n3", "n4", "n5", "n6"]
    assert reloaded["edges"][3]["label"] == "true"
    assert reloaded["nodes"][2]["config"]["variable"] == "amount"


def test_structural_errors_are_rejected(client, env):
    bad_edge = {**WORKFLOW_DOC, "edges": WORKFLOW_DOC["edges"] + [
        {"id": "eX", "from": "n1", "to": "missing_node"},
    ]}
    r = client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"], json=bad_edge)
    assert r.status_code == 422
    assert any("doesn't exist" in e["message"] for e in r.json()["errors"])

    two_starts = {**WORKFLOW_DOC, "nodes": WORKFLOW_DOC["nodes"] + [
        {"id": "n7", "kind": "start", "label": "Another start", "x": 0, "y": 0},
    ]}
    r = client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"], json=two_starts)
    assert r.status_code == 422
    assert any("one start node" in e["message"] for e in r.json()["errors"])

    unknown_kind = {**WORKFLOW_DOC, "nodes": WORKFLOW_DOC["nodes"][:-1] + [
        {"id": "n6", "kind": "teleport", "label": "??", "x": 0, "y": 0},
    ]}
    r = client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"], json=unknown_kind)
    assert r.status_code == 422


def test_soft_issues_are_computed_server_side(client, env):
    orphan = {
        "nodes": WORKFLOW_DOC["nodes"] + [
            {"id": "n9", "kind": "message", "label": "Orphan", "x": 700, "y": 40},
        ],
        "edges": WORKFLOW_DOC["edges"],
        # Client-supplied issues must be ignored, not stored.
        "issues": [{"nodeId": "n1", "level": "error", "message": "stale client issue"}],
    }
    saved = _data(client.put(f"{API}/bots/{env['bot_id']}/workflow",
                             headers=env["headers"], json=orphan))
    messages = [i["message"] for i in saved["issues"]]
    assert any("Not connected to the start node" in m for m in messages)
    assert not any("stale client issue" in m for m in messages)
    # Restore the clean document for the execution tests below.
    _data(client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"],
                     json=WORKFLOW_DOC))


# ── execution through the chat tester (real router + engine) ─────────────────


def _chat(client, env, message, session=None):
    r = client.post(f"{API}/bots/{env['bot_id']}/testing/chat", headers=env["headers"],
                    json={"message": message, **({"sessionId": session} if session else {})})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_saved_workflow_executes_end_to_end(client, env):
    first = _chat(client, env, "i need a plan")
    session = first["sessionId"]
    assert first["route"] == "workflow"
    assert first["matchedIntent"] == "setup_plan"
    assert first["workflow"]["source"] == "definition"
    assert first["workflow"]["nodeTrace"] == ["n1", "n2", "n3"]
    assert "payment plan" in first["reply"] and "How much can you pay" in first["reply"]
    assert first["done"] is False and first["activeWorkflow"] == "collections_plan"

    second = _chat(client, env, "I can do 2500 per month", session=session)
    assert second["route"] == "workflow"  # continuation, no intent needed
    assert second["workflow"]["nodeTrace"] == ["n3", "n4", "n5"]
    assert second["workflow"]["slots"]["amount"] == "2500"
    assert second["done"] is True
    assert "Goodbye" in second["reply"]


def test_condition_false_branch_hands_off(client, env):
    first = _chat(client, env, "set up a payment plan")
    second = _chat(client, env, "only 200", session=first["sessionId"])
    assert second["workflow"]["nodeTrace"] == ["n3", "n4", "n6"]
    assert second["workflow"]["status"] == "handoff"
    assert "agent" in second["reply"].lower()


def test_saving_changes_updates_execution(client, env):
    changed = {**WORKFLOW_DOC, "nodes": [
        n if n["id"] != "n2" else
        {**n, "config": {"text": "UPDATED GREETING for the new flow."}}
        for n in WORKFLOW_DOC["nodes"]
    ]}
    _data(client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"], json=changed))
    result = _chat(client, env, "i need a plan")
    assert "UPDATED GREETING" in result["reply"]
    # Restore.
    _data(client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=env["headers"],
                     json=WORKFLOW_DOC))


def test_workflow_state_isolated_per_session(client, env):
    a = _chat(client, env, "i need a plan")
    b = _chat(client, env, "i need a plan")
    assert a["sessionId"] != b["sessionId"]
    done_a = _chat(client, env, "3000", session=a["sessionId"])
    assert done_a["done"] is True
    # Session B is still waiting for its own amount.
    done_b = _chat(client, env, "500", session=b["sessionId"])
    assert done_b["workflow"]["nodeTrace"][-1] == "n6"  # false branch, unaffected by A


def test_cross_tenant_access_rejected(client, env):
    from shared.ids import new_id
    from shared.models import Role, Tenant, User

    session = _session()
    try:
        role = session.execute(select(Role).where(Role.code == "tenant_admin")).scalar_one()
        other = Tenant(id=new_id("tn"), name=f"Other {_SUFFIX}", code=f"oth_{_SUFFIX}",
                       domain=f"oth-{_SUFFIX}.example.test", status="active")
        session.add(other)
        session.flush()
        intruder = User(id=new_id("usr"), email=f"intruder.{_SUFFIX}@example.test",
                        name="Intruder", password_hash="x", role_id=role.id,
                        tenant_id=other.id, status="active")
        session.add(intruder)
        session.commit()
        _created.extend([("tenants", other.id), ("users", intruder.id)])
        headers = {"Authorization": f"Bearer {create_access_token(user_id=intruder.id, role='tenant_admin', tenant_id=other.id)}"}
    finally:
        session.close()

    assert client.get(f"{API}/bots/{env['bot_id']}/workflow", headers=headers).status_code == 404
    assert client.post(f"{API}/bots/{env['bot_id']}/testing/chat", headers=headers,
                       json={"message": "hello"}).status_code == 404
    assert client.put(f"{API}/bots/{env['bot_id']}/workflow", headers=headers,
                      json=WORKFLOW_DOC).status_code == 404
