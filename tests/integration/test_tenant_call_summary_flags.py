"""Tenant-level Call Summary controls.

Two independent Super Admin switches on the tenant record:

- ``callSummaryEnabled`` — whether post-call summary/outcome/NBA analysis
  runs at all (gates the enqueue at call finalize),
- ``usePreviousCallSummary`` — whether a new call loads the customer's
  latest stored summary (gates the centralized recall service).

Covers the API contract (defaults, create, independent update), the runtime
gating on both ends, the generation-off-but-usage-on combination, tenant
isolation and existing-tenant migration compatibility. Runs against the real
dev MySQL + Mongo like the post-call memory suite; the LLM is stubbed.
"""

import json
import uuid

import pymongo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text as sa_text

from backend.core.security import create_access_token
from backend.main import app
from shared.bot_config import ResolvedBotConfig
from shared.config import get_settings
from shared.db.mysql import get_engine, get_sessionmaker
from shared.models import ConversationMemory, Tenant, User
from voice_runtime.recording import SessionRecorder, TurnRecord

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]

# Ephemeral runtime tenants (created directly, like conftest's factory does).
TENANT_ON = f"tn_test_cs_on_{_SUFFIX}"
TENANT_OFF = f"tn_test_cs_off_{_SUFFIX}"
BOT = "bot-101"  # existing dev bot — satisfies the memory row's bot FK

_created_tenants: list[str] = []
_created_memories: list[str] = []
_created_conversations: list[str] = []


def _bearer(email: str = "admin@aurexion.com") -> dict:
    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _make_tenant(tenant_id: str, *, generate: bool, use_previous: bool) -> None:
    session = get_sessionmaker()()
    try:
        session.add(Tenant(
            id=tenant_id, name=f"CS Test {tenant_id[-4:]}",
            domain=f"{tenant_id}.example.test", status="active",
            call_summary_enabled=generate,
            use_previous_call_summary=use_previous,
        ))
        session.commit()
        _created_tenants.append(tenant_id)
    finally:
        session.close()


def _set_flags(tenant_id: str, *, generate: bool | None = None,
               use_previous: bool | None = None) -> None:
    session = get_sessionmaker()()
    try:
        tenant = session.get(Tenant, tenant_id)
        if generate is not None:
            tenant.call_summary_enabled = generate
        if use_previous is not None:
            tenant.use_previous_call_summary = use_previous
        session.commit()
    finally:
        session.close()


@pytest.fixture(scope="module", autouse=True)
def runtime_tenants():
    _make_tenant(TENANT_ON, generate=True, use_previous=True)
    _make_tenant(TENANT_OFF, generate=False, use_previous=False)
    yield
    settings = get_settings()
    mongo = pymongo.MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=4000)
    with get_engine().begin() as conn:
        if _created_memories:
            conn.execute(sa_text(
                "DELETE FROM conversation_memories WHERE id IN :ids"
            ).bindparams(ids=tuple(_created_memories)))
        if _created_conversations:
            conn.execute(sa_text(
                "DELETE FROM usage_events WHERE session_id IN ("
                "SELECT session_id FROM conversation_sessions WHERE id IN :ids)"
            ).bindparams(ids=tuple(_created_conversations)))
            conn.execute(sa_text(
                "DELETE FROM conversation_sessions WHERE id IN :ids"
            ).bindparams(ids=tuple(_created_conversations)))
        if _created_tenants:
            # Children created by the tenant-create API / call metering first,
            # then the tenant rows themselves.
            conn.execute(sa_text(
                "DELETE FROM usage_events WHERE tenant_id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
            conn.execute(sa_text(
                "DELETE FROM usage_records WHERE tenant_id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
            conn.execute(sa_text(
                "DELETE FROM users WHERE tenant_id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
            conn.execute(sa_text(
                "DELETE FROM subscriptions WHERE tenant_id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
            conn.execute(sa_text(
                "DELETE FROM tenant_settings WHERE tenant_id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
            conn.execute(sa_text(
                "DELETE FROM audit_logs WHERE tenant_id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
            conn.execute(sa_text(
                "DELETE FROM tenants WHERE id IN :ids"
            ).bindparams(ids=tuple(_created_tenants)))
    if _created_conversations:
        mongo[settings.mongodb_database]["conversation_transcripts"].delete_many(
            {"control_plane_id": {"$in": _created_conversations}}
        )
    mongo.close()


# ── API contract ─────────────────────────────────────────────────────────────


def _create_payload(**overrides) -> dict:
    unique = uuid.uuid4().hex[:8]
    payload = {
        "name": f"Summary Flags Co {unique}",
        "domain": f"summary-{unique}.echotest.io",
        "planCode": "starter",
        "adminEmail": f"admin@summary-{unique}.echotest.io",
        "status": "active",
    }
    payload.update(overrides)
    return payload


def _track_tenant(data: dict) -> dict:
    _created_tenants.append(data["id"])
    return data


class TestTenantApi:
    def test_new_tenant_defaults_both_flags_to_false(self):
        with TestClient(app) as client:
            response = client.post(f"{API}/tenants", headers=_bearer(),
                                   json=_create_payload())
        assert response.status_code == 201
        data = _track_tenant(response.json()["data"])
        assert data["callSummaryEnabled"] is False
        assert data["usePreviousCallSummary"] is False

    def test_create_with_call_summary_enabled(self):
        with TestClient(app) as client:
            response = client.post(
                f"{API}/tenants", headers=_bearer(),
                json=_create_payload(callSummaryEnabled=True),
            )
            assert response.status_code == 201
            data = _track_tenant(response.json()["data"])
            assert data["callSummaryEnabled"] is True
            assert data["usePreviousCallSummary"] is False
            # Persisted, not just echoed.
            detail = client.get(f"{API}/tenants/{data['id']}", headers=_bearer())
        assert detail.json()["data"]["callSummaryEnabled"] is True

    def test_update_toggles_each_flag_independently(self):
        with TestClient(app) as client:
            created = _track_tenant(client.post(
                f"{API}/tenants", headers=_bearer(), json=_create_payload(),
            ).json()["data"])
            tenant_id = created["id"]

            # Enable ONLY previous-summary usage.
            response = client.patch(
                f"{API}/tenants/{tenant_id}", headers=_bearer(),
                json={"usePreviousCallSummary": True},
            )
            data = response.json()["data"]
            assert data["usePreviousCallSummary"] is True
            assert data["callSummaryEnabled"] is False

            # Enable generation, then disable usage — both persist.
            client.patch(f"{API}/tenants/{tenant_id}", headers=_bearer(),
                         json={"callSummaryEnabled": True})
            client.patch(f"{API}/tenants/{tenant_id}", headers=_bearer(),
                         json={"usePreviousCallSummary": False})
            detail = client.get(
                f"{API}/tenants/{tenant_id}", headers=_bearer()
            ).json()["data"]
        assert detail["callSummaryEnabled"] is True
        assert detail["usePreviousCallSummary"] is False

    def test_non_boolean_value_rejected(self):
        with TestClient(app) as client:
            created = _track_tenant(client.post(
                f"{API}/tenants", headers=_bearer(), json=_create_payload(),
            ).json()["data"])
            response = client.patch(
                f"{API}/tenants/{created['id']}", headers=_bearer(),
                json={"callSummaryEnabled": "sometimes"},
            )
        assert response.status_code == 422

    def test_existing_tenant_rows_read_as_disabled(self):
        """Migration compatibility: a tenant row written without the new
        columns (ORM default path) reads back as both-off via the API."""
        tenant_id = f"tn_test_cs_mig_{uuid.uuid4().hex[:8]}"
        session = get_sessionmaker()()
        try:
            session.add(Tenant(id=tenant_id, name="Legacy Tenant",
                               domain=f"{tenant_id}.example.test", status="active"))
            session.commit()
            _created_tenants.append(tenant_id)
        finally:
            session.close()
        with TestClient(app) as client:
            data = client.get(
                f"{API}/tenants/{tenant_id}", headers=_bearer()
            ).json()["data"]
        assert data["callSummaryEnabled"] is False
        assert data["usePreviousCallSummary"] is False


# ── Runtime gating ───────────────────────────────────────────────────────────


def _config(tenant: str) -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id=tenant, bot_id=BOT, bot_name="Flag Test Bot", version="v1",
        published=True, language="hi-IN", languages=["en-IN", "hi-IN"],
        stt={"provider": "mock"}, tts={"provider": "mock"},
        llm={"provider": "mock", "model": "mock"},
        system_prompt="You are Recovery Bot.",
    )


def _recorder(tenant: str, caller: str) -> SessionRecorder:
    recorder = SessionRecorder(
        f"vs_test_{uuid.uuid4().hex[:10]}", _config(tenant),
        channel="vaani", caller=caller,
    )
    recorder.add_turn(TurnRecord(role="bot", text="क्या आज payment कर पाएँगे?"))
    recorder.add_turn(TurnRecord(role="user", text="मैं दो हज़ार सोमवार को दूँगा।"))
    recorder.disposition = "promise_to_pay"
    return recorder


async def _finalize(recorder: SessionRecorder) -> None:
    from shared.db.mongo import Mongo

    await Mongo.connect()
    await recorder.finalize("completed")
    _created_conversations.append(recorder.control_plane_id)
    session = get_sessionmaker()()
    try:
        row_id = session.execute(
            select(ConversationMemory.id).where(
                ConversationMemory.conversation_id == recorder.control_plane_id
            )
        ).scalar_one_or_none()
        if row_id:
            _created_memories.append(row_id)
    finally:
        session.close()


def _memory_row(conversation_id: str) -> ConversationMemory | None:
    session = get_sessionmaker()()
    try:
        return session.execute(
            select(ConversationMemory).where(
                ConversationMemory.conversation_id == conversation_id
            )
        ).scalar_one_or_none()
    finally:
        session.close()


ANALYSIS = {
    "call_outcome": "promise_to_pay",
    "summary": "Customer committed to paying 2000 INR on Monday.",
    "customer_intent": "delay_payment",
    "customer_sentiment": "cooperative",
    "customer_commitments": [{
        "type": "payment", "amount": 2000, "currency": "INR",
        "raw_due_expression": "सोमवार", "status": "promised",
        "description": "pay two thousand rupees on Monday",
    }],
    "next_best_action": {"action": "follow_up_on_commitment",
                         "reason": "Open commitment", "priority": "medium"},
    "follow_up_required": True,
    "confidence": 0.9,
    "dominant_language": "hi-IN",
    "last_customer_language": "hi-IN",
}


class _AnalysisLLMStub:
    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        class _Result:
            text = json.dumps(ANALYSIS)
            input_tokens = 400
            output_tokens = 180

        return _Result()


async def _process_all(monkeypatch, tenant: str):
    from shared.post_call import processor

    async def _fake_resolve(bot_id, require_published=False):
        return _config(tenant)

    monkeypatch.setattr("shared.bot_config.resolve_bot_config", _fake_resolve)
    monkeypatch.setattr(processor, "build_analysis_llm",
                        lambda config: _AnalysisLLMStub())
    return await processor.run_pending_once(limit=20)


class TestPostCallGating:
    async def test_disabled_tenant_skips_summary_generation(self):
        recorder = _recorder(TENANT_OFF, "+91 97000 11111")
        await _finalize(recorder)
        # Transcript + control-plane row persisted normally…
        session = get_sessionmaker()()
        try:
            conversation = session.execute(sa_text(
                "SELECT id FROM conversation_sessions WHERE id = :i"
            ).bindparams(i=recorder.control_plane_id)).scalar_one_or_none()
        finally:
            session.close()
        assert conversation is not None
        # …but NO analysis job exists.
        assert _memory_row(recorder.control_plane_id) is None

    async def test_enabled_tenant_generates_summary(self, monkeypatch):
        recorder = _recorder(TENANT_ON, "+91 97000 22222")
        await _finalize(recorder)
        row = _memory_row(recorder.control_plane_id)
        assert row is not None and row.status == "queued"
        await _process_all(monkeypatch, TENANT_ON)
        row = _memory_row(recorder.control_plane_id)
        assert row.status == "completed"
        assert row.summary


class TestRecallGating:
    async def test_disabled_usage_never_loads_stored_memory(self, monkeypatch):
        """Having an old summary in the DB must not make the runtime use it —
        this tenant generated one but has usage switched off."""
        from shared.post_call.recall import load_previous_memory

        caller = "+91 97000 33333"
        _set_flags(TENANT_ON, use_previous=False)
        try:
            recorder = _recorder(TENANT_ON, caller)
            await _finalize(recorder)
            await _process_all(monkeypatch, TENANT_ON)
            assert _memory_row(recorder.control_plane_id).status == "completed"
            assert await load_previous_memory(TENANT_ON, BOT, phone=caller) is None
        finally:
            _set_flags(TENANT_ON, use_previous=True)

    async def test_enabled_usage_loads_latest_memory(self, monkeypatch):
        from shared.post_call.recall import load_previous_memory

        caller = "+91 97000 44444"
        recorder = _recorder(TENANT_ON, caller)
        await _finalize(recorder)
        await _process_all(monkeypatch, TENANT_ON)
        memory = await load_previous_memory(TENANT_ON, BOT, phone=caller)
        assert memory is not None
        assert memory.call_outcome == "promise_to_pay"
        assert memory.open_commitments[0]["amount"] == 2000.0

    async def test_stored_memory_usable_while_generation_disabled(self, monkeypatch):
        """callSummaryEnabled=false + usePreviousCallSummary=true is valid:
        no NEW summaries, but the existing one keeps serving new calls."""
        from shared.post_call.recall import load_previous_memory

        caller = "+91 97000 55555"
        first = _recorder(TENANT_ON, caller)
        await _finalize(first)
        await _process_all(monkeypatch, TENANT_ON)

        _set_flags(TENANT_ON, generate=False, use_previous=True)
        try:
            # New call: nothing new is enqueued…
            second = _recorder(TENANT_ON, caller)
            await _finalize(second)
            assert _memory_row(second.control_plane_id) is None
            # …but the previously stored memory still loads.
            memory = await load_previous_memory(TENANT_ON, BOT, phone=caller)
            assert memory is not None
            assert memory.conversation_id == first.control_plane_id
        finally:
            _set_flags(TENANT_ON, generate=True, use_previous=True)

    async def test_tenant_isolation_with_flags_enabled(self, monkeypatch):
        """Both tenants opted in: the other tenant still never sees this
        caller's memory — scope beats phone number."""
        from shared.post_call.recall import load_previous_memory

        caller = "+91 97000 66666"
        recorder = _recorder(TENANT_ON, caller)
        await _finalize(recorder)
        await _process_all(monkeypatch, TENANT_ON)
        _set_flags(TENANT_OFF, generate=True, use_previous=True)
        try:
            assert await load_previous_memory(TENANT_ON, BOT, phone=caller) is not None
            assert await load_previous_memory(TENANT_OFF, BOT, phone=caller) is None
        finally:
            _set_flags(TENANT_OFF, generate=False, use_previous=False)

    async def test_unknown_tenant_fails_closed(self):
        from shared.post_call.recall import load_previous_memory

        assert await load_previous_memory(
            "tn_does_not_exist", BOT, phone="+91 97000 77777"
        ) is None
