"""knowledge plane initial: documents, chunks, embeddings, ingestion jobs

Revision ID: a1f2c3d4e5f6
Revises:
Create Date: 2026-07-16

Creates the pgvector extension, the three knowledge tables, the HNSW vector
index (cosine — matches OpenAI text-embedding-* models) and the full-text
GIN index used by keyword retrieval.

Rollback: `alembic -c backend/alembic_pg.ini downgrade base` drops the tables
(extensions are left installed; they are shared database objects).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from backend.config import get_settings
from backend.knowledge.models import EMBEDDING_DIM

revision: str = "a1f2c3d4e5f6"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    settings = get_settings()

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "knowledge_documents",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=True),
        sa.Column("kb_id", sa.String(40), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("file_ext", sa.String(16), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(128), nullable=False, server_default=""),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.String(512), nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("doc_type", sa.String(40), nullable=True),
        sa.Column("language", sa.String(20), nullable=True),
        sa.Column("page_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("embedding_dimension", sa.Integer, nullable=True),
        sa.Column("meta", JSONB, nullable=True),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("updated_by", sa.String(40), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("kb_id", "content_hash", name="uq_kdoc_kb_content_hash"),
    )
    op.create_index("ix_kdoc_tenant_kb", "knowledge_documents", ["tenant_id", "kb_id"])
    op.create_index("ix_kdoc_kb_status", "knowledge_documents", ["kb_id", "status"])
    op.create_index("ix_kdoc_status", "knowledge_documents", ["status"])
    op.create_index("ix_kdoc_is_deleted", "knowledge_documents", ["is_deleted"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=True),
        sa.Column("kb_id", sa.String(40), nullable=False),
        sa.Column(
            "document_id",
            sa.String(40),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("page_number", sa.Integer, nullable=True),
        sa.Column("section", sa.String(300), nullable=True),
        sa.Column("topic", sa.String(300), nullable=True),
        sa.Column("chunk_type", sa.String(40), nullable=True),
        sa.Column("keywords", JSONB, nullable=True),
        sa.Column("language", sa.String(20), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding_text", sa.Text, nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("embedding_dimension", sa.Integer, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("meta", JSONB, nullable=True),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("updated_by", sa.String(40), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(40), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_kchunk_doc_index"),
    )
    op.create_index("ix_kchunk_tenant_kb", "knowledge_chunks", ["tenant_id", "kb_id"])
    op.create_index(
        "ix_kchunk_kb_status_deleted", "knowledge_chunks", ["kb_id", "status", "is_deleted"]
    )
    op.create_index("ix_kchunk_document", "knowledge_chunks", ["document_id"])

    # Vector ANN index — HNSW, cosine (matches the OpenAI embedding family).
    op.execute(
        "CREATE INDEX ix_kchunk_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops) "
        f"WITH (m = {int(settings.pgvector_hnsw_m)}, "
        f"ef_construction = {int(settings.pgvector_hnsw_ef_construction)})"
    )
    # Keyword retrieval — expression GIN index; the ts config MUST match
    # RETRIEVAL_TS_CONFIG used in queries.
    op.execute(
        "CREATE INDEX ix_kchunk_content_tsv ON knowledge_chunks "
        f"USING gin (to_tsvector('{settings.retrieval_ts_config}', content))"
    )

    op.create_table(
        "knowledge_ingestion_jobs",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), nullable=True),
        sa.Column("kb_id", sa.String(40), nullable=False),
        sa.Column(
            "document_id",
            sa.String(40),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(30), nullable=True),
        sa.Column("progress", sa.Float, nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_kjob_status_queued", "knowledge_ingestion_jobs", ["status", "queued_at"])
    op.create_index("ix_kjob_document", "knowledge_ingestion_jobs", ["document_id"])
    op.create_index("ix_kjob_tenant_kb", "knowledge_ingestion_jobs", ["tenant_id", "kb_id"])


def downgrade() -> None:
    op.drop_table("knowledge_ingestion_jobs")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    # Extensions are shared database objects — intentionally not dropped.
