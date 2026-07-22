"""Ingestion pipeline: parse → chunk → embed → store → verify.

Runs inside the background ingestion worker. Jobs are claimed atomically
(FOR UPDATE SKIP LOCKED) so multiple workers can run safely; every stage
checks for cancellation and updates job progress.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from shared.config import get_settings
from shared.db.mysql import get_sessionmaker
from shared.db.postgres import get_pg_sessionmaker
from shared.knowledge.embeddings import get_embedding_provider
from shared.knowledge.ingestion import storage
from shared.knowledge.models import IngestionJob, KnowledgeDocument
from shared.knowledge.schemas import ChunkPayload
from shared.knowledge.security import detect_prompt_injection
from shared.knowledge.vector_store import PgVectorStore
from shared.models import KnowledgeSource

logger = logging.getLogger(__name__)


class IngestionCancelled(Exception):
    pass


class IngestionPipeline:
    def __init__(self, store: PgVectorStore | None = None, embedder=None) -> None:
        self.store = store or PgVectorStore()
        self.embedder = embedder or get_embedding_provider()

    # ── job claiming ──────────────────────────────────────────────────────

    async def claim_next_job(self) -> str | None:
        """Atomically claim the oldest queued job; returns its id or None."""
        async with get_pg_sessionmaker()() as session:
            job = (
                await session.execute(
                    select(IngestionJob)
                    .where(IngestionJob.status == "queued")
                    .order_by(IngestionJob.queued_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.started_at = datetime.now(timezone.utc)
            job.stage = "parsing"
            job.progress = 1.0
            await session.commit()
            return job.id

    async def _update_job(self, job_id: str, **fields) -> None:
        async with get_pg_sessionmaker()() as session:
            job = await session.get(IngestionJob, job_id)
            if job is None:
                return
            for key, value in fields.items():
                setattr(job, key, value)
            await session.commit()

    async def _check_cancelled(self, job_id: str) -> None:
        async with get_pg_sessionmaker()() as session:
            job = await session.get(IngestionJob, job_id)
            if job is not None and job.status == "cancelled":
                raise IngestionCancelled()

    # ── the pipeline ──────────────────────────────────────────────────────

    async def process_job(self, job_id: str) -> None:
        async with get_pg_sessionmaker()() as session:
            job = await session.get(IngestionJob, job_id)
            if job is None:
                return
            doc = await session.get(KnowledgeDocument, job.document_id)
            if doc is None:
                job.status = "failed"
                job.error = "document row missing"
                await session.commit()
                return
            document_id, kb_id, tenant_id = doc.id, doc.kb_id, doc.tenant_id
            file_name, storage_path = doc.file_name, doc.storage_path
            reindex = bool((job.payload or {}).get("reindex"))
            max_attempts, attempts = job.max_attempts, job.attempts
            doc.status = "processing"
            await session.commit()

        try:
            await self._run_stages(
                job_id=job_id,
                document_id=document_id,
                kb_id=kb_id,
                tenant_id=tenant_id,
                file_name=file_name,
                storage_path=storage_path,
                reindex=reindex,
            )
        except IngestionCancelled:
            logger.info("ingestion.cancelled job=%s doc=%s", job_id, document_id)
            await self._finalize_document(document_id, status="cancelled")
        except Exception as exc:  # noqa: BLE001 - categorized + persisted below
            reason = f"{exc.__class__.__name__}: {exc}"[:2000]
            logger.exception("ingestion.failed job=%s doc=%s", job_id, document_id)
            if attempts < max_attempts:
                await self._update_job(
                    job_id, status="queued", stage=None, progress=0.0, error=reason
                )
            else:
                await self._update_job(
                    job_id,
                    status="failed",
                    error=reason,
                    finished_at=datetime.now(timezone.utc),
                )
                await self._finalize_document(document_id, status="failed", failure_reason=reason)
                await asyncio.to_thread(self._sync_source, kb_id, tenant_id, failed=True)

    async def _run_stages(
        self,
        *,
        job_id: str,
        document_id: str,
        kb_id: str,
        tenant_id: str | None,
        file_name: str,
        storage_path: str,
        reindex: bool,
    ) -> None:
        settings = get_settings()

        # 1. Parse (+ document-aware chunking; blocking → worker thread).
        from shared.knowledge.parsing.loader import load_document

        path = storage.resolve_path(storage_path)
        raw_chunks = await asyncio.to_thread(load_document, str(path), file_name)
        if not raw_chunks:
            raise ValueError("Parser produced no content (empty or unreadable document)")
        await self._check_cancelled(job_id)
        await self._update_job(job_id, stage="chunking", progress=30.0)

        # 2. Build chunk payloads with provenance + injection flags.
        from shared.knowledge.chunking.chunker import count_tokens

        payloads: list[ChunkPayload] = []
        pages: set[int] = set()
        for index, chunk in enumerate(raw_chunks):
            content = (chunk.get("page_content") or "").strip()
            if not content:
                continue
            meta = dict(chunk.get("metadata") or {})
            page_number = meta.get("page_number")
            if isinstance(page_number, int):
                pages.add(page_number)
            injections = detect_prompt_injection(content)
            if injections:
                meta["prompt_injection_flags"] = injections
            meta["file_name"] = file_name
            payloads.append(
                ChunkPayload(
                    tenant_id=tenant_id,
                    kb_id=kb_id,
                    document_id=document_id,
                    chunk_index=index,
                    content=content,
                    embedding_text=meta.get("embedding_text"),
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    token_count=count_tokens(content),
                    page_number=page_number if isinstance(page_number, int) else None,
                    section=meta.get("section"),
                    topic=meta.get("topic"),
                    chunk_type=meta.get("chunk_type"),
                    keywords=meta.get("keywords"),
                    language=meta.get("language"),
                    meta=meta,
                )
            )
        if not payloads:
            raise ValueError("No non-empty chunks produced")
        await self._check_cancelled(job_id)
        await self._update_job(job_id, stage="embedding", progress=45.0)

        # 3. Embed in batches.
        texts = [p.embedding_text or p.content for p in payloads]
        vectors = await self.embedder.embed_documents(texts)
        for payload, vector in zip(payloads, vectors, strict=True):
            payload.embedding = vector
            payload.embedding_model = self.embedder.model
            payload.embedding_dimension = self.embedder.dimension
        await self._check_cancelled(job_id)
        await self._update_job(job_id, stage="storing", progress=75.0)

        # 4. Store. Re-index replaces the old chunk set atomically enough for
        # retrieval (upsert by (document_id, chunk_index), then trim leftovers).
        if reindex:
            await self.store.hard_delete_document(tenant_id, document_id)
        await self.store.upsert_chunks(payloads)

        # 5. Verify sample retrieval before marking ready.
        await self._update_job(job_id, stage="verifying", progress=90.0)
        probe = await self.store.dense_search(
            tenant_id=tenant_id,
            kb_ids=[kb_id],
            query_embedding=vectors[0],
            limit=1,
        )
        if not probe:
            raise RuntimeError("Verification search returned no results after indexing")

        await self._finalize_document(
            document_id,
            status="ready",
            chunk_count=len(payloads),
            page_count=len(pages),
            embedding_model=self.embedder.model,
            embedding_dimension=self.embedder.dimension,
        )
        await self._update_job(
            job_id,
            status="completed",
            stage="done",
            progress=100.0,
            finished_at=datetime.now(timezone.utc),
            error=None,
        )
        await asyncio.to_thread(self._sync_source, kb_id, tenant_id, failed=False)
        await self.refresh_source_chunk_count(kb_id, tenant_id)
        logger.info(
            "ingestion.completed doc=%s kb=%s chunks=%d pages=%d",
            document_id, kb_id, len(payloads), len(pages),
        )

    async def _finalize_document(self, document_id: str, *, status: str, **fields) -> None:
        async with get_pg_sessionmaker()() as session:
            doc = await session.get(KnowledgeDocument, document_id)
            if doc is None:
                return
            doc.status = status
            for key, value in fields.items():
                setattr(doc, key, value)
            await session.commit()

    @staticmethod
    def _sync_source(kb_id: str, tenant_id: str | None, *, failed: bool) -> None:
        """Reflect ingestion outcome onto the MySQL control-plane source row."""
        from sqlalchemy import func as sa_func

        session = get_sessionmaker()()
        try:
            source = session.get(KnowledgeSource, kb_id)
            if source is None:
                return
            if failed:
                source.status = "failed"
            else:
                source.status = "indexed"
                source.last_sync_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()

    async def refresh_source_chunk_count(self, kb_id: str, tenant_id: str | None) -> None:
        count = await self.store.count_chunks(tenant_id, kb_id)

        def _write() -> None:
            session = get_sessionmaker()()
            try:
                source = session.get(KnowledgeSource, kb_id)
                if source is not None:
                    source.chunks = count
                    session.commit()
            finally:
                session.close()

        await asyncio.to_thread(_write)
