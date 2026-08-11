"""Compliance policies + bot-level guardrail profile override.

Adds the versioned ``compliance_policies`` table (calling windows, contact
limits, prohibited conduct, waiver rules — enforced deterministically only
while ``status='active'`` and effective) with its immutable
``compliance_wordings`` legal-template table, and a nullable
``voice_bots.guardrail_profile_id`` — NULL keeps the pre-existing behavior
(bot inherits the tenant's default profile), so every existing bot continues
inheriting after this migration with no backfill required.

Revision ID: c8e0a2b4d6f8
Revises: b6d8f0a2c4e6
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "c8e0a2b4d6f8"
down_revision = "b6d8f0a2c4e6"
branch_labels = None
depends_on = None

ID_LEN = 40


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(ID_LEN), nullable=True),
        sa.Column("updated_by", sa.String(ID_LEN), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "compliance_policies",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("tenant_id", sa.String(ID_LEN),
                  sa.ForeignKey("tenants.id", name="fk_compliance_policy_tenant"),
                  nullable=True),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("jurisdiction", sa.String(10), nullable=True),
        sa.Column("regulator", sa.String(40), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("effective_date", sa.Date, nullable=True),
        sa.Column("applies_to", sa.JSON, nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("calling_windows", sa.JSON, nullable=True),
        sa.Column("contact_limits", sa.JSON, nullable=True),
        sa.Column("prohibited_conduct", sa.JSON, nullable=True),
        sa.Column("waiver_rules", sa.JSON, nullable=True),
        sa.Column("escalation_rules", sa.JSON, nullable=True),
        sa.Column("sources", sa.JSON, nullable=True),
        sa.Column("approved_by", sa.String(ID_LEN), nullable=True),
        sa.Column("approved_at", sa.DateTime, nullable=True),
        sa.Column("approval_note", sa.String(500), nullable=True),
        *_audit_columns(),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(ID_LEN), nullable=True),
        sa.UniqueConstraint("tenant_id", "code", "version", name="uq_policy_code_version"),
    )
    op.create_index("ix_compliance_policies_tenant_status",
                    "compliance_policies", ["tenant_id", "status"])

    op.create_table(
        "compliance_wordings",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("policy_id", sa.String(ID_LEN),
                  sa.ForeignKey("compliance_policies.id", name="fk_wording_policy"),
                  nullable=False),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("language", sa.String(15), nullable=False, server_default="en"),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("exact", sa.Boolean, nullable=False, server_default=sa.text("1")),
        *_audit_columns(),
        sa.UniqueConstraint("policy_id", "code", "language", "version",
                            name="uq_wording_version"),
    )
    op.create_index("ix_compliance_wordings_policy_id",
                    "compliance_wordings", ["policy_id"])

    op.add_column(
        "voice_bots",
        sa.Column("guardrail_profile_id", sa.String(ID_LEN), nullable=True),
    )

    # Trigger ledger: which compliance policy (and version) produced a hit,
    # and what actually happened (blocked / redacted / flagged / emitted /
    # rescheduled / escalated).
    op.add_column("guardrail_triggers",
                  sa.Column("policy_code", sa.String(60), nullable=True))
    op.add_column("guardrail_triggers",
                  sa.Column("policy_version", sa.Integer, nullable=True))
    op.add_column("guardrail_triggers",
                  sa.Column("outcome", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("guardrail_triggers", "outcome")
    op.drop_column("guardrail_triggers", "policy_version")
    op.drop_column("guardrail_triggers", "policy_code")
    op.drop_column("voice_bots", "guardrail_profile_id")
    op.drop_index("ix_compliance_wordings_policy_id", table_name="compliance_wordings")
    op.drop_table("compliance_wordings")
    op.drop_index("ix_compliance_policies_tenant_status", table_name="compliance_policies")
    op.drop_table("compliance_policies")
