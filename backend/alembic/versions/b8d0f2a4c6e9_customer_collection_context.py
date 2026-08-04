"""Customer collection context table + conversation disposition.

Revision ID: b8d0f2a4c6e9
Revises: f2a4c6e8b0d2
Create Date: 2026-08-04

Adds the per-customer collection context the voice runtime loads at call
start (identity, overdue facts, payment options, mutable call-state flags)
and a `disposition` column on conversation_sessions so the captured call
outcome (promise to pay, payment claimed, wrong number, dispute, callback,
complaint, escalation) is queryable in the control plane.

Additive and non-destructive: a new table plus one nullable column.
"""

import sqlalchemy as sa
from alembic import op

revision = "b8d0f2a4c6e9"
down_revision = "f2a4c6e8b0d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_contexts",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column("tenant_id", sa.String(length=40),
                  sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("bot_id", sa.String(length=40),
                  sa.ForeignKey("voice_bots.id"), nullable=False),
        sa.Column("customer_ref", sa.String(length=80), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("customer_name", sa.String(length=150), nullable=True),
        sa.Column("dcs_name", sa.String(length=150), nullable=True),
        sa.Column("lender_name", sa.String(length=150), nullable=True),
        sa.Column("loan_account_number", sa.String(length=40), nullable=True),
        sa.Column("preferred_language", sa.String(length=15), nullable=True),
        sa.Column("overdue_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_outstanding", sa.Numeric(12, 2), nullable=True),
        sa.Column("minimum_payable", sa.Numeric(12, 2), nullable=True),
        sa.Column("penal_charges", sa.Numeric(12, 2), nullable=True),
        sa.Column("days_overdue", sa.Integer(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("previous_promise_date", sa.Date(), nullable=True),
        sa.Column("partial_payment_allowed", sa.Boolean(), nullable=True),
        sa.Column("payment_methods", sa.JSON(), nullable=True),
        sa.Column("secure_payment_link_available", sa.Boolean(), nullable=True),
        sa.Column("active_offers", sa.JSON(), nullable=True),
        sa.Column("offer_terms", sa.Text(), nullable=True),
        sa.Column("credit_reporting_status", sa.String(length=120), nullable=True),
        sa.Column("callback_number", sa.String(length=20), nullable=True),
        sa.Column("grievance_contact", sa.String(length=150), nullable=True),
        sa.Column("payment_status", sa.String(length=20),
                  nullable=False, server_default="pending"),
        sa.Column("customer_verified", sa.Boolean(),
                  nullable=False, server_default=sa.text("0")),
        sa.Column("recording_notice_required", sa.Boolean(),
                  nullable=False, server_default=sa.text("1")),
        sa.Column("complaint_pending", sa.Boolean(),
                  nullable=False, server_default=sa.text("0")),
        sa.Column("account_disputed", sa.Boolean(),
                  nullable=False, server_default=sa.text("0")),
        sa.Column("callback_requested", sa.Boolean(),
                  nullable=False, server_default=sa.text("0")),
        sa.Column("callback_requested_at", sa.DateTime(), nullable=True),
        sa.Column("last_call_id", sa.String(length=64), nullable=True),
        sa.Column("last_disposition", sa.String(length=40), nullable=True),
        sa.Column("is_final_transcript", sa.Boolean(),
                  nullable=False, server_default=sa.text("0")),
        sa.Column("interruption_detected", sa.Boolean(),
                  nullable=False, server_default=sa.text("0")),
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
    op.create_index("ix_customer_contexts_bot_phone", "customer_contexts",
                    ["bot_id", "phone"])
    op.create_index("ix_customer_contexts_tenant_bot", "customer_contexts",
                    ["tenant_id", "bot_id"])
    op.create_index("ix_customer_contexts_tenant_id", "customer_contexts",
                    ["tenant_id"])
    op.create_index("ix_customer_contexts_bot_id", "customer_contexts",
                    ["bot_id"])

    op.add_column(
        "conversation_sessions",
        sa.Column("disposition", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_sessions", "disposition")
    op.drop_index("ix_customer_contexts_bot_id", table_name="customer_contexts")
    op.drop_index("ix_customer_contexts_tenant_id", table_name="customer_contexts")
    op.drop_index("ix_customer_contexts_tenant_bot", table_name="customer_contexts")
    op.drop_index("ix_customer_contexts_bot_phone", table_name="customer_contexts")
    op.drop_table("customer_contexts")
