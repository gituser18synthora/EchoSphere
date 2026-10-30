"""Persistent post-call conversation memory (summary / outcome / NBA).

One row per completed conversation: created in status ``queued`` at call
finalize (the unique conversation_id is the idempotency boundary — a
duplicate hangup can never create a second record), filled by the post-call
processor with the validated structured memory, call outcome and Next Best
Action. Latest-memory lookups are tenant+bot scoped per customer-resolution
path (runtime context record, legacy customer context, phone tail).

Revision ID: f1b3d5a7c9e2
Revises: e9a1c3b5d7f9
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op

revision = "f1b3d5a7c9e2"
down_revision = "e9a1c3b5d7f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_memories",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("tenants.id"),
                  nullable=False, index=True),
        sa.Column("conversation_id", sa.String(40),
                  sa.ForeignKey("conversation_sessions.id"), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("bot_id", sa.String(40), sa.ForeignKey("voice_bots.id"),
                  nullable=False, index=True),
        sa.Column("channel", sa.String(20), nullable=False,
                  server_default="voice"),
        sa.Column("runtime_context_record_id", sa.String(40), nullable=True),
        sa.Column("customer_context_id", sa.String(40), nullable=True),
        sa.Column("phone_tail", sa.String(15), nullable=True),
        sa.Column("status", sa.String(20), nullable=False,
                  server_default="queued"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False,
                  server_default="3"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_processing_at", sa.DateTime, nullable=True),
        sa.Column("generated_at", sa.DateTime, nullable=True),
        sa.Column("final_state", sa.JSON, nullable=True),
        sa.Column("call_outcome", sa.String(60), nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("memory", sa.JSON, nullable=True),
        sa.Column("next_action", sa.String(60), nullable=True),
        sa.Column("next_best_action", sa.JSON, nullable=True),
        sa.Column("follow_up_required", sa.Boolean, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("follow_up_at", sa.DateTime, nullable=True),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("language", sa.String(15), nullable=True),
        sa.Column("dominant_language", sa.String(15), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(),
                  nullable=False),
        sa.Column("is_deleted", sa.Boolean, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(40), nullable=True),
        sa.UniqueConstraint("conversation_id", name="uq_conversation_memory"),
    )
    op.create_index(
        "ix_conv_memory_record", "conversation_memories",
        ["tenant_id", "bot_id", "runtime_context_record_id", "created_at"],
    )
    op.create_index(
        "ix_conv_memory_cctx", "conversation_memories",
        ["tenant_id", "bot_id", "customer_context_id", "created_at"],
    )
    op.create_index(
        "ix_conv_memory_phone", "conversation_memories",
        ["tenant_id", "bot_id", "phone_tail", "created_at"],
    )
    op.create_index(
        "ix_conv_memory_status", "conversation_memories",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conv_memory_status", table_name="conversation_memories")
    op.drop_index("ix_conv_memory_phone", table_name="conversation_memories")
    op.drop_index("ix_conv_memory_cctx", table_name="conversation_memories")
    op.drop_index("ix_conv_memory_record", table_name="conversation_memories")
    op.drop_table("conversation_memories")
