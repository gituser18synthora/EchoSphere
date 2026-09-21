"""Workflow revision snapshots + per-release workflow pins.

Revision ID: a1b2c3d4e5f6
Revises: b5d7f9a1c3e5
Create Date: 2026-09-18

Until this migration is applied the runtime keeps executing the latest
saved workflow version (feature-detected at runtime); afterwards a published
release pins the workflow versions that were live at publish time.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "b5d7f9a1c3e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("pinned_workflows", sa.JSON(), nullable=True))
    op.create_table(
        "workflow_revisions",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("workflow_id", sa.String(length=40), sa.ForeignKey("workflows.id"), nullable=False),
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("bot_id", sa.String(length=40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("nodes", sa.JSON(), nullable=True),
        sa.Column("edges", sa.JSON(), nullable=True),
        sa.Column("behavior_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_workflow_revisions_wf_version", "workflow_revisions",
                    ["workflow_id", "version"], unique=True)
    op.create_index("ix_workflow_revisions_tenant_id", "workflow_revisions", ["tenant_id"])
    op.create_index("ix_workflow_revisions_bot_id", "workflow_revisions", ["bot_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_revisions_bot_id", table_name="workflow_revisions")
    op.drop_index("ix_workflow_revisions_tenant_id", table_name="workflow_revisions")
    op.drop_index("ix_workflow_revisions_wf_version", table_name="workflow_revisions")
    op.drop_table("workflow_revisions")
    op.drop_column("releases", "pinned_workflows")
