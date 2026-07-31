"""Retained source audio for cloned voices.

Revision ID: f4b6d8e0a2c4
Revises: e2a4c6d8f0b2
Create Date: 2026-07-30

New table voice_clone_audio: one row per audio sample a tenant voice clone
was built from. The files themselves live under VOICE_CLONE_AUDIO_DIR
(storage/voice_clones by default) at storage_path — server-generated,
tenant-scoped paths. Rows created before this feature simply do not exist;
the API reports "source audio unavailable" for those clones.

Rollback: `alembic downgrade e2a4c6d8f0b2` (drops the table; stored files
are not touched).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f4b6d8e0a2c4"
down_revision: Union[str, None] = "e2a4c6d8f0b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _has_table("voice_clone_audio"):
        return
    op.create_table(
        "voice_clone_audio",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(40),
            sa.ForeignKey("tenants.id", name="fk_voice_clone_audio_tenant_id"),
            nullable=False,
        ),
        sa.Column(
            "voice_id", sa.String(40),
            sa.ForeignKey("voice_profiles.id", name="fk_voice_clone_audio_voice_id"),
            nullable=False,
        ),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_sec", sa.Float, nullable=True),
        sa.Column(
            "source_type", sa.String(20), nullable=False,
            server_default="file_upload",
        ),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("provider_voice_id", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="stored"),
        sa.Column(
            "created_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime, nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("updated_by", sa.String(40), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(40), nullable=True),
    )
    op.create_index(
        "ix_voice_clone_audio_tenant_id", "voice_clone_audio", ["tenant_id"]
    )
    op.create_index(
        "ix_voice_clone_audio_voice_id", "voice_clone_audio", ["voice_id"]
    )


def downgrade() -> None:
    if _has_table("voice_clone_audio"):
        op.drop_table("voice_clone_audio")
