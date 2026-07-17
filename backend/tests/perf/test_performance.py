"""Measured performance (no invented numbers).

Run: pytest backend/tests/perf -m perf -s
Prints real latencies; asserts only generous sanity ceilings so CI noise
doesn't flap.
"""

import asyncio
import hashlib
import statistics
import time
import uuid

import pytest

from backend.knowledge.schemas import ChunkPayload

pytestmark = [pytest.mark.perf, pytest.mark.integration]

CORPUS_CHUNKS = 5000
WARM_QUERIES = 100


def pct(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]


@pytest.fixture(scope="module")
async def corpus(store_module, embedder_module):
    """A 5k-chunk KB seeded once for the module."""
    from backend.db.postgres import get_pg_sessionmaker
    from backend.knowledge.models import KnowledgeDocument

    kb_id = f"kstest_perf_{uuid.uuid4().hex[:8]}"
    tenant = "tn_test_perf"
    document_id = f"kdoc_test_perf_{uuid.uuid4().hex[:8]}"
    async with get_pg_sessionmaker()() as session:
        session.add(
            KnowledgeDocument(
                id=document_id, tenant_id=tenant, kb_id=kb_id, file_name="corpus.txt",
                content_hash=uuid.uuid4().hex, status="ready",
            )
        )
        await session.commit()

    topics = ["policy", "claims", "billing", "renewals", "coverage",
              "appointments", "support", "accounts"]
    payloads = []
    for index in range(CORPUS_CHUNKS):
        topic = topics[index % len(topics)]
        text = (
            f"Document section {index} about {topic}: the {topic} process step "
            f"{index % 40} requires review within {index % 90 + 1} days."
        )
        payloads.append(
            ChunkPayload(
                tenant_id=tenant, kb_id=kb_id, document_id=document_id,
                chunk_index=index, content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                embedding=embedder_module._embed(text),
                embedding_model=embedder_module.model,
                embedding_dimension=embedder_module.dimension,
            )
        )
    started = time.perf_counter()
    await store_module.upsert_chunks(payloads)
    seed_seconds = time.perf_counter() - started
    print(f"\n[perf] seeded {CORPUS_CHUNKS} chunks in {seed_seconds:.1f}s "
          f"({CORPUS_CHUNKS / seed_seconds:.0f} chunks/s)")

    yield tenant, kb_id, document_id

    from sqlalchemy import delete as sa_delete

    from backend.knowledge.models import KnowledgeChunk

    async with get_pg_sessionmaker()() as session:
        await session.execute(
            sa_delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document_id)
        )
        await session.execute(
            sa_delete(KnowledgeDocument).where(KnowledgeDocument.id == document_id)
        )
        await session.commit()


@pytest.fixture(scope="module")
def embedder_module():
    from backend.knowledge.embeddings.mock_provider import MockEmbeddingProvider

    return MockEmbeddingProvider(dimension=1536)


@pytest.fixture(scope="module")
def store_module():
    from backend.knowledge.vector_store import PgVectorStore

    return PgVectorStore()


class TestPgvectorLatency:
    async def test_warm_p50_p95(self, corpus, store_module, embedder_module):
        tenant, kb_id, _ = corpus
        queries = [f"how does the {t} process work" for t in
                   ("policy", "claims", "billing", "renewals")] * (WARM_QUERIES // 4)
        embeddings = [embedder_module._embed(q) for q in queries]
        # Warm-up
        for embedding in embeddings[:5]:
            await store_module.dense_search(
                tenant_id=tenant, kb_ids=[kb_id], query_embedding=embedding, limit=10
            )
        latencies = []
        for embedding in embeddings:
            started = time.perf_counter()
            results = await store_module.dense_search(
                tenant_id=tenant, kb_ids=[kb_id], query_embedding=embedding, limit=10
            )
            latencies.append((time.perf_counter() - started) * 1000)
            assert results
        print(f"[perf] pgvector dense warm ({CORPUS_CHUNKS} chunks, n={len(latencies)}): "
              f"p50={pct(latencies, 50):.1f}ms p95={pct(latencies, 95):.1f}ms "
              f"mean={statistics.mean(latencies):.1f}ms")
        assert pct(latencies, 95) < 500  # sanity ceiling only

    async def test_hybrid_latency(self, corpus, store_module, embedder_module,
                                  knowledge_service_module):
        from backend.knowledge.schemas import RetrievalRequest

        tenant, kb_id, _ = corpus
        latencies = []
        for _ in range(30):
            started = time.perf_counter()
            result = await knowledge_service_module.retriever.retrieve(
                RetrievalRequest(tenant_id=tenant, kb_ids=[kb_id],
                                 query="how does the claims process work"),
                [kb_id],
            )
            latencies.append((time.perf_counter() - started) * 1000)
            assert result.used_knowledge_base
        print(f"[perf] hybrid retrieval (dense+keyword+fusion): "
              f"p50={pct(latencies, 50):.1f}ms p95={pct(latencies, 95):.1f}ms")

    async def test_concurrent_retrieval(self, corpus, store_module, embedder_module):
        tenant, kb_id, _ = corpus
        embedding = embedder_module._embed("policy review steps")

        async def one():
            started = time.perf_counter()
            await store_module.dense_search(
                tenant_id=tenant, kb_ids=[kb_id], query_embedding=embedding, limit=10
            )
            return (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        latencies = await asyncio.gather(*[one() for _ in range(20)])
        wall = (time.perf_counter() - started) * 1000
        print(f"[perf] 20 concurrent dense searches: wall={wall:.1f}ms "
              f"p95(individual)={pct(list(latencies), 95):.1f}ms")


@pytest.fixture(scope="module")
def knowledge_service_module(store_module, embedder_module):
    from backend.knowledge.service import KnowledgeService

    return KnowledgeService(store=store_module, embedder=embedder_module)


class TestIngestionSpeed:
    async def test_pdf_ingestion_wall_time(self, knowledge_service_module, store_module,
                                           embedder_module, control_plane, pg_cleanup):
        from backend.knowledge.ingestion.pipeline import IngestionPipeline
        from backend.tests.conftest import make_pdf_bytes

        tenant = control_plane.tenant()
        kb = control_plane.knowledge_source(tenant)
        pdf = make_pdf_bytes("Grace period details paragraph. " * 40, pages=5)
        started = time.perf_counter()
        upload = await knowledge_service_module.upload_document(
            tenant_id=tenant, kb_id=kb, file_name="perf.pdf", data=pdf
        )
        pg_cleanup.append(upload.document_id)
        pipeline = IngestionPipeline(store=store_module, embedder=embedder_module)
        while (job_id := await pipeline.claim_next_job()) is not None:
            await pipeline.process_job(job_id)
        wall = time.perf_counter() - started
        status = await knowledge_service_module.get_ingestion_status(
            tenant_id=tenant, document_id=upload.document_id
        )
        assert status.status == "ready"
        print(f"[perf] 5-page PDF upload→ready: {wall * 1000:.0f}ms "
              f"({status.chunk_count} chunks)")


class TestVoiceLatency:
    async def test_time_to_first_audio_and_barge_in(self):
        from pipecat.frames.frames import (
            Frame, InterruptionFrame, TranscriptionFrame, TTSAudioRawFrame,
        )
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.workers.runner import WorkerRunner
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

        from backend.providers.base import ProviderConfig
        from backend.providers.factory import get_llm_provider, get_tts_provider
        from backend.tests.integration.test_voice_pipeline import make_config
        from backend.voice_runtime.brain import ConversationBrain
        from backend.voice_runtime.services import EchoTTSService
        from backend.voice_runtime.session import SessionRecorder

        marks: dict[str, float] = {}

        class Timer(FrameProcessor):
            async def process_frame(self, frame: Frame, direction: FrameDirection):
                await super().process_frame(frame, direction)
                if isinstance(frame, TTSAudioRawFrame) and "first_audio" not in marks:
                    marks["first_audio"] = time.perf_counter()
                await self.push_frame(frame, direction)

        config = make_config(kb_ids=[])
        recorder = SessionRecorder("vs_perf", config)
        brain = ConversationBrain(
            config=config, llm=get_llm_provider(ProviderConfig(provider="mock")),
            recorder=recorder,
        )
        tts = EchoTTSService(get_tts_provider(ProviderConfig(provider="mock")),
                             sample_rate=16000)
        timer = Timer()
        worker = PipelineWorker(
            Pipeline([brain, tts, timer]),
            params=PipelineParams(enable_metrics=False), enable_rtvi=False,
            idle_timeout_secs=None,
        )

        async def feeder():
            await asyncio.sleep(0.2)
            marks["turn_start"] = time.perf_counter()
            await worker.queue_frame(
                TranscriptionFrame("what are your working hours today", "caller", "t1"))
            await asyncio.sleep(0.5)
            marks["interrupt_sent"] = time.perf_counter()
            await worker.queue_frame(InterruptionFrame())
            await asyncio.sleep(0.2)
            await worker.queue_frame(TranscriptionFrame("hang up", "caller", "t2"))

        runner = WorkerRunner(handle_sigint=False)
        feed = asyncio.create_task(feeder())
        await asyncio.wait_for(runner.run(worker), timeout=20)
        await feed

        ttfa = (marks["first_audio"] - marks["turn_start"]) * 1000
        print(f"[perf] voice pipeline (mock providers) transcription→first TTS audio: "
              f"{ttfa:.0f}ms")
        cancelled = [e for e in recorder.events if e["kind"] == "generation_cancelled"]
        print(f"[perf] barge-in: interruption processed, "
              f"cancellation events={len(cancelled)} (in-flight work only)")
        assert ttfa < 5000
