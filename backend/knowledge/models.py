"""PostgreSQL models for the knowledge plane (documents, chunks, embeddings, jobs).

Kept on a separate DeclarativeBase from the MySQL control plane so each Alembic
environment migrates exactly one database. `kb_id` references the MySQL
`knowledge_sources.id` logically (cross-database, so no FK constraint).

Tenant isolation: every row carries `tenant_id` (NULL = platform/global scope)
and `kb_id`; retrieval always filters on both after the caller's KB authorization
has been resolved against the MySQL control plane.
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID_LEN = 40

# The vector column dimension is fixed at migration time. Documents with a
# different embedding dimension are rejected at write time (see vector store).
EMBEDDING_DIM = 1536


class PGBase(DeclarativeBase):
    pass


class PGTimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PGSoftDeleteMixin:
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)


class KnowledgeDocument(PGBase, PGTimestampMixin, PGSoftDeleteMixin):
    """One uploaded file (or URL/FAQ payload) inside a knowledge base."""

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    kb_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)

    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    # pending | processing | ready | failed | cancelled | archived
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    doc_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)

    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)

    __table_args__ = (
        UniqueConstraint("kb_id", "content_hash", name="uq_kdoc_kb_content_hash"),
        Index("ix_kdoc_tenant_kb", "tenant_id", "kb_id"),
        Index("ix_kdoc_kb_status", "kb_id", "status"),
        Index("ix_kdoc_status", "status"),
        Index("ix_kdoc_is_deleted", "is_deleted"),
    )


class KnowledgeChunk(PGBase, PGTimestampMixin, PGSoftDeleteMixin):
    """A retrievable chunk with its embedding. Filters used by retrieval:
    (tenant_id, kb_id, status, is_deleted) — all covered by composite indexes.
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    kb_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(ID_LEN),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    section: Mapped[str | None] = mapped_column(String(300), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(300), nullable=True)
    chunk_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    keywords: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)

    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # active | archived
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_kchunk_doc_index"),
        Index("ix_kchunk_tenant_kb", "tenant_id", "kb_id"),
        Index("ix_kchunk_kb_status_deleted", "kb_id", "status", "is_deleted"),
        Index("ix_kchunk_document", "document_id"),
        # HNSW + tsvector GIN indexes are created in the Alembic migration
        # (expression indexes; kept out of the ORM definition).
    )


class IngestionJob(PGBase, PGTimestampMixin):
    """Durable background-ingestion job (DB-backed queue, polled by the worker).

    Idempotent: one active job per document; retries increment `attempts`.
    """

    __tablename__ = "knowledge_ingestion_jobs"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    tenant_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    kb_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False)
    document_id: Mapped[str] = mapped_column(
        String(ID_LEN),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )

    # queued | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_kjob_status_queued", "status", "queued_at"),
        Index("ix_kjob_document", "document_id"),
        Index("ix_kjob_tenant_kb", "tenant_id", "kb_id"),
    )
