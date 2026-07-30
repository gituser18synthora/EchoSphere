"""chunk review: token_count column + review filter/sort indexes

Revision ID: b2c4e6f8a0d1
Revises: a1f2c3d4e5f6
Create Date: 2026-07-21

Adds `knowledge_chunks.token_count` (populated at ingestion going forward;
existing rows are backfilled with an approximate char/4 estimate) plus the
indexes the Super-Admin Chunk Review console filters and sorts on
(page_number, language, created_at, token_count, and per-document status).
The document list gets created_at + file_ext indexes for ordering/filtering.

Rollback: `alembic -c backend/alembic_pg.ini downgrade a1f2c3d4e5f6` drops the
added column and indexes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c4e6f8a0d1"
down_revision: Union[str, None] = "a1f2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunks",
        sa.Column("token_count", sa.Integer, nullable=True),
    )
    # Backfill existing rows with an approximate token estimate (real counts are
    # written by the ingestion pipeline on the next (re)index). GREATEST(1, ...)
    # so a non-empty chunk never records zero tokens.
    op.execute(
        "UPDATE knowledge_chunks "
        "SET token_count = GREATEST(1, CEIL(char_length(content) / 4.0)) "
        "WHERE token_count IS NULL"
    )

    op.create_index(
        "ix_kchunk_doc_status",
        "knowledge_chunks",
        ["document_id", "status", "is_deleted"],
    )
    op.create_index("ix_kchunk_language", "knowledge_chunks", ["language"])
    op.create_index("ix_kchunk_page_number", "knowledge_chunks", ["page_number"])
    op.create_index("ix_kchunk_created_at", "knowledge_chunks", ["created_at"])
    op.create_index("ix_kchunk_token_count", "knowledge_chunks", ["token_count"])

    op.create_index("ix_kdoc_created_at", "knowledge_documents", ["created_at"])
    op.create_index("ix_kdoc_file_ext", "knowledge_documents", ["file_ext"])


def downgrade() -> None:
    op.drop_index("ix_kdoc_file_ext", table_name="knowledge_documents")
    op.drop_index("ix_kdoc_created_at", table_name="knowledge_documents")
    op.drop_index("ix_kchunk_token_count", table_name="knowledge_chunks")
    op.drop_index("ix_kchunk_created_at", table_name="knowledge_chunks")
    op.drop_index("ix_kchunk_page_number", table_name="knowledge_chunks")
    op.drop_index("ix_kchunk_language", table_name="knowledge_chunks")
    op.drop_index("ix_kchunk_doc_status", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "token_count")
