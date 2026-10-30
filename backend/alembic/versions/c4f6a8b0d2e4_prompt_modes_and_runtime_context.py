"""Full/unified prompt mode + tenant-defined runtime context.

Revision ID: c4f6a8b0d2e4
Revises: b8d0f2a4c6e9
Create Date: 2026-08-04

Prompt Studio redesign:
- prompt_versions.prompt_mode ("structured" | "full") and .full_prompt — a
  version can now be one unified prompt document; compiled_prompt stays the
  single runtime interface for both modes.
- conversation_sessions.prompt_id / .prompt_version — every conversation
  records exactly which published prompt version it ran on.

Runtime context redesign:
- runtime_context_schemas — per-bot tenant-defined field definitions, live
  source (User Details API vs manual test JSON), masking, missing-value
  policy and the opt-in domain policy pack.
- runtime_context_records — stored per-customer payloads as validated JSON
  (any domain), matched by phone/customer_ref like the legacy loan table.

Additive and non-destructive; existing rows read as structured-mode prompts.
"""

import sqlalchemy as sa
from alembic import op

revision = "c4f6a8b0d2e4"
down_revision = "b8d0f2a4c6e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prompt_versions",
        sa.Column("prompt_mode", sa.String(length=20),
                  nullable=False, server_default="structured"),
    )
    op.add_column(
        "prompt_versions",
        sa.Column("full_prompt", sa.Text(), nullable=True),
    )

    op.add_column(
        "conversation_sessions",
        sa.Column("prompt_id", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "conversation_sessions",
        sa.Column("prompt_version", sa.Integer(), nullable=True),
    )

    op.create_table(
        "runtime_context_schemas",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("tenant_id", sa.String(length=40),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("bot_id", sa.String(length=40),
                  sa.ForeignKey("voice_bots.id"), nullable=False),
        sa.Column("name", sa.String(length=200),
                  nullable=False, server_default="User details"),
        sa.Column("source_mode", sa.String(length=20),
                  nullable=False, server_default="manual"),
        sa.Column("api_connection_id", sa.String(length=40),
                  sa.ForeignKey("api_connections.id"), nullable=True),
        sa.Column("response_path", sa.String(length=200), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("allow_additional", sa.Boolean(),
                  nullable=False, server_default=sa.text("1")),
        sa.Column("test_payload", sa.JSON(), nullable=True),
        sa.Column("missing_value_policy", sa.String(length=500), nullable=True),
        sa.Column("domain_policy", sa.String(length=30),
                  nullable=False, server_default="generic"),
        sa.Column("status", sa.String(length=20),
                  nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("updated_by", sa.String(length=40), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(length=40), nullable=True),
        sa.UniqueConstraint("bot_id", name="uq_runtime_context_schema_bot"),
    )
    op.create_index("ix_runtime_context_schemas_tenant_bot",
                    "runtime_context_schemas", ["tenant_id", "bot_id"])
    op.create_index("ix_runtime_context_schemas_tenant_id",
                    "runtime_context_schemas", ["tenant_id"])
    op.create_index("ix_runtime_context_schemas_bot_id",
                    "runtime_context_schemas", ["bot_id"])

    op.create_table(
        "runtime_context_records",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("tenant_id", sa.String(length=40),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("bot_id", sa.String(length=40),
                  sa.ForeignKey("voice_bots.id"), nullable=False),
        sa.Column("customer_ref", sa.String(length=80), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("call_state", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("updated_by", sa.String(length=40), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_by", sa.String(length=40), nullable=True),
    )
    op.create_index("ix_runtime_context_records_bot_phone",
                    "runtime_context_records", ["bot_id", "phone"])
    op.create_index("ix_runtime_context_records_tenant_bot",
                    "runtime_context_records", ["tenant_id", "bot_id"])
    op.create_index("ix_runtime_context_records_tenant_id",
                    "runtime_context_records", ["tenant_id"])
    op.create_index("ix_runtime_context_records_bot_id",
                    "runtime_context_records", ["bot_id"])


def downgrade() -> None:
    op.drop_index("ix_runtime_context_records_bot_id",
                  table_name="runtime_context_records")
    op.drop_index("ix_runtime_context_records_tenant_id",
                  table_name="runtime_context_records")
    op.drop_index("ix_runtime_context_records_tenant_bot",
                  table_name="runtime_context_records")
    op.drop_index("ix_runtime_context_records_bot_phone",
                  table_name="runtime_context_records")
    op.drop_table("runtime_context_records")
    op.drop_index("ix_runtime_context_schemas_bot_id",
                  table_name="runtime_context_schemas")
    op.drop_index("ix_runtime_context_schemas_tenant_id",
                  table_name="runtime_context_schemas")
    op.drop_index("ix_runtime_context_schemas_tenant_bot",
                  table_name="runtime_context_schemas")
    op.drop_table("runtime_context_schemas")
    op.drop_column("conversation_sessions", "prompt_version")
    op.drop_column("conversation_sessions", "prompt_id")
    op.drop_column("prompt_versions", "full_prompt")
    op.drop_column("prompt_versions", "prompt_mode")
