"""Telephony call id on conversations; tenant API tokens via service accounts.

Revision ID: b2d4f6a8c0e2
Revises: a9c1e3b5d7f9
Create Date: 2026-10-21

1. ``conversation_sessions.call_id`` — the telephony provider's own call id
   (webhook ``callId``) was kept only in the Redis session and the Mongo
   ``call_started`` event, so an integrator holding a FreeSWITCH/Vaani call
   id could not find the conversation. Indexed tenant-first: lookups are
   always "this tenant's call", never a global search. Historical rows are
   backfilled from the Mongo events by
   ``backend/scripts/backfill_conversation_call_ids.py``, not here.

2. ``users.is_service_account`` + ``tenant_api_keys`` — a tenant API token
   authenticates as a per-tenant service account (a users row that cannot
   sign in), so the existing permission, tenant-isolation and audit paths
   apply unchanged. Only the key's SHA-256 is stored.

Additive and non-destructive: nullable column, boolean with a server default
of 0, one new table.
"""

import sqlalchemy as sa
from alembic import op

revision = "b2d4f6a8c0e2"
down_revision = "a9c1e3b5d7f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_sessions",
        sa.Column("call_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_conversations_tenant_call_id", "conversation_sessions",
        ["tenant_id", "call_id"], unique=False,
    )

    op.add_column(
        "users",
        sa.Column("is_service_account", sa.Boolean(), nullable=False,
                  server_default=sa.text("0")),
    )

    op.create_table(
        "tenant_api_keys",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("tenant_id", sa.String(length=40),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(length=40),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False, server_default="Integration token"),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(length=40), nullable=True),
        sa.Column("updated_by", sa.String(length=40), nullable=True),
    )
    op.create_index("ix_tenant_api_keys_tenant_id", "tenant_api_keys", ["tenant_id"], unique=False)
    op.create_index("ix_tenant_api_keys_tenant_status", "tenant_api_keys", ["tenant_id", "status"], unique=False)
    op.create_index("ux_tenant_api_keys_key_hash", "tenant_api_keys", ["key_hash"], unique=True)


def downgrade() -> None:
    # Dropping the table drops its indexes and foreign keys with it. The
    # indexes must NOT be dropped first: MySQL refuses to drop the tenant_id
    # index while the foreign key still relies on it (error 1553).
    op.drop_table("tenant_api_keys")
    op.drop_column("users", "is_service_account")
    op.drop_index("ix_conversations_tenant_call_id", table_name="conversation_sessions")
    op.drop_column("conversation_sessions", "call_id")
