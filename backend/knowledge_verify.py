"""Knowledge-plane data verification and safe re-indexing (CLI helpers).

`verify` cross-checks the MySQL control plane (`knowledge_sources`) against the
PostgreSQL knowledge plane (documents/chunks/indexes) and reports:
  - searchable sources with no active chunks
  - active chunks with NULL embeddings
  - embedding model/dimension drift vs. the configured embedder
  - orphan chunks whose kb_id no longer exists in the control plane
  - MySQL `chunks` counters that disagree with actual PG chunk counts
  - missing HNSW / tsvector GIN indexes

`reindex` re-queues ingestion for every non-deleted document of a KB. Safe:
chunks are upserted on (document_id, chunk_index), so a re-run replaces rows
in place instead of duplicating them, and old chunks stay searchable until
the new run overwrites them.
"""

import asyncio

from sqlalchemy import func, select, text

from shared.config import get_settings
from shared.db.mysql import get_sessionmaker
from shared.knowledge.models import EMBEDDING_DIM, KnowledgeChunk, KnowledgeDocument
from shared.models import KnowledgeSource

_SEARCHABLE = {"indexed", "stale"}
_REQUIRED_INDEXES = {"ix_kchunk_embedding_hnsw", "ix_kchunk_content_tsv"}


def _load_sources() -> list[KnowledgeSource]:
    session = get_sessionmaker()()
    try:
        return list(
            session.execute(
                select(KnowledgeSource).where(KnowledgeSource.is_deleted.is_(False))
            ).scalars()
        )
    finally:
        session.close()


async def _pg_stats() -> dict:
    from shared.db.postgres import get_pg_sessionmaker

    async with get_pg_sessionmaker()() as session:
        chunk_rows = (
            await session.execute(
                select(
                    KnowledgeChunk.kb_id,
                    func.count().label("total"),
                    func.count().filter(KnowledgeChunk.embedding.is_(None)).label("no_embedding"),
                )
                .where(KnowledgeChunk.status == "active", KnowledgeChunk.is_deleted.is_(False))
                .group_by(KnowledgeChunk.kb_id)
            )
        ).all()
        model_rows = (
            await session.execute(
                select(
                    KnowledgeChunk.embedding_model,
                    KnowledgeChunk.embedding_dimension,
                    func.count(),
                )
                .where(KnowledgeChunk.status == "active", KnowledgeChunk.is_deleted.is_(False))
                .group_by(KnowledgeChunk.embedding_model, KnowledgeChunk.embedding_dimension)
            )
        ).all()
        index_rows = (
            await session.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'knowledge_chunks'")
            )
        ).all()
        ready_docs_no_chunks = (
            await session.execute(
                select(KnowledgeDocument.id, KnowledgeDocument.kb_id, KnowledgeDocument.file_name)
                .where(
                    KnowledgeDocument.status == "ready",
                    KnowledgeDocument.is_deleted.is_(False),
                    KnowledgeDocument.chunk_count == 0,
                )
            )
        ).all()
    return {
        "chunks_by_kb": {r.kb_id: (r.total, r.no_embedding) for r in chunk_rows},
        "models": [(m or "<null>", d, c) for m, d, c in model_rows],
        "indexes": {r.indexname for r in index_rows},
        "ready_docs_no_chunks": ready_docs_no_chunks,
    }


def run_verify() -> int:
    """Print a verification report; return the number of problems found."""
    settings = get_settings()
    sources = _load_sources()
    stats = asyncio.run(_pg_stats())
    chunks_by_kb: dict[str, tuple[int, int]] = stats["chunks_by_kb"]
    problems = 0

    print(f"Knowledge verification — {len(sources)} active sources, "
          f"{sum(t for t, _ in chunks_by_kb.values())} active chunks\n")

    print("── Sources vs chunks ──")
    known_kb_ids = {s.id for s in sources}
    for s in sources:
        total, no_emb = chunks_by_kb.get(s.id, (0, 0))
        notes = []
        if s.status in _SEARCHABLE and total == 0:
            notes.append("searchable but has NO chunks (won't return results)")
        if no_emb:
            notes.append(f"{no_emb} chunks missing embeddings")
        if s.chunks != total:
            notes.append(f"MySQL counter says {s.chunks}, PG has {total}")
        if notes:
            problems += 1
            print(f"  ✗ {s.id} [{s.status:8}] {s.name!r}: " + "; ".join(notes))
    orphans = set(chunks_by_kb) - known_kb_ids
    for kb in sorted(orphans):
        problems += 1
        print(f"  ✗ orphan chunks: kb_id={kb} has {chunks_by_kb[kb][0]} chunks but no live source row")
    if problems == 0:
        print("  ✓ every searchable source has chunks, counters match, no orphans")

    print("\n── Embeddings ──")
    expected_model = (
        "mock-embedding" if settings.embedding_provider == "mock" else settings.embedding_model
    )
    for model, dim, count in stats["models"]:
        drift = []
        if model != expected_model:
            drift.append(f"model != configured ({expected_model})")
        if dim != EMBEDDING_DIM:
            drift.append(f"dimension {dim} != store dimension {EMBEDDING_DIM}")
        marker = "✗" if drift else "✓"
        if drift:
            problems += 1
        print(f"  {marker} {count} chunks: model={model} dim={dim}"
              + (f" — {'; '.join(drift)}; re-index required" if drift else ""))
    if not stats["models"]:
        print("  (no active chunks)")

    print("\n── Postgres indexes ──")
    missing = _REQUIRED_INDEXES - stats["indexes"]
    for name in sorted(_REQUIRED_INDEXES):
        if name in stats["indexes"]:
            print(f"  ✓ {name}")
        else:
            problems += 1
            print(f"  ✗ {name} MISSING — run `python -m backend.cli pg-migrate`")

    if stats["ready_docs_no_chunks"]:
        print("\n── Ready documents without chunks ──")
        for doc in stats["ready_docs_no_chunks"]:
            problems += 1
            print(f"  ✗ {doc.id} kb={doc.kb_id} {doc.file_name!r} is 'ready' but produced 0 chunks")

    print(f"\n{problems} problem(s) found." if problems else "\nAll checks passed.")
    return problems


def run_reindex(kb_id: str) -> int:
    """Queue a safe re-index for every non-deleted document of a KB."""
    from shared.knowledge.service import get_knowledge_service

    async def _run() -> int:
        from shared.db.postgres import get_pg_sessionmaker

        service = get_knowledge_service()
        async with get_pg_sessionmaker()() as session:
            docs = (
                await session.execute(
                    select(KnowledgeDocument.id, KnowledgeDocument.tenant_id, KnowledgeDocument.file_name)
                    .where(
                        KnowledgeDocument.kb_id == kb_id,
                        KnowledgeDocument.is_deleted.is_(False),
                    )
                )
            ).all()
        for doc in docs:
            await service.reindex_document(tenant_id=doc.tenant_id, document_id=doc.id)
            print(f"  queued re-index: {doc.id} ({doc.file_name})")
        return len(docs)

    count = asyncio.run(_run())
    if count == 0:
        print(f"No documents found for KB {kb_id}.")
    else:
        print(f"{count} document(s) queued. The ingestion worker (embedded in the API, "
              "or `python -m backend.workers.ingestion`) will process them; chunks are "
              "upserted in place so no duplicates are created.")
    return count
