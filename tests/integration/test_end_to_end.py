"""The end-to-end flow (spec §28): admin login → KB → upload → ingest → assign →
publish → voice session with KB answer, greeting skip, barge-in, workflow,
handoff → transcripts/events/usage persisted → data survives restart.

Everything runs against the real local MySQL/PostgreSQL/Redis/MongoDB with
mock speech/LLM providers (no external accounts needed).
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from pipecat.frames.frames import InterruptionFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner

from shared.knowledge.ingestion.pipeline import IngestionPipeline
from backend.main import app
from shared.orchestration.workflow_engine import WorkflowEngine
from shared.providers.base import ProviderConfig
from shared.providers.factory import get_llm_provider, get_tts_provider
from tests.integration.test_voice_pipeline import Collector, make_config
from voice_runtime.brain import ConversationBrain
from voice_runtime.services import EchoTTSService
from voice_runtime.recording import SessionRecorder

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def login(client, email: str, password: str = "Demo@2026!") -> dict:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['token']}"}


async def test_full_flow(client, store, mock_embedder, knowledge_service, pg_cleanup,
                         pdf_bytes, control_plane):
    # 1. Tenant admin logs in (real password auth).
    headers = login(client, "priya.sharma@meridianhealth.com")

    # 2. Creates a knowledge base (assigned to demo bot bot-101 → step 9).
    kb_response = client.post(
        "/api/v1/knowledge", headers=headers,
        json={"name": f"E2E KB {uuid.uuid4().hex[:6]}", "type": "document",
              "scope": "bot", "botId": "bot-101"},
    )
    assert kb_response.status_code == 201
    kb_id = kb_response.json()["data"]["id"]
    control_plane.kb_ids.append(kb_id)

    # 3-4. Uploads a document; ingestion starts.
    upload = client.post(
        f"/api/v1/knowledge/{kb_id}/documents", headers=headers,
        files={"file": ("policy.pdf", pdf_bytes, "application/pdf")},
    )
    assert upload.status_code == 201
    document_id = upload.json()["data"]["documentId"]
    pg_cleanup.append(document_id)

    # 5-8. Parse → chunks → embeddings in pgvector → source ready.
    pipeline = IngestionPipeline(store=store, embedder=mock_embedder)
    while (job_id := await pipeline.claim_next_job()) is not None:
        await pipeline.process_job(job_id)
    status = client.get(
        f"/api/v1/knowledge/documents/{document_id}/status", headers=headers
    ).json()["data"]
    assert status["status"] == "ready" and status["chunkCount"] > 0

    # 10. Publish-path sanity: retrieval works through the shared service.
    search = client.post(
        "/api/v1/knowledge/search-test", headers=headers,
        json={"query": "What is the policy grace period?", "kbIds": [kb_id]},
    ).json()["data"]
    assert search["answerable"] and "30 days" in search["sources"][0]["text"]

    # 11. Voice session issued through the API (trusted mapping).
    session_response = client.post(
        "/api/v1/voice-sessions", headers=headers,
        json={"botId": "bot-101", "channel": "browser"},
    )
    assert session_response.status_code == 201
    session_payload = session_response.json()["data"]
    assert session_payload["wsPath"].startswith("/ws/voice/")

    # 12-25. Mocked voice call through the real Pipecat pipeline.
    config = make_config(kb_ids=[kb_id], tenant_id="tn-001", bot_id="bot-101")
    recorder = SessionRecorder(session_payload["sessionId"], config)
    workflow_engine = WorkflowEngine()
    workflow_engine._checkpointer = MemorySaver()
    brain = ConversationBrain(
        config=config,
        llm=get_llm_provider(ProviderConfig(provider="mock")),
        recorder=recorder,
        knowledge_service=knowledge_service,
        workflow_engine=workflow_engine,
    )
    brain._router._intents = []
    brain._router = type(brain._router)(
        intents=[{"name": "book", "samples": ["book an appointment"],
                  "route": "workflow:appointment_booking", "confidence_threshold": 0.3}],
        has_knowledge_bases=True,
    )
    tts = EchoTTSService(get_tts_provider(ProviderConfig(provider="mock")),
                         sample_rate=16000)
    collector = Collector()
    pipeline_obj = Pipeline([brain, tts, collector])
    worker = PipelineWorker(pipeline_obj, params=PipelineParams(enable_metrics=False),
                            enable_rtvi=False, idle_timeout_secs=None)

    async def feeder(worker):
        await asyncio.sleep(0.2)
        await brain.speak_greeting()
        # 12-15: document question → KB retrieval → grounded answer
        await worker.queue_frame(
            TranscriptionFrame("what is the policy grace period", "caller", "t1"))
        await asyncio.sleep(0.8)
        # 16-17: greeting → KB skipped
        await worker.queue_frame(TranscriptionFrame("thank you", "caller", "t2"))
        await asyncio.sleep(0.5)
        # 18-20: barge-in cancels active work. Turn-taking debounces STT
        # finals, so wait until the turn actually STARTS (its route_decision
        # is recorded before retrieval), then interrupt while the pgvector
        # retrieval (tens of ms) is still in flight.
        await worker.queue_frame(
            TranscriptionFrame("tell me about the renewal policy terms", "caller", "t3"))
        for _ in range(600):
            await asyncio.sleep(0.002)
            if sum(1 for e in recorder.events if e["kind"] == "route_decision") >= 3:
                break
        await worker.queue_frame(InterruptionFrame())
        await asyncio.sleep(0.3)
        # 21-22: multi-step workflow with persisted state
        await worker.queue_frame(
            TranscriptionFrame("I want to book an appointment", "caller", "t4"))
        await asyncio.sleep(0.5)
        await worker.queue_frame(TranscriptionFrame("Asha Verma", "caller", "t5"))
        await asyncio.sleep(0.5)
        # 23-24: human handoff
        await worker.queue_frame(
            TranscriptionFrame("actually transfer me to a human agent", "caller", "t6"))
        await asyncio.sleep(0.5)
        # 25: call ends
        await worker.queue_frame(TranscriptionFrame("hang up", "caller", "t7"))

    runner = WorkerRunner(handle_sigint=False)
    feed_task = asyncio.create_task(feeder(worker))
    await asyncio.wait_for(runner.run(worker), timeout=40)
    await feed_task

    # Assertions over the call
    routes = [e for e in recorder.events if e["kind"] == "route_decision"]
    by_turn = {i: e["route"] for i, e in enumerate(routes)}
    assert by_turn[0] == "knowledge"                      # 13-14
    kb_events = [e for e in recorder.events if e["kind"] == "kb_retrieval"]
    assert kb_events and kb_events[0]["answerable"]       # 14 tenant-authorized sources
    grounded = [t for t in recorder.turns if t.role == "bot" and t.kb_used]
    # 15: the answer is grounded in the uploaded document (cited source + page).
    assert grounded and grounded[0].kb_sources
    assert grounded[0].kb_sources[0]["kbId"] == kb_id
    assert "policy.pdf" in grounded[0].text
    assert by_turn[1] == "chat"                           # 17 greeting skips KB
    assert any(e["kind"] == "generation_cancelled" for e in recorder.events)  # 19-20
    assert by_turn[3] == "workflow"                       # 21
    workflow_replies = [t.text for t in recorder.turns if t.role == "bot"]
    assert any("phone" in t.lower() for t in workflow_replies)  # 22 state persisted
    assert any(e["kind"] == "handoff" for e in recorder.events)  # 23-24

    # 26-28: transcript, events and usage persisted.
    from shared.db.mongo import Mongo

    # Rebind the Motor client to this test's event loop (the TestClient
    # lifespan connected it on its own portal loop).
    await Mongo.connect()
    await recorder.finalize(reason="completed")
    stored = await Mongo.transcripts().find_one({"session_id": recorder.session_id})
    assert stored and stored["turns"] and stored["usage"]["kb_searches"] >= 1
    assert stored["events"]

    # 29: audit events exist for the upload.
    audit = client.get("/api/v1/audit?pageSize=50", headers=headers).json()["data"]
    assert any(a.get("action") == "knowledge.document.upload" for a in audit)

    # 30-31: "restart" — fresh connections still see the data.
    from shared.db.postgres import dispose_pg_engine

    await dispose_pg_engine()
    survived = client.post(
        "/api/v1/knowledge/search-test", headers=headers,
        json={"query": "policy grace period", "kbIds": [kb_id]},
    ).json()["data"]
    assert survived["answerable"]

    # 32: existing platform functionality still works.
    bots = client.get("/api/v1/bots", headers=headers)
    assert bots.status_code == 200 and bots.json()["success"]

    # cleanup Mongo doc
    await Mongo.transcripts().delete_one({"session_id": recorder.session_id})
    await Mongo.voice_events().delete_many({"session_id": recorder.session_id})
