"""PostgreSQL + pgvector store: batched idempotent upserts, dense (HNSW cosine)
and keyword (websearch tsquery) retrieval with tenant/KB filtering in SQL.

Tenant safety: callers resolve *authorized* kb_ids first (control plane), and
this store additionally filters `tenant_id` on every statement so a bug above
this layer can never leak cross-tenant rows. Deleted/archived chunks are
excluded in SQL, not in Python.
"""

import logging

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import get_settings
from shared.db.postgres import get_pg_sessionmaker, pg_health_check
from shared.knowledge.models import EMBEDDING_DIM, KnowledgeChunk
from shared.knowledge.schemas import ChunkPayload, SourceRef

logger = logging.getLogger(__name__)

_UPSERT_BATCH = 200


class PgVectorStore:
    """The single production VectorStore implementation."""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory or get_pg_sessionmaker()

    # ── writes ────────────────────────────────────────────────────────────

    async def upsert_chunks(self, chunks: list[ChunkPayload]) -> int:
        if not chunks:
            return 0
        for chunk in chunks:
            if chunk.embedding is not None and len(chunk.embedding) != EMBEDDING_DIM:
                raise ValueError(
                    f"Chunk {chunk.document_id}/{chunk.chunk_index} has embedding "
                    f"dimension {len(chunk.embedding)}, store requires {EMBEDDING_DIM}"
                )
        from shared.ids import new_id

        written = 0
        async with self._session_factory() as session:
            for start in range(0, len(chunks), _UPSERT_BATCH):
                batch = chunks[start : start + _UPSERT_BATCH]
                rows = [
                    {
                        "id": new_id("chk"),
                        "tenant_id": c.tenant_id,
                        "kb_id": c.kb_id,
                        "document_id": c.document_id,
                        "chunk_index": c.chunk_index,
                        "page_number": c.page_number,
                        "section": (c.section or None) and c.section[:300],
                        "topic": (c.topic or None) and c.topic[:300],
                        "chunk_type": c.chunk_type,
                        "keywords": c.keywords,
                        "language": c.language,
                        "content": c.content,
                        "embedding_text": c.embedding_text,
                        "content_hash": c.content_hash,
                        "token_count": c.token_count,
                        "embedding": c.embedding,
                        "embedding_model": c.embedding_model,
                        "embedding_dimension": c.embedding_dimension,
                        "status": "active",
                        "meta": c.meta,
                    }
                    for c in batch
                ]
                stmt = pg_insert(KnowledgeChunk).values(rows)
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_kchunk_doc_index",
                    set_={
                        "content": stmt.excluded.content,
                        "embedding_text": stmt.excluded.embedding_text,
                        "content_hash": stmt.excluded.content_hash,
                        "token_count": stmt.excluded.token_count,
                        "embedding": stmt.excluded.embedding,
                        "embedding_model": stmt.excluded.embedding_model,
                        "embedding_dimension": stmt.excluded.embedding_dimension,
                        "page_number": stmt.excluded.page_number,
                        "section": stmt.excluded.section,
                        "topic": stmt.excluded.topic,
                        "chunk_type": stmt.excluded.chunk_type,
                        "keywords": stmt.excluded.keywords,
                        "language": stmt.excluded.language,
                        "meta": stmt.excluded.meta,
                        "status": "active",
                        "is_deleted": False,
                        "deleted_at": None,
                        "deleted_by": None,
                        "updated_at": func.now(),
                    },
                )
                await session.execute(stmt)
                written += len(rows)
            await session.commit()
        return written

    async def delete_document(self, tenant_id: str | None, document_id: str) -> int:
        """Soft-delete every chunk of a document (never surfaces in retrieval)."""
        async with self._session_factory() as session:
            stmt = (
                update(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document_id)
                .where(self._tenant_clause(tenant_id, include_global=tenant_id is None))
                .values(is_deleted=True, status="archived", deleted_at=func.now())
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    async def delete_knowledge_base(self, tenant_id: str | None, kb_id: str) -> int:
        async with self._session_factory() as session:
            stmt = (
                update(KnowledgeChunk)
                .where(KnowledgeChunk.kb_id == kb_id)
                .where(self._tenant_clause(tenant_id, include_global=tenant_id is None))
                .values(is_deleted=True, status="archived", deleted_at=func.now())
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    async def hard_delete_document(self, tenant_id: str | None, document_id: str) -> int:
        """Physical removal — used by re-indexing, guarded by callers."""
        async with self._session_factory() as session:
            stmt = (
                delete(KnowledgeChunk)
                .where(KnowledgeChunk.document_id == document_id)
                .where(self._tenant_clause(tenant_id, include_global=tenant_id is None))
            )
            result = await session.execute(stmt)
            await session.commit()
            return result.rowcount or 0

    # ── reads ─────────────────────────────────────────────────────────────

    @staticmethod
    def _tenant_clause(tenant_id: str | None, include_global: bool):
        """Defense-in-depth tenant filter applied to every read/write."""
        if tenant_id is None:
            # Platform scope: only global (tenant-less) rows.
            return KnowledgeChunk.tenant_id.is_(None)
        if include_global:
            return (KnowledgeChunk.tenant_id == tenant_id) | KnowledgeChunk.tenant_id.is_(None)
        return KnowledgeChunk.tenant_id == tenant_id

    def _base_filters(self, tenant_id: str | None, kb_ids: list[str], include_global: bool):
        return [
            KnowledgeChunk.kb_id.in_(kb_ids),
            self._tenant_clause(tenant_id, include_global),
            KnowledgeChunk.status == "active",
            KnowledgeChunk.is_deleted.is_(False),
        ]

    async def dense_search(
        self,
        *,
        tenant_id: str | None,
        kb_ids: list[str],
        query_embedding: list[float],
        limit: int,
        include_global: bool = True,
    ) -> list[SourceRef]:
        if not kb_ids:
            return []
        settings = get_settings()
        distance = KnowledgeChunk.embedding.cosine_distance(query_embedding)
        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.kb_id,
                KnowledgeChunk.document_id,
                KnowledgeChunk.chunk_index,
                KnowledgeChunk.page_number,
                KnowledgeChunk.section,
                KnowledgeChunk.topic,
                KnowledgeChunk.content,
                KnowledgeChunk.meta,
                distance.label("distance"),
            )
            .where(*self._base_filters(tenant_id, kb_ids, include_global))
            .where(KnowledgeChunk.embedding.is_not(None))
            .order_by(distance, KnowledgeChunk.id)
            .limit(limit)
        )
        async with self._session_factory() as session:
            await session.execute(
                text("SET LOCAL hnsw.ef_search = :ef").bindparams(
                    bindparam("ef", int(settings.pgvector_hnsw_ef_search), literal_execute=True)
                )
            )
            rows = (await session.execute(stmt)).all()
        return [
            SourceRef(
                kb_id=row.kb_id,
                document_id=row.document_id,
                chunk_id=row.id,
                chunk_index=row.chunk_index,
                page_number=row.page_number,
                section=row.section,
                topic=row.topic,
                score=max(0.0, 1.0 - float(row.distance)),
                vector_score=max(0.0, 1.0 - float(row.distance)),
                text=row.content,
                document_name=(row.meta or {}).get("file_name"),
            )
            for row in rows
        ]

    async def keyword_search(
        self,
        *,
        tenant_id: str | None,
        kb_ids: list[str],
        query: str,
        limit: int,
        include_global: bool = True,
    ) -> list[SourceRef]:
        if not kb_ids or not query.strip():
            return []
        settings = get_settings()
        ts_config = settings.retrieval_ts_config

        async with self._session_factory() as session:
            rows = await self._keyword_execute(
                session, tenant_id, kb_ids, query, limit, include_global, ts_config,
                mode="websearch",
            )
            if not rows:
                # AND semantics matched nothing — retry with OR over the terms.
                rows = await self._keyword_execute(
                    session, tenant_id, kb_ids, query, limit, include_global, ts_config,
                    mode="or",
                )
        return [
            SourceRef(
                kb_id=row.kb_id,
                document_id=row.document_id,
                chunk_id=row.id,
                chunk_index=row.chunk_index,
                page_number=row.page_number,
                section=row.section,
                topic=row.topic,
                score=float(row.rank),
                keyword_score=float(row.rank),
                text=row.content,
                document_name=(row.meta or {}).get("file_name"),
            )
            for row in rows
        ]

    async def _keyword_execute(
        self,
        session: AsyncSession,
        tenant_id: str | None,
        kb_ids: list[str],
        query: str,
        limit: int,
        include_global: bool,
        ts_config: str,
        mode: str,
    ):
        if mode == "websearch":
            tsquery = func.websearch_to_tsquery(ts_config, query)
        else:
            terms = [t for t in "".join(c if c.isalnum() else " " for c in query).split() if t]
            if not terms:
                return []
            tsquery = func.to_tsquery(ts_config, " | ".join(terms))
        tsvector = func.to_tsvector(ts_config, KnowledgeChunk.content)
        rank = func.ts_rank_cd(tsvector, tsquery).label("rank")
        stmt = (
            select(
                KnowledgeChunk.id,
                KnowledgeChunk.kb_id,
                KnowledgeChunk.document_id,
                KnowledgeChunk.chunk_index,
                KnowledgeChunk.page_number,
                KnowledgeChunk.section,
                KnowledgeChunk.topic,
                KnowledgeChunk.content,
                KnowledgeChunk.meta,
                rank,
            )
            .where(*self._base_filters(tenant_id, kb_ids, include_global))
            .where(tsvector.op("@@")(tsquery))
            .order_by(rank.desc(), KnowledgeChunk.id)
            .limit(limit)
        )
        return (await session.execute(stmt)).all()

    async def count_chunks(self, tenant_id: str | None, kb_id: str) -> int:
        async with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(*self._base_filters(tenant_id, [kb_id], include_global=True))
            )
            return int((await session.execute(stmt)).scalar() or 0)

    async def health_check(self) -> dict:
        return await pg_health_check()
