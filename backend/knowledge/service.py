"""KnowledgeService — the ONE knowledge implementation shared by REST APIs,
MCP tools, the voice runtime and internal services.

Responsibilities:
- KB authorization against the MySQL control plane (`knowledge_sources`),
  covering the three retrieval modes (single kb_id / list / all-authorized).
- Document upload validation (extension, size, MIME sniff, duplicate hash).
- Ingestion job lifecycle (queue, status, retry, cancel, re-index).
- Tenant-safe hybrid retrieval via HybridRetriever + PgVectorStore.

Tenant identity is ALWAYS resolved by the caller from a trusted source (JWT,
phone-number mapping, authenticated MCP session) — never from request bodies.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from backend.config import get_settings
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.db.mysql import get_sessionmaker
from backend.knowledge.embeddings import get_embedding_provider
from backend.knowledge.ingestion import storage
from backend.knowledge.models import IngestionJob, KnowledgeChunk, KnowledgeDocument
from backend.knowledge.retrieval import HybridRetriever
from backend.knowledge.schemas import IngestionStatus, RetrievalRequest, RetrievalResult
from backend.knowledge.vector_store import PgVectorStore
from backend.models import KnowledgeSource

logger = logging.getLogger(__name__)

_READY_STATUSES = {"indexed"}
_SEARCHABLE_STATUSES = {"indexed", "stale"}  # stale = re-sync pending but still usable

# Magic-byte signatures for the formats we accept (best-effort sniffing).
_SIGNATURES: list[tuple[bytes, set[str]]] = [
    (b"%PDF", {"pdf"}),
    (b"PK\x03\x04", {"docx", "xlsx", "pptx"}),
    (b"\xd0\xcf\x11\xe0", {"doc", "xls", "ppt"}),
]


@dataclass
class UploadResult:
    document_id: str
    job_id: str
    kb_id: str
    duplicate: bool
    status: str


def sniff_mime(data: bytes, ext: str) -> str:
    """Validate magic bytes against the claimed extension; return a MIME type."""
    for signature, extensions in _SIGNATURES:
        if data[: len(signature)] == signature:
            if ext not in extensions:
                raise ApiError("File content does not match its extension", status_code=400)
            break
    else:
        if ext in {"pdf", "docx", "xlsx", "pptx", "doc", "xls", "ppt"}:
            raise ApiError("File content does not match its extension", status_code=400)
    import mimetypes

    return mimetypes.guess_type(f"f.{ext}")[0] or "application/octet-stream"


class KnowledgeService:
    def __init__(self, store: PgVectorStore | None = None, embedder=None) -> None:
        self.store = store or PgVectorStore()
        self.embedder = embedder or get_embedding_provider()
        self.retriever = HybridRetriever(self.store, self.embedder)

    # ── KB authorization (control plane) ──────────────────────────────────

    @staticmethod
    def _query_authorized_kbs(
        tenant_id: str | None,
        kb_ids: list[str] | None,
        bot_id: str | None,
        include_global: bool,
    ) -> list[KnowledgeSource]:
        """Sync MySQL lookup — runs in a worker thread from async callers."""
        session = get_sessionmaker()()
        try:
            stmt = select(KnowledgeSource).where(KnowledgeSource.is_deleted.is_(False))
            if tenant_id is None:
                stmt = stmt.where(KnowledgeSource.scope == "global")
            elif include_global:
                stmt = stmt.where(
                    (KnowledgeSource.tenant_id == tenant_id)
                    | (KnowledgeSource.scope == "global")
                )
            else:
                stmt = stmt.where(KnowledgeSource.tenant_id == tenant_id)
            if kb_ids:
                stmt = stmt.where(KnowledgeSource.id.in_(kb_ids))
            if bot_id:
                stmt = stmt.where(
                    (KnowledgeSource.bot_id == bot_id)
                    | (KnowledgeSource.scope.in_(("tenant", "global")))
                )
            return list(session.execute(stmt).scalars())
        finally:
            session.close()

    async def authorize_kb_ids(
        self,
        *,
        tenant_id: str | None,
        kb_ids: list[str] | None,
        bot_id: str | None = None,
        include_global: bool = True,
        searchable_only: bool = True,
    ) -> list[str]:
        """Resolve and validate the KB set for the three retrieval modes.

        Raises NotFoundError (sanitized 404) if any explicitly requested KB is
        missing, deleted, archived or not owned — without revealing which.
        """
        rows = await asyncio.to_thread(
            self._query_authorized_kbs, tenant_id, kb_ids, bot_id, include_global
        )
        if kb_ids:
            found = {row.id for row in rows}
            missing = [kb for kb in kb_ids if kb not in found]
            if missing:
                raise NotFoundError("Knowledge base not found")
            usable = [
                row.id for row in rows if not searchable_only or row.status in _SEARCHABLE_STATUSES
            ]
            return usable
        return [
            row.id for row in rows if not searchable_only or row.status in _SEARCHABLE_STATUSES
        ]

    # ── retrieval ─────────────────────────────────────────────────────────

    async def search(self, request: RetrievalRequest) -> RetrievalResult:
        started = time.perf_counter()
        authorized = await self.authorize_kb_ids(
            tenant_id=request.tenant_id,
            kb_ids=request.kb_ids,
            bot_id=request.bot_id,
            include_global=request.include_global,
        )
        result = await self.retriever.retrieve(request, authorized)
        logger.info(
            "knowledge.search tenant=%s bot=%s kbs=%d answerable=%s confidence=%.3f "
            "sources=%d duration_ms=%.1f",
            request.tenant_id,
            request.bot_id,
            len(authorized),
            result.answerable,
            result.confidence,
            len(result.sources),
            (time.perf_counter() - started) * 1000,
        )
        return result

    # ── upload & ingestion lifecycle ──────────────────────────────────────

    async def upload_document(
        self,
        *,
        tenant_id: str | None,
        kb_id: str,
        file_name: str,
        data: bytes,
        uploaded_by: str | None = None,
    ) -> UploadResult:
        settings = get_settings()
        # KB must exist, be owned and not deleted (any status — uploads are
        # allowed while a KB is still indexing other files).
        await self.authorize_kb_ids(
            tenant_id=tenant_id, kb_ids=[kb_id], searchable_only=False
        )

        try:
            ext = storage.file_extension(file_name)
        except storage.StorageError as exc:
            raise ApiError(str(exc), status_code=400) from exc
        if len(data) == 0:
            raise ApiError("Uploaded file is empty", status_code=400)
        if len(data) > settings.knowledge_max_file_mb * 1024 * 1024:
            raise ApiError(
                f"File exceeds the {settings.knowledge_max_file_mb} MB limit", status_code=400
            )
        mime = sniff_mime(data, ext)
        content_hash = storage.content_sha256(data)

        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            existing = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.kb_id == kb_id,
                        KnowledgeDocument.content_hash == content_hash,
                        KnowledgeDocument.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return UploadResult(
                    document_id=existing.id,
                    job_id="",
                    kb_id=kb_id,
                    duplicate=True,
                    status=existing.status,
                )

            document_id = new_id("kdoc")
            storage_path = storage.save_original(tenant_id, kb_id, document_id, file_name, data)
            document = KnowledgeDocument(
                id=document_id,
                tenant_id=tenant_id,
                kb_id=kb_id,
                file_name=file_name[:255],
                file_ext=ext,
                mime_type=mime,
                size_bytes=len(data),
                content_hash=content_hash,
                storage_path=storage_path,
                status="pending",
                created_by=uploaded_by,
            )
            session.add(document)
            await session.flush()  # job row references the document — order the inserts
            job = IngestionJob(
                id=new_id("kjob"),
                tenant_id=tenant_id,
                kb_id=kb_id,
                document_id=document_id,
                status="queued",
                max_attempts=settings.ingestion_max_attempts,
            )
            session.add(job)
            await session.commit()

        await asyncio.to_thread(self._mark_source_indexing, kb_id)
        logger.info(
            "knowledge.upload tenant=%s kb=%s doc=%s bytes=%d", tenant_id, kb_id, document_id, len(data)
        )
        return UploadResult(
            document_id=document_id, job_id=job.id, kb_id=kb_id, duplicate=False, status="pending"
        )

    @staticmethod
    def _mark_source_indexing(kb_id: str) -> None:
        session = get_sessionmaker()()
        try:
            source = session.get(KnowledgeSource, kb_id)
            if source is not None:
                source.status = "indexing"
                session.commit()
        finally:
            session.close()

    async def get_document(
        self, *, tenant_id: str | None, document_id: str
    ) -> KnowledgeDocument:
        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            doc = (
                await session.execute(
                    select(KnowledgeDocument).where(
                        KnowledgeDocument.id == document_id,
                        KnowledgeDocument.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
        if doc is None or (tenant_id is not None and doc.tenant_id not in (tenant_id, None)):
            raise NotFoundError("Document not found")
        return doc

    async def list_documents(
        self, *, tenant_id: str | None, kb_id: str
    ) -> list[IngestionStatus]:
        await self.authorize_kb_ids(tenant_id=tenant_id, kb_ids=[kb_id], searchable_only=False)
        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            docs = (
                await session.execute(
                    select(KnowledgeDocument)
                    .where(
                        KnowledgeDocument.kb_id == kb_id,
                        KnowledgeDocument.is_deleted.is_(False),
                    )
                    .order_by(KnowledgeDocument.created_at.desc())
                )
            ).scalars().all()
            statuses = []
            for doc in docs:
                job = (
                    await session.execute(
                        select(IngestionJob)
                        .where(IngestionJob.document_id == doc.id)
                        .order_by(IngestionJob.queued_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                statuses.append(self._status_of(doc, job))
        return statuses

    @staticmethod
    def _status_of(doc: KnowledgeDocument, job: IngestionJob | None) -> IngestionStatus:
        return IngestionStatus(
            document_id=doc.id,
            kb_id=doc.kb_id,
            file_name=doc.file_name,
            status=doc.status,
            stage=job.stage if job else None,
            progress=job.progress if job else (100.0 if doc.status == "ready" else 0.0),
            attempts=job.attempts if job else 0,
            failure_reason=doc.failure_reason,
            chunk_count=doc.chunk_count,
            page_count=doc.page_count,
            queued_at=job.queued_at if job else None,
            started_at=job.started_at if job else None,
            finished_at=job.finished_at if job else None,
        )

    async def get_ingestion_status(
        self, *, tenant_id: str | None, document_id: str
    ) -> IngestionStatus:
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            job = (
                await session.execute(
                    select(IngestionJob)
                    .where(IngestionJob.document_id == document_id)
                    .order_by(IngestionJob.queued_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return self._status_of(doc, job)

    async def retry_ingestion(self, *, tenant_id: str | None, document_id: str) -> IngestionStatus:
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        if doc.status not in {"failed", "cancelled"}:
            raise ApiError("Only failed or cancelled documents can be retried", status_code=409)
        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            doc_row = await session.get(KnowledgeDocument, document_id)
            doc_row.status = "pending"
            doc_row.failure_reason = None
            session.add(
                IngestionJob(
                    id=new_id("kjob"),
                    tenant_id=doc.tenant_id,
                    kb_id=doc.kb_id,
                    document_id=document_id,
                    status="queued",
                    max_attempts=get_settings().ingestion_max_attempts,
                )
            )
            await session.commit()
        await asyncio.to_thread(self._mark_source_indexing, doc.kb_id)
        return await self.get_ingestion_status(tenant_id=tenant_id, document_id=document_id)

    async def cancel_ingestion(self, *, tenant_id: str | None, document_id: str) -> IngestionStatus:
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            job = (
                await session.execute(
                    select(IngestionJob)
                    .where(
                        IngestionJob.document_id == document_id,
                        IngestionJob.status.in_(("queued", "running")),
                    )
                    .order_by(IngestionJob.queued_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if job is None:
                raise ApiError("No active ingestion job to cancel", status_code=409)
            job.status = "cancelled"
            doc_row = await session.get(KnowledgeDocument, document_id)
            if doc_row.status in ("pending", "processing"):
                doc_row.status = "cancelled"
            await session.commit()
        return await self.get_ingestion_status(tenant_id=tenant_id, document_id=document_id)

    async def reindex_document(self, *, tenant_id: str | None, document_id: str) -> IngestionStatus:
        """Safe re-index: old chunks stay searchable until the new run replaces them."""
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        from backend.db.postgres import get_pg_sessionmaker

        async with get_pg_sessionmaker()() as session:
            session.add(
                IngestionJob(
                    id=new_id("kjob"),
                    tenant_id=doc.tenant_id,
                    kb_id=doc.kb_id,
                    document_id=document_id,
                    status="queued",
                    max_attempts=get_settings().ingestion_max_attempts,
                    payload={"reindex": True},
                )
            )
            doc_row = await session.get(KnowledgeDocument, document_id)
            doc_row.status = "processing"
            await session.commit()
        return await self.get_ingestion_status(tenant_id=tenant_id, document_id=document_id)

    async def delete_document(
        self, *, tenant_id: str | None, document_id: str, deleted_by: str | None = None
    ) -> None:
        doc = await self.get_document(tenant_id=tenant_id, document_id=document_id)
        from sqlalchemy import func as sa_func

        from backend.db.postgres import get_pg_sessionmaker

        await self.store.delete_document(doc.tenant_id, document_id)
        async with get_pg_sessionmaker()() as session:
            doc_row = await session.get(KnowledgeDocument, document_id)
            doc_row.is_deleted = True
            doc_row.status = "archived"
            doc_row.deleted_at = sa_func.now()
            doc_row.deleted_by = deleted_by
            await session.commit()

    async def delete_knowledge_base_data(
        self, *, tenant_id: str | None, kb_id: str
    ) -> int:
        """Archive every chunk+document for a KB (called when a source is archived)."""
        from sqlalchemy import func as sa_func, update as sa_update

        from backend.db.postgres import get_pg_sessionmaker

        removed = await self.store.delete_knowledge_base(tenant_id, kb_id)
        async with get_pg_sessionmaker()() as session:
            await session.execute(
                sa_update(KnowledgeDocument)
                .where(KnowledgeDocument.kb_id == kb_id)
                .values(is_deleted=True, status="archived", deleted_at=sa_func.now())
            )
            await session.commit()
        return removed


_service: KnowledgeService | None = None


def get_knowledge_service() -> KnowledgeService:
    """Process-wide singleton (embedder + store are safe to share)."""
    global _service
    if _service is None:
        _service = KnowledgeService()
    return _service
