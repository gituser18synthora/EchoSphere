"""Post-call memory end-to-end: enqueue at finalize, processing, recall, API.

Runs against the real dev MySQL + Mongo, mirroring the conversation
transcripts suite. The LLM is stubbed at the processor boundary — these tests
pin the durable machinery, idempotency, scoping and precedence rules, not the
model's prose.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pymongo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text as sa_text

from backend.core.security import create_access_token
from backend.main import app
from shared.bot_config import ResolvedBotConfig
from shared.config import get_settings
from shared.db.mysql import get_engine, get_sessionmaker
from shared.models import ConversationMemory
from voice_runtime.recording import SessionRecorder, TurnRecord

pytestmark = pytest.mark.integration

_SUFFIX = uuid.uuid4().hex[:6]
TENANT_A = "tn-001"
BOT_A = "bot-101"
TENANT_B = "tn-002"
CALLER = "+91 98111 22333"  # tail 9811122333


def _bearer(email: str) -> dict:
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _config(tenant=TENANT_A, bot=BOT_A) -> ResolvedBotConfig:
    return ResolvedBotConfig(
        tenant_id=tenant, bot_id=bot, bot_name="Memory Test Bot", version="v1",
        published=True, language="hi-IN", languages=["en-IN", "hi-IN"],
        stt={"provider": "mock"}, tts={"provider": "mock"},
        llm={"provider": "mock", "model": "mock"},
        system_prompt="You are Recovery Bot.",
    )


def _recorder(*, tenant=TENANT_A, bot=BOT_A, caller=CALLER) -> SessionRecorder:
    recorder = SessionRecorder(
        f"vs_test_{uuid.uuid4().hex[:10]}", _config(tenant, bot),
        channel="vaani", caller=caller,
    )
    recorder.add_turn(TurnRecord(role="bot", text="क्या आज payment कर पाएँगे?"))
    recorder.add_turn(TurnRecord(role="user", text="नहीं, आज नहीं। मैं दो हज़ार सोमवार को दूँगा।"))
    recorder.disposition = "promise_to_pay"
    return recorder


_created_memories: list[str] = []
_created_conversations: list[str] = []


async def _finalize(recorder: SessionRecorder, reason="completed") -> None:
    from shared.db.mongo import Mongo

    await Mongo.connect()  # idempotent; finalize writes the transcript doc
    await recorder.finalize(reason)
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


@pytest.fixture(scope="module", autouse=True)
def enable_tenant_summary_flags():
    """Post-call generation and recall are tenant opt-in (both flags default
    to false) — this suite exercises the machinery itself, so switch both on
    for the two dev tenants and restore the previous values afterwards."""
    with get_engine().begin() as conn:
        previous = {
            row[0]: (row[1], row[2])
            for row in conn.execute(sa_text(
                "SELECT id, call_summary_enabled, use_previous_call_summary "
                "FROM tenants WHERE id IN :ids"
            ).bindparams(ids=(TENANT_A, TENANT_B)))
        }
        conn.execute(sa_text(
            "UPDATE tenants SET call_summary_enabled=1, "
            "use_previous_call_summary=1 WHERE id IN :ids"
        ).bindparams(ids=(TENANT_A, TENANT_B)))
    yield
    with get_engine().begin() as conn:
        for tenant_id, (generate, use_previous) in previous.items():
            conn.execute(sa_text(
                "UPDATE tenants SET call_summary_enabled=:g, "
                "use_previous_call_summary=:u WHERE id=:i"
            ).bindparams(g=generate, u=use_previous, i=tenant_id))


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    settings = get_settings()
    client = pymongo.MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=4000)
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
    sessions = [
        cv for cv in _created_conversations
    ]
    if sessions:
        client[settings.mongodb_database]["conversation_transcripts"].delete_many(
            {"control_plane_id": {"$in": sessions}}
        )
    client.close()


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


class _AnalysisLLMStub:
    """Scripted post-call analysis result (and a failing variant)."""

    def __init__(self, payload: dict | None = None, fail=False):
        self._payload = payload
        self._fail = fail

    async def generate(self, messages, *, system=None, temperature=None,
                       max_tokens=None, tools=None):
        if self._fail:
            raise RuntimeError("provider down")

        class _Result:
            text = json.dumps(self._payload)
            input_tokens = 400
            output_tokens = 180

        return _Result()


GOOD_ANALYSIS = {
    "call_outcome": "promise_to_pay",
    "summary": "Customer cannot pay today and committed to paying ₹2,000 on Monday.",
    "customer_intent": "delay_payment",
    "customer_sentiment": "cooperative",
    "customer_commitments": [{
        "type": "payment", "amount": 2000, "currency": "INR",
        "raw_due_expression": "सोमवार", "status": "promised",
        "description": "pay two thousand rupees on Monday",
    }],
    "unresolved_items": ["remaining_balance"],
    "resolved_items": ["identity_confirmation"],
    "collected_slots": {"identity_confirmed": "yes"},
    "next_best_action": {"action": "follow_up_on_commitment",
                         "reason": "Open commitment", "priority": "medium"},
    "follow_up_required": True,
    "confidence": 0.92,
    "dominant_language": "hi-IN",
    "last_customer_language": "hi-IN",
}


async def _process_all(monkeypatch, payload=GOOD_ANALYSIS, fail=False):
    from shared.post_call import processor

    async def _fake_resolve(bot_id, require_published=False):
        return _config(bot=bot_id)

    monkeypatch.setattr(
        "shared.bot_config.resolve_bot_config", _fake_resolve
    )
    monkeypatch.setattr(
        processor, "build_analysis_llm",
        lambda config: _AnalysisLLMStub(payload, fail=fail),
    )
    return await processor.run_pending_once(limit=20)


class TestEnqueueIdempotency:
    async def test_completed_call_generates_exactly_one_queued_row(self):
        recorder = _recorder()
        await _finalize(recorder)
        row = _memory_row(recorder.control_plane_id)
        assert row is not None
        assert row.status == "queued"
        assert row.phone_tail == "9811122333"
        assert row.final_state["disposition"] == "promise_to_pay"

    async def test_duplicate_hangup_creates_no_second_record(self):
        recorder = _recorder()
        await _finalize(recorder)
        # The same disconnect delivered again → finalize runs again.
        await recorder.finalize("completed")
        session = get_sessionmaker()()
        try:
            count = session.execute(
                select(ConversationMemory).where(
                    ConversationMemory.conversation_id == recorder.control_plane_id
                )
            ).scalars().all()
        finally:
            session.close()
        assert len(count) == 1

    async def test_silent_call_enqueues_nothing(self):
        recorder = SessionRecorder(
            f"vs_test_{uuid.uuid4().hex[:10]}", _config(), channel="vaani",
            caller=CALLER,
        )
        await _finalize(recorder)
        assert _memory_row(recorder.control_plane_id) is None


class TestProcessing:
    async def test_analysis_persists_structured_memory(self, monkeypatch):
        recorder = _recorder()
        await _finalize(recorder)
        await _process_all(monkeypatch)
        row = _memory_row(recorder.control_plane_id)
        assert row.status == "completed"
        assert row.call_outcome == "promise_to_pay"
        assert "₹2,000" in row.summary
        commitment = row.memory["customer_commitments"][0]
        assert commitment["amount"] == 2000.0
        # "सोमवार" resolved to an absolute Monday after the call date.
        due = datetime.fromisoformat(commitment["due_date"])
        assert due.weekday() == 0
        assert row.next_action == "follow_up_on_commitment"
        assert row.follow_up_required is True
        assert row.follow_up_at is not None
        assert float(row.confidence) == pytest.approx(0.92)

    async def test_already_paid_claim_never_becomes_verified(self, monkeypatch):
        recorder = _recorder()
        recorder.disposition = "payment_claimed"
        recorder.call_state = {
            "payment_verification": {"outcome": "unverified"},
        }
        await _finalize(recorder)
        payload = dict(GOOD_ANALYSIS)
        payload.update({
            "call_outcome": "payment_claimed",
            "customer_commitments": [],
            # Even if the model tries to claim verification…
            "resolved_items": ["payment_verification"],
            "next_best_action": {"action": "close_goal_completed"},
        })
        await _process_all(monkeypatch, payload=payload)
        row = _memory_row(recorder.control_plane_id)
        # …the deterministic layer forces verification first.
        assert row.next_action == "verify_previous_payment"

    async def test_llm_failure_retries_then_fails_with_fallback(self, monkeypatch):
        recorder = _recorder()
        await _finalize(recorder)
        row = _memory_row(recorder.control_plane_id)
        session = get_sessionmaker()()
        try:
            db_row = session.get(ConversationMemory, row.id)
            db_row.max_attempts = 2
            session.commit()
        finally:
            session.close()

        await _process_all(monkeypatch, fail=True)  # attempt 1 → requeued
        row = _memory_row(recorder.control_plane_id)
        assert row.status == "queued" and row.attempts == 1
        await _process_all(monkeypatch, fail=True)  # attempt 2 → terminal
        row = _memory_row(recorder.control_plane_id)
        assert row.status == "failed"
        assert row.error
        # Deterministic fallback memory is still available for the next call.
        assert row.memory is not None
        assert row.memory["source"] == "fallback"
        assert row.next_action  # rules-based NBA present
        # The transcript survived untouched.
        settings = get_settings()
        client = pymongo.MongoClient(settings.mongodb_uri,
                                     serverSelectionTimeoutMS=4000)
        doc = client[settings.mongodb_database]["conversation_transcripts"].find_one(
            {"session_id": recorder.session_id}
        )
        client.close()
        assert doc is not None and len(doc["turns"]) == 2


class TestRecall:
    async def test_next_call_loads_previous_memory_by_phone(self, monkeypatch):
        from shared.post_call.recall import load_previous_memory

        caller = "+91 98222 33444"
        recorder = _recorder(caller=caller)
        await _finalize(recorder)
        await _process_all(monkeypatch)
        memory = await load_previous_memory(
            TENANT_A, BOT_A, phone=caller,
            exclude_session_id="vs_new_call",
        )
        assert memory is not None
        assert memory.call_outcome == "promise_to_pay"
        assert memory.open_commitments[0]["amount"] == 2000.0
        assert memory.preferred_language() == "hi-IN"
        assert "override this memory" in memory.prompt_section()

    async def test_tenant_isolation(self, monkeypatch):
        from shared.post_call.recall import load_previous_memory

        caller = "+91 98333 44555"
        recorder = _recorder(caller=caller)
        await _finalize(recorder)
        await _process_all(monkeypatch)
        # Same phone under another tenant: nothing.
        assert await load_previous_memory(TENANT_B, BOT_A, phone=caller) is None

    async def test_bot_scoping(self, monkeypatch):
        from shared.post_call.recall import load_previous_memory

        caller = "+91 98444 55666"
        recorder = _recorder(caller=caller)
        await _finalize(recorder)
        await _process_all(monkeypatch)
        # Same tenant, different bot: memory follows the bot scope.
        assert await load_previous_memory(
            TENANT_A, "bot-other", phone=caller
        ) is None

    async def test_immediate_callback_skips_processing_row(self):
        from shared.post_call.recall import load_previous_memory

        recorder = _recorder(caller="+91 90000 11111")
        await _finalize(recorder)  # queued, NOT yet processed
        memory = await load_previous_memory(
            TENANT_A, BOT_A, phone="+91 90000 11111",
        )
        assert memory is None  # never blocks, never crashes — just no memory

    async def test_current_session_is_never_its_own_memory(self, monkeypatch):
        from shared.post_call.recall import load_previous_memory

        recorder = _recorder(caller="+91 90000 22222")
        await _finalize(recorder)
        await _process_all(monkeypatch)
        assert await load_previous_memory(
            TENANT_A, BOT_A, phone="+91 90000 22222",
            exclude_session_id=recorder.session_id,
        ) is None


async def _release_mongo():
    """Unbind the global Motor client from the pytest loop so the TestClient
    app can rebind it to its own portal loop (Motor clients are loop-bound)."""
    from shared.db.mongo import Mongo

    await Mongo.disconnect()


class TestApi:
    async def test_detail_exposes_summary_and_nba(self, monkeypatch):
        recorder = _recorder()
        await _finalize(recorder)
        await _process_all(monkeypatch)
        await _release_mongo()
        headers = _bearer("priya.sharma@meridianhealth.com")
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/conversations/{recorder.control_plane_id}",
                headers=headers,
            )
        body = response.json()
        assert body["success"] is True
        summary = body["data"]["summary"]
        assert summary["status"] == "completed"
        assert summary["callOutcome"] == "promise_to_pay"
        assert summary["nextBestAction"]["action"] == "follow_up_on_commitment"
        assert summary["customerCommitments"][0]["amount"] == 2000.0
        assert summary["followUpRequired"] is True

    async def test_other_tenant_cannot_read_memory(self, monkeypatch):
        recorder = _recorder()
        await _finalize(recorder)
        await _process_all(monkeypatch)
        await _release_mongo()
        headers = _bearer("admin@pokket.com")  # tenant B
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/conversations/{recorder.control_plane_id}",
                headers=headers,
            )
        assert response.status_code == 404

    async def test_unauthenticated_request_is_rejected(self):
        await _release_mongo()
        with TestClient(app) as client:
            response = client.get("/api/v1/conversations/cv_whatever")
        assert response.status_code in (401, 403)

    async def test_queued_row_reports_processing_state(self):
        recorder = _recorder()
        await _finalize(recorder)  # not processed yet
        await _release_mongo()
        headers = _bearer("priya.sharma@meridianhealth.com")
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/conversations/{recorder.control_plane_id}",
                headers=headers,
            )
        summary = response.json()["data"]["summary"]
        assert summary["status"] == "queued"
        assert summary["summary"] is None
