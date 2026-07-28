"""Tenant-owned voice clones on voice_profiles.

Revision ID: e2a4c6d8f0b2
Revises: c1d3e5f7a9b1
Create Date: 2026-07-27

Additive only:
- voice_profiles.tenant_id: NULL = platform catalog voice (visible to every
  tenant, unchanged behavior for all existing rows); set = the owning tenant's
  private voice (e.g. an ElevenLabs instant voice clone).
- voice_profiles.source: "platform" | "cloned" — distinguishes curated catalog
  voices from tenant-created provider clones.
- voice_profiles.clone_metadata: JSON clone provenance (sample file names and
  sizes, requires_verification, provider options). Training audio is never
  stored.

Rollback: `alembic downgrade c1d3e5f7a9b1`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e2a4c6d8f0b2"
down_revision: Union[str, None] = "c1d3e5f7a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in {ix["name"] for ix in inspector.get_indexes(table)}


def _has_fk(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in {fk["name"] for fk in inspector.get_foreign_keys(table)}


def upgrade() -> None:
    if not _has_column("voice_profiles", "tenant_id"):
        op.add_column(
            "voice_profiles", sa.Column("tenant_id", sa.String(40), nullable=True)
        )
    if not _has_index("voice_profiles", "ix_voice_profiles_tenant_id"):
        op.create_index("ix_voice_profiles_tenant_id", "voice_profiles", ["tenant_id"])
    if not _has_fk("voice_profiles", "fk_voice_profiles_tenant_id"):
        op.create_foreign_key(
            "fk_voice_profiles_tenant_id", "voice_profiles", "tenants",
            ["tenant_id"], ["id"],
        )
    if not _has_column("voice_profiles", "source"):
        op.add_column(
            "voice_profiles",
            sa.Column(
                "source", sa.String(20), nullable=False,
                server_default=sa.text("'platform'"),
            ),
        )
    if not _has_column("voice_profiles", "clone_metadata"):
        op.add_column("voice_profiles", sa.Column("clone_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    if _has_column("voice_profiles", "clone_metadata"):
        op.drop_column("voice_profiles", "clone_metadata")
    if _has_column("voice_profiles", "source"):
        op.drop_column("voice_profiles", "source")
    if _has_fk("voice_profiles", "fk_voice_profiles_tenant_id"):
        op.drop_constraint("fk_voice_profiles_tenant_id", "voice_profiles", type_="foreignkey")
    if _has_index("voice_profiles", "ix_voice_profiles_tenant_id"):
        op.drop_index("ix_voice_profiles_tenant_id", table_name="voice_profiles")
    if _has_column("voice_profiles", "tenant_id"):
        op.drop_column("voice_profiles", "tenant_id")
