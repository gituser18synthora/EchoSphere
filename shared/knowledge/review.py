"""Knowledge chunk-review service — the read/curation layer behind the Super
Admin "Knowledge Management → Chunk Review" console.

Responsibilities:
- Server-side filtered / sorted / paginated listing of uploaded documents and
  the chunks generated from them (PostgreSQL knowledge plane).
- Document + chunk detail with quality signals (token/char counts, overlap,
  short-chunk / missing-metadata / OCR / table / prompt-injection / PII flags),
  and prev/current/next chunk context for boundary verification.
- Safe curation actions (chunk active/inactive, flag-for-review) and retrieval
  testing with full dense/keyword/fused scoring.

Tenant safety: every query is scoped to an *effective* tenant resolved by the
caller from a trusted source. `tenant_id=None` is the platform (Super Admin)
scope and sees every tenant; a concrete tenant_id restricts to that tenant's
rows only (cross-tenant rows are excluded in SQL, never in Python).

Embeddings are NEVER selected or returned — only their presence + model +
dimension. Control-plane names (tenant, knowledge base, uploader) live in MySQL
and are resolved in a single batched worker-thread lookup after the PG query.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Text, and_, cast, func, or_, select

from shared.config import get_settings
from shared.db.mysql import get_sessionmaker
from shared.db.postgres import get_pg_sessionmaker
from shared.errors import ApiError, NotFoundError
from shared.knowledge.models import IngestionJob, KnowledgeChunk, KnowledgeDocument
from shared.knowledge.schemas import RetrievalRequest
from shared.knowledge.security import detect_pii
from shared.models import KnowledgeSource, Tenant, User

logger = logging.getLogger(__name__)

SHORT_CHUNK_TOKENS = 20
SHORT_CHUNK_CHARS = 120
_OVERLAP_CAP = 800

_DOC_SORTS = {
    "createdAt": KnowledgeDocument.created_at,
    "fileName": KnowledgeDocument.file_name,
    "sizeBytes": KnowledgeDocument.size_bytes,
    "chunkCount": KnowledgeDocument.chunk_count,
    "pageCount": KnowledgeDocument.page_count,
    "status": KnowledgeDocument.status,
}
_CHUNK_SORTS = {
    "chunkIndex": KnowledgeChunk.chunk_index,
    "createdAt": KnowledgeChunk.created_at,
    "updatedAt": KnowledgeChunk.updated_at,
    "tokenCount": KnowledgeChunk.token_count,
    "pageNumber": KnowledgeChunk.page_number,
}

_UPLOAD_STATUSES = ["pending", "processing", "ready", "failed", "cancelled", "archived"]
_INGESTION_STATUSES = ["queued", "running", "completed", "failed", "cancelled"]
_CHUNK_STATUSES = ["active", "archived"]

_CONTENT_PREVIEW = 280


@dataclass
class DocumentFilters:
    kb_ids: list[str] | None = None
    document_id: str | None = None
    file_ext: str | None = None
    status: str | None = None  # upload/lifecycle status (doc.status)
    ingestion_status: str | None = None  # latest job status
    language: str | None = None
    uploaded_from: datetime | None = None
    uploaded_to: datetime | None = None
    failed_only: bool = False
    include_archived: bool = False
    search: str | None = None


@dataclass
class ChunkFilters:
    kb_ids: list[str] | None = None
    document_id: str | None = None
    status: str | None = None
    language: str | None = None
    page_number: int | None = None
    section: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    min_tokens: int | None = None
    max_tokens: int | None = None
    has_keywords: bool | None = None
    has_metadata: bool | None = None
    flagged_only: bool = False
    search: str | None = None
    include_archived: bool = True


# Chunk columns we ever read — deliberately EXCLUDES `embedding` (the 1536-dim
# vector) so it is never pulled from the DB or sent to the client.
_CHUNK_COLS = (
    KnowledgeChunk.id,
    KnowledgeChunk.tenant_id,
    KnowledgeChunk.kb_id,
    KnowledgeChunk.document_id,
    KnowledgeChunk.chunk_index,
    KnowledgeChunk.page_number,
    KnowledgeChunk.section,
    KnowledgeChunk.topic,
    KnowledgeChunk.chunk_type,
    KnowledgeChunk.keywords,
    KnowledgeChunk.language,
    KnowledgeChunk.content,
    KnowledgeChunk.content_hash,
    KnowledgeChunk.token_count,
    KnowledgeChunk.embedding_model,
    KnowledgeChunk.embedding_dimension,
    KnowledgeChunk.status,
    KnowledgeChunk.meta,
    KnowledgeChunk.created_at,
    KnowledgeChunk.updated_at,
    KnowledgeChunk.embedding.is_not(None).label("has_embedding"),
)


class KnowledgeReviewService:
    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_pg_sessionmaker()

    # ── tenant filter helper ────────────────────────────────────────────────

    @staticmethod
    def _tenant_clause(model, tenant_id: str | None):
        """Platform scope (None) sees everything; a tenant sees only its rows."""
        if tenant_id is None:
            return None
        return model.tenant_id == tenant_id

    # ── control-plane name resolution (MySQL, batched, off the event loop) ──

    @staticmethod
    def _lookup_names(
        tenant_ids: set[str], kb_ids: set[str], user_ids: set[str]
    ) -> tuple[dict, dict, dict]:
        session = get_sessionmaker()()
        try:
            tenants: dict[str, dict] = {}
            if tenant_ids:
                for t in session.execute(
                    select(Tenant).where(Tenant.id.in_(tenant_ids))
                ).scalars():
                    tenants[t.id] = {"name": t.name, "code": t.code}
            kbs: dict[str, dict] = {}
            if kb_ids:
                for k in session.execute(
                    select(KnowledgeSource).where(KnowledgeSource.id.in_(kb_ids))
                ).scalars():
                    kbs[k.id] = {
                        "name": k.name,
                        "tenantId": k.tenant_id,
                        "scope": k.scope,
                        "status": k.status,
                    }
            users: dict[str, str] = {}
            if user_ids:
                for u in session.execute(
                    select(User).where(User.id.in_(user_ids))
                ).scalars():
                    users[u.id] = u.name
            return tenants, kbs, users
        finally:
            session.close()

    async def _resolve_names(
        self, tenant_ids: set[str], kb_ids: set[str], user_ids: set[str]
    ) -> tuple[dict, dict, dict]:
        tenant_ids = {t for t in tenant_ids if t}
        kb_ids = {k for k in kb_ids if k}
        user_ids = {u for u in user_ids if u}
        if not (tenant_ids or kb_ids or user_ids):
            return {}, {}, {}
        return await asyncio.to_thread(self._lookup_names, tenant_ids, kb_ids, user_ids)

    # ── documents ───────────────────────────────────────────────────────────

    def _document_where(self, tenant_id: str | None, f: DocumentFilters):
        clauses: list = []
        tenant_clause = self._tenant_clause(KnowledgeDocument, tenant_id)
        if tenant_clause is not None:
            clauses.append(tenant_clause)
        if not f.include_archived:
            clauses.append(KnowledgeDocument.is_deleted.is_(False))
        if f.kb_ids:
            clauses.append(KnowledgeDocument.kb_id.in_(f.kb_ids))
        if f.document_id:
            clauses.append(KnowledgeDocument.id == f.document_id)
        if f.file_ext:
            clauses.append(KnowledgeDocument.file_ext == f.file_ext.lower().lstrip("."))
        if f.status:
            clauses.append(KnowledgeDocument.status == f.status)
        if f.language:
            clauses.append(KnowledgeDocument.language == f.language)
        if f.uploaded_from:
            clauses.append(KnowledgeDocument.created_at >= f.uploaded_from)
        if f.uploaded_to:
            clauses.append(KnowledgeDocument.created_at <= f.uploaded_to)
        latest_job_status = (
            select(IngestionJob.status)
            .where(IngestionJob.document_id == KnowledgeDocument.id)
            .order_by(IngestionJob.queued_at.desc())
            .limit(1)
            .correlate(KnowledgeDocument)
            .scalar_subquery()
        )
        if f.ingestion_status:
            clauses.append(latest_job_status == f.ingestion_status)
        if f.failed_only:
            clauses.append(
                or_(
                    KnowledgeDocument.status.in_(("failed", "cancelled")),
                    latest_job_status == "failed",
                )
            )
        if f.search:
            like = f"%{f.search.strip()}%"
            clauses.append(
                or_(
                    KnowledgeDocument.file_name.ilike(like),
                    KnowledgeDocument.id == f.search.strip(),
                )
            )
        return clauses

    async def list_documents(
        self,
        *,
        tenant_id: str | None,
        filters: DocumentFilters,
        page: int,
        page_size: int,
        sort_by: str | None,
        sort_dir: str,
    ) -> tuple[list[dict], int]:
        clauses = self._document_where(tenant_id, filters)
        sort_col = _DOC_SORTS.get(sort_by or "", KnowledgeDocument.created_at)
        order = sort_col.desc() if sort_dir == "desc" else sort_col.asc()

        async with self._session_factory() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count()).select_from(KnowledgeDocument).where(*clauses)
                    )
                ).scalar()
                or 0
            )
            docs = (
                (
                    await session.execute(
                        select(KnowledgeDocument)
                        .where(*clauses)
                        .order_by(order, KnowledgeDocument.id)
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                )
                .scalars()
                .all()
            )
            doc_ids = [d.id for d in docs]
            jobs = await self._latest_jobs(session, doc_ids)

        tenants, kbs, users = await self._resolve_names(
            {d.tenant_id for d in docs},
            {d.kb_id for d in docs},
            {d.created_by for d in docs},
        )
        rows = [
            self._serialize_document(d, jobs.get(d.id), tenants, kbs, users) for d in docs
        ]
        return rows, total

    @staticmethod
    async def _latest_jobs(session, doc_ids: list[str]) -> dict[str, IngestionJob]:
        if not doc_ids:
            return {}
        # DISTINCT ON (document_id) — one latest job per document (Postgres).
        jobs = (
            (
                await session.execute(
                    select(IngestionJob)
                    .where(IngestionJob.document_id.in_(doc_ids))
                    .order_by(IngestionJob.document_id, IngestionJob.queued_at.desc())
                    .distinct(IngestionJob.document_id)
                )
            )
            .scalars()
            .all()
        )
        return {j.document_id: j for j in jobs}

    def _serialize_document(
        self,
        d: KnowledgeDocument,
        job: IngestionJob | None,
        tenants: dict,
        kbs: dict,
        users: dict,
    ) -> dict:
        t = tenants.get(d.tenant_id or "", {})
        kb = kbs.get(d.kb_id, {})
        return {
            "documentId": d.id,
            "tenantId": d.tenant_id,
            "tenantName": t.get("name") or ("Platform (global)" if d.tenant_id is None else None),
            "tenantCode": t.get("code"),
            "kbId": d.kb_id,
            "kbName": kb.get("name"),
            "fileName": d.file_name,
            "fileExt": d.file_ext,
            "fileType": d.file_ext,
            "mimeType": d.mime_type,
            "sizeBytes": d.size_bytes,
            "docType": d.doc_type,
            "language": d.language,
            "status": d.status,
            "uploadStatus": "archived" if d.is_deleted else ("stored" if d.storage_path else "missing"),
            "ingestionStatus": job.status if job else d.status,
            "ingestionStage": job.stage if job else None,
            "ingestionProgress": (
                job.progress if job else (100.0 if d.status == "ready" else 0.0)
            ),
            "attempts": job.attempts if job else 0,
            "failureReason": d.failure_reason or (job.error if job else None),
            "pageCount": d.page_count,
            "chunkCount": d.chunk_count,
            "embeddingModel": d.embedding_model,
            "embeddingDimension": d.embedding_dimension,
            "isDeleted": d.is_deleted,
            "uploadedBy": d.created_by,
            "uploadedByName": users.get(d.created_by or ""),
            "uploadedAt": _iso(d.created_at),
            "processingCompletedAt": _iso(job.finished_at) if job else None,
            "updatedAt": _iso(d.updated_at),
        }

    async def get_document(self, *, tenant_id: str | None, document_id: str) -> dict:
        clauses = [KnowledgeDocument.id == document_id]
        tenant_clause = self._tenant_clause(KnowledgeDocument, tenant_id)
        if tenant_clause is not None:
            clauses.append(tenant_clause)
        async with self._session_factory() as session:
            doc = (
                await session.execute(select(KnowledgeDocument).where(*clauses))
            ).scalar_one_or_none()
            if doc is None:
                raise NotFoundError("Document not found")
            jobs = await self._latest_jobs(session, [doc.id])
            quality = await self._document_quality(session, doc.id)

        tenants, kbs, users = await self._resolve_names(
            {doc.tenant_id}, {doc.kb_id}, {doc.created_by}
        )
        row = self._serialize_document(doc, jobs.get(doc.id), tenants, kbs, users)
        row["quality"] = quality
        row["hasOriginalFile"] = bool(doc.storage_path) and not doc.is_deleted
        return row

    async def _document_quality(self, session, document_id: str) -> dict:
        meta = KnowledgeChunk.meta
        agg = (
            await session.execute(
                select(
                    func.count().label("total"),
                    func.count().filter(KnowledgeChunk.status == "active").label("active"),
                    func.count().filter(KnowledgeChunk.status == "archived").label("archived"),
                    func.min(KnowledgeChunk.token_count).label("min_tokens"),
                    func.max(KnowledgeChunk.token_count).label("max_tokens"),
                    func.avg(KnowledgeChunk.token_count).label("avg_tokens"),
                    func.count()
                    .filter(KnowledgeChunk.page_number.is_(None))
                    .label("missing_page"),
                    func.count()
                    .filter(or_(KnowledgeChunk.section.is_(None), KnowledgeChunk.section == ""))
                    .label("missing_section"),
                    func.count()
                    .filter(KnowledgeChunk.token_count < SHORT_CHUNK_TOKENS)
                    .label("short"),
                    func.count()
                    .filter(meta["ocr_used"].astext == "true")
                    .label("ocr"),
                    func.count()
                    .filter(
                        or_(
                            meta["table_detected"].astext == "true",
                            KnowledgeChunk.chunk_type == "table",
                        )
                    )
                    .label("table"),
                    func.count()
                    .filter(func.jsonb_typeof(meta["prompt_injection_flags"]) == "array")
                    .label("injection"),
                    func.count()
                    .filter(func.jsonb_typeof(meta["review"]) == "object")
                    .label("flagged"),
                )
                .where(
                    KnowledgeChunk.document_id == document_id,
                    KnowledgeChunk.is_deleted.is_(False),
                )
            )
        ).one()
        return {
            "totalChunks": int(agg.total or 0),
            "activeChunks": int(agg.active or 0),
            "archivedChunks": int(agg.archived or 0),
            "minTokens": int(agg.min_tokens) if agg.min_tokens is not None else None,
            "maxTokens": int(agg.max_tokens) if agg.max_tokens is not None else None,
            "avgTokens": round(float(agg.avg_tokens), 1) if agg.avg_tokens is not None else None,
            "chunksMissingPage": int(agg.missing_page or 0),
            "chunksMissingSection": int(agg.missing_section or 0),
            "shortChunks": int(agg.short or 0),
            "ocrChunks": int(agg.ocr or 0),
            "tableChunks": int(agg.table or 0),
            "promptInjectionChunks": int(agg.injection or 0),
            "flaggedChunks": int(agg.flagged or 0),
        }

    # ── chunks ──────────────────────────────────────────────────────────────

    def _chunk_where(self, tenant_id: str | None, f: ChunkFilters):
        clauses: list = []
        tenant_clause = self._tenant_clause(KnowledgeChunk, tenant_id)
        if tenant_clause is not None:
            clauses.append(tenant_clause)
        if not f.include_archived:
            clauses.append(KnowledgeChunk.is_deleted.is_(False))
        if f.kb_ids:
            clauses.append(KnowledgeChunk.kb_id.in_(f.kb_ids))
        if f.document_id:
            clauses.append(KnowledgeChunk.document_id == f.document_id)
        if f.status:
            clauses.append(KnowledgeChunk.status == f.status)
        if f.language:
            clauses.append(KnowledgeChunk.language == f.language)
        if f.page_number is not None:
            clauses.append(KnowledgeChunk.page_number == f.page_number)
        if f.section:
            clauses.append(KnowledgeChunk.section.ilike(f"%{f.section.strip()}%"))
        if f.created_from:
            clauses.append(KnowledgeChunk.created_at >= f.created_from)
        if f.created_to:
            clauses.append(KnowledgeChunk.created_at <= f.created_to)
        if f.min_tokens is not None:
            clauses.append(KnowledgeChunk.token_count >= f.min_tokens)
        if f.max_tokens is not None:
            clauses.append(KnowledgeChunk.token_count <= f.max_tokens)
        if f.has_keywords is not None:
            has_kw = and_(
                KnowledgeChunk.keywords.is_not(None),
                func.jsonb_typeof(KnowledgeChunk.keywords) == "array",
                func.jsonb_array_length(KnowledgeChunk.keywords) > 0,
            )
            clauses.append(has_kw if f.has_keywords else ~has_kw)
        if f.has_metadata is not None:
            has_meta = and_(
                KnowledgeChunk.meta.is_not(None),
                func.jsonb_typeof(KnowledgeChunk.meta) == "object",
            )
            clauses.append(has_meta if f.has_metadata else ~has_meta)
        if f.flagged_only:
            clauses.append(func.jsonb_typeof(KnowledgeChunk.meta["review"]) == "object")
        if f.search:
            term = f.search.strip()
            like = f"%{term}%"
            clauses.append(
                or_(
                    KnowledgeChunk.id == term,
                    KnowledgeChunk.content.ilike(like),
                    KnowledgeChunk.section.ilike(like),
                    KnowledgeChunk.topic.ilike(like),
                    cast(KnowledgeChunk.keywords, Text).ilike(like),
                )
            )
        return clauses

    async def list_chunks(
        self,
        *,
        tenant_id: str | None,
        filters: ChunkFilters,
        page: int,
        page_size: int,
        sort_by: str | None,
        sort_dir: str,
    ) -> tuple[list[dict], int]:
        clauses = self._chunk_where(tenant_id, filters)
        # No explicit sort → natural order: chunk_index ASC within a document
        # (reading order, so boundary/overlap review is coherent), newest-first
        # otherwise. An explicit sortBy always honours the caller's direction.
        if sort_by is None:
            if filters.document_id:
                sort_col, direction = KnowledgeChunk.chunk_index, "asc"
            else:
                sort_col, direction = KnowledgeChunk.created_at, "desc"
        else:
            sort_col = _CHUNK_SORTS.get(sort_by, KnowledgeChunk.chunk_index)
            direction = sort_dir
        order = sort_col.desc() if direction == "desc" else sort_col.asc()

        async with self._session_factory() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count()).select_from(KnowledgeChunk).where(*clauses)
                    )
                ).scalar()
                or 0
            )
            rows = (
                await session.execute(
                    select(*_CHUNK_COLS)
                    .where(*clauses)
                    .order_by(order, KnowledgeChunk.chunk_index, KnowledgeChunk.id)
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()

        kbs = await self._resolve_names(set(), {r.kb_id for r in rows}, set())
        kb_map = kbs[1]
        return [self._serialize_chunk_row(r, kb_map) for r in rows], total

    def _serialize_chunk_row(self, r, kb_map: dict) -> dict:
        content = r.content or ""
        warnings = self._row_warnings(r)
        return {
            "chunkId": r.id,
            "documentId": r.document_id,
            "kbId": r.kb_id,
            "kbName": kb_map.get(r.kb_id, {}).get("name"),
            "tenantId": r.tenant_id,
            "chunkIndex": r.chunk_index,
            "pageNumber": r.page_number,
            "section": r.section,
            "topic": r.topic,
            "chunkType": r.chunk_type,
            "language": r.language,
            "keywords": r.keywords or [],
            "tokenCount": r.token_count,
            "charCount": len(content),
            "status": r.status,
            "contentPreview": content[:_CONTENT_PREVIEW],
            "content": content,
            "hasMetadata": bool(r.meta),
            "embeddingModel": r.embedding_model,
            "embeddingDimension": r.embedding_dimension,
            "embeddingGenerated": bool(r.has_embedding),
            "createdAt": _iso(r.created_at),
            "updatedAt": _iso(r.updated_at),
            "warnings": warnings,
        }

    @staticmethod
    def _row_warnings(r) -> dict:
        content = r.content or ""
        meta = r.meta or {}
        return {
            "shortChunk": (r.token_count is not None and r.token_count < SHORT_CHUNK_TOKENS)
            or len(content) < SHORT_CHUNK_CHARS,
            "emptyChunk": not content.strip(),
            "missingPage": r.page_number is None,
            "missingSection": not (r.section or "").strip(),
            "ocr": bool(meta.get("ocr_used")) or meta.get("extraction_method") not in (None, "native_text"),
            "table": bool(meta.get("table_detected"))
            or r.chunk_type == "table"
            or "<doc-table>" in content
            or "[TABLE]" in content,
            "fromImage": bool(meta.get("from_image")),
            "promptInjection": bool(meta.get("prompt_injection_flags")),
            "flaggedForReview": isinstance(meta.get("review"), dict)
            and bool(meta["review"].get("flagged")),
        }

    async def get_chunk(self, *, tenant_id: str | None, chunk_id: str) -> dict:
        clauses = [KnowledgeChunk.id == chunk_id]
        tenant_clause = self._tenant_clause(KnowledgeChunk, tenant_id)
        if tenant_clause is not None:
            clauses.append(tenant_clause)
        async with self._session_factory() as session:
            row = (await session.execute(select(*_CHUNK_COLS).where(*clauses))).one_or_none()
            if row is None:
                raise NotFoundError("Chunk not found")

            # Neighbours by chunk_index within the same (non-deleted) document.
            prev = (
                await session.execute(
                    select(*_CHUNK_COLS)
                    .where(
                        KnowledgeChunk.document_id == row.document_id,
                        KnowledgeChunk.chunk_index < row.chunk_index,
                        KnowledgeChunk.is_deleted.is_(False),
                    )
                    .order_by(KnowledgeChunk.chunk_index.desc())
                    .limit(1)
                )
            ).one_or_none()
            nxt = (
                await session.execute(
                    select(*_CHUNK_COLS)
                    .where(
                        KnowledgeChunk.document_id == row.document_id,
                        KnowledgeChunk.chunk_index > row.chunk_index,
                        KnowledgeChunk.is_deleted.is_(False),
                    )
                    .order_by(KnowledgeChunk.chunk_index.asc())
                    .limit(1)
                )
            ).one_or_none()
            dup_count = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(KnowledgeChunk)
                        .where(
                            KnowledgeChunk.document_id == row.document_id,
                            KnowledgeChunk.content_hash == row.content_hash,
                            KnowledgeChunk.is_deleted.is_(False),
                        )
                    )
                ).scalar()
                or 0
            )

        kbs = await self._resolve_names(set(), {row.kb_id}, set())
        detail = self._serialize_chunk_row(row, kbs[1])
        detail["contentPreview"] = detail["content"][:_CONTENT_PREVIEW]
        detail["metadata"] = row.meta or {}
        detail["contentHash"] = row.content_hash

        # Quality signals that need cross-row context / heavier compute.
        pii = detect_pii(row.content or "")
        injection = (row.meta or {}).get("prompt_injection_flags") or []
        overlap = _overlap_chars(prev.content if prev else "", row.content or "")
        review_meta = (row.meta or {}).get("review") if isinstance(row.meta, dict) else None
        detail["quality"] = {
            **detail["warnings"],
            "tokenCount": row.token_count,
            "charCount": len(row.content or ""),
            "overlapWithPrevChars": overlap,
            "duplicate": dup_count > 1,
            "duplicateCount": dup_count,
            "piiKinds": pii,
            "pii": bool(pii),
            "promptInjectionPatterns": injection,
            "reviewFlag": review_meta,
        }
        detail["prev"] = _neighbor(prev)
        detail["current"] = _neighbor(row)
        detail["next"] = _neighbor(nxt)
        return detail

    # ── curation actions ────────────────────────────────────────────────────

    async def _load_chunk_row(self, session, tenant_id: str | None, chunk_id: str) -> KnowledgeChunk:
        clauses = [KnowledgeChunk.id == chunk_id]
        tenant_clause = self._tenant_clause(KnowledgeChunk, tenant_id)
        if tenant_clause is not None:
            clauses.append(tenant_clause)
        chunk = (
            await session.execute(select(KnowledgeChunk).where(*clauses))
        ).scalar_one_or_none()
        if chunk is None:
            raise NotFoundError("Chunk not found")
        return chunk

    async def set_chunk_status(
        self, *, tenant_id: str | None, chunk_id: str, status: str, updated_by: str | None
    ) -> dict:
        if status not in _CHUNK_STATUSES:
            raise ApiError("status must be 'active' or 'archived'", status_code=422)
        async with self._session_factory() as session:
            chunk = await self._load_chunk_row(session, tenant_id, chunk_id)
            previous = chunk.status
            chunk.status = status
            chunk.updated_by = updated_by
            await session.commit()
        return {"chunkId": chunk_id, "status": status, "previousStatus": previous}

    async def flag_chunk(
        self,
        *,
        tenant_id: str | None,
        chunk_id: str,
        flagged: bool,
        reason: str | None,
        flagged_by: str | None,
    ) -> dict:
        async with self._session_factory() as session:
            chunk = await self._load_chunk_row(session, tenant_id, chunk_id)
            meta = dict(chunk.meta or {})
            if flagged:
                meta["review"] = {
                    "flagged": True,
                    "reason": (reason or "").strip()[:500] or None,
                    "by": flagged_by,
                }
            else:
                meta.pop("review", None)
            chunk.meta = meta
            chunk.updated_by = flagged_by
            await session.commit()
        return {"chunkId": chunk_id, "flagged": flagged, "reason": reason}

    # ── retrieval testing ─────────────────────────────────────────────────

    async def retrieval_test(
        self,
        *,
        caller_tenant_id: str | None,
        kb_ids: list[str] | None,
        document_id: str | None,
        query: str,
        top_k: int,
        min_score: float | None,
    ) -> dict:
        from shared.knowledge.service import get_knowledge_service

        settings = get_settings()
        threshold = min_score if min_score is not None else settings.retrieval_min_score

        # Resolve the scope: an explicit KB list, or the document's KB.
        if document_id and not kb_ids:
            doc = await self.get_document(tenant_id=caller_tenant_id, document_id=document_id)
            kb_ids = [doc["kbId"]]
        if not kb_ids:
            raise ApiError("A knowledge base or document is required for retrieval testing", 422)

        # Retrieval testing runs in the KB's own tenant scope so a Super Admin
        # can test any tenant's KB. A tenant-scoped reviewer is restricted to
        # their own tenant's KBs (checked below).
        _, kb_map, _ = await self._resolve_names(set(), set(kb_ids), set())
        missing = [k for k in kb_ids if k not in kb_map]
        if missing:
            raise NotFoundError("Knowledge base not found")
        tenants = {kb_map[k]["tenantId"] for k in kb_ids}
        if len(tenants) > 1:
            raise ApiError("Retrieval testing supports one tenant's knowledge bases at a time", 422)
        target_tenant = next(iter(tenants))
        if caller_tenant_id is not None and target_tenant != caller_tenant_id:
            raise NotFoundError("Knowledge base not found")

        # Search with min_score=0 so candidates below the answerability threshold
        # are still returned for inspection; the real threshold is applied here.
        result = await get_knowledge_service().search(
            RetrievalRequest(
                tenant_id=target_tenant,
                kb_ids=kb_ids,
                query=query,
                top_k=top_k,
                min_score=0.0,
                include_global=True,
            )
        )

        doc_ids = {s.document_id for s in result.sources}
        doc_names = await self._document_names(doc_ids)
        results = []
        for rank, s in enumerate(result.sources, start=1):
            vector = s.vector_score or 0.0
            results.append(
                {
                    "rank": rank,
                    "chunkId": s.chunk_id,
                    "documentId": s.document_id,
                    "documentName": s.document_name or doc_names.get(s.document_id),
                    "kbId": s.kb_id,
                    "pageNumber": s.page_number,
                    "section": s.section,
                    "score": round(s.score, 4),
                    "vectorScore": round(vector, 4),
                    "keywordScore": round(s.keyword_score, 4) if s.keyword_score is not None else None,
                    "passedThreshold": vector >= threshold,
                    "text": (s.text or "")[:800],
                }
            )
        confidence = max((r["vectorScore"] for r in results), default=0.0)
        return {
            "query": result.query,
            "kbIds": kb_ids,
            "tenantId": target_tenant,
            "topK": top_k,
            "threshold": round(threshold, 4),
            "confidence": round(confidence, 4),
            "answerable": confidence >= threshold and bool(results),
            "durationMs": result.duration_ms,
            "results": results,
        }

    async def _document_names(self, doc_ids: set[str]) -> dict[str, str]:
        if not doc_ids:
            return {}
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(KnowledgeDocument.id, KnowledgeDocument.file_name).where(
                        KnowledgeDocument.id.in_(doc_ids)
                    )
                )
            ).all()
        return {r.id: r.file_name for r in rows}

    # ── filter facets ─────────────────────────────────────────────────────

    async def facets(self, *, tenant_id: str | None) -> dict:
        """Distinct values for filter dropdowns, plus the static enums."""
        async with self._session_factory() as session:
            doc_where = [] if tenant_id is None else [KnowledgeDocument.tenant_id == tenant_id]
            file_types = (
                (
                    await session.execute(
                        select(KnowledgeDocument.file_ext)
                        .where(*doc_where)
                        .distinct()
                        .order_by(KnowledgeDocument.file_ext)
                    )
                )
                .scalars()
                .all()
            )
            chunk_where = [] if tenant_id is None else [KnowledgeChunk.tenant_id == tenant_id]
            languages = (
                (
                    await session.execute(
                        select(KnowledgeChunk.language)
                        .where(KnowledgeChunk.language.is_not(None), *chunk_where)
                        .distinct()
                        .order_by(KnowledgeChunk.language)
                    )
                )
                .scalars()
                .all()
            )
        tenants = await asyncio.to_thread(self._list_tenants, tenant_id)
        return {
            "tenants": tenants,
            "fileTypes": [ft for ft in file_types if ft],
            "languages": [ln for ln in languages if ln],
            "uploadStatuses": _UPLOAD_STATUSES,
            "ingestionStatuses": _INGESTION_STATUSES,
            "chunkStatuses": _CHUNK_STATUSES,
        }

    @staticmethod
    def _list_tenants(tenant_id: str | None) -> list[dict]:
        session = get_sessionmaker()()
        try:
            stmt = select(Tenant).where(Tenant.is_deleted.is_(False))
            if tenant_id is not None:
                stmt = stmt.where(Tenant.id == tenant_id)
            return [
                {"id": t.id, "name": t.name, "code": t.code}
                for t in session.execute(stmt.order_by(Tenant.name)).scalars()
            ]
        finally:
            session.close()

    async def list_knowledge_bases(self, *, tenant_id: str | None) -> list[dict]:
        """KBs (from the control plane) for the KB filter dropdown."""

        def _q() -> list[dict]:
            session = get_sessionmaker()()
            try:
                stmt = select(KnowledgeSource).where(KnowledgeSource.is_deleted.is_(False))
                if tenant_id is not None:
                    stmt = stmt.where(
                        or_(
                            KnowledgeSource.tenant_id == tenant_id,
                            KnowledgeSource.scope == "global",
                        )
                    )
                return [
                    {
                        "id": k.id,
                        "name": k.name,
                        "tenantId": k.tenant_id,
                        "scope": k.scope,
                        "status": k.status,
                        "chunks": k.chunks,
                    }
                    for k in session.execute(stmt.order_by(KnowledgeSource.name)).scalars()
                ]
            finally:
                session.close()

        return await asyncio.to_thread(_q)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _neighbor(row) -> dict | None:
    if row is None:
        return None
    return {
        "chunkId": row.id,
        "chunkIndex": row.chunk_index,
        "pageNumber": row.page_number,
        "section": row.section,
        "content": row.content,
        "status": row.status,
    }


def _overlap_chars(prev: str, cur: str, cap: int = _OVERLAP_CAP) -> int:
    """Longest suffix of `prev` that is a prefix of `cur` (chunk overlap size)."""
    if not prev or not cur:
        return 0
    maxlen = min(len(prev), len(cur), cap)
    tail = prev[-maxlen:]
    head = cur[:maxlen]
    for k in range(maxlen, 0, -1):
        if tail[-k:] == head[:k]:
            return k
    return 0


_service: KnowledgeReviewService | None = None


def get_review_service() -> KnowledgeReviewService:
    global _service
    if _service is None:
        _service = KnowledgeReviewService()
    return _service
