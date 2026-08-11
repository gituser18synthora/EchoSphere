"""Guardrail profiles, profile↔rule association, trigger ledger.

Adds the ``guardrail_profiles`` catalog (Standard / Healthcare / Finance are
seeded by base_seed, not here), the normalized ``guardrail_profile_rules``
association, and the tenant-scoped ``guardrail_triggers`` enforcement ledger.
Extends ``guardrails`` with a stable ``code`` (what the runtime enforcement
registry dispatches on) and ``is_mandatory`` (platform rules that apply to
every tenant and can never be disabled), and ``tenants`` with the assigned
``guardrail_profile_id``. Existing seeded guardrail rows are backfilled with
their codes and mandatory flags by name — a no-op on fresh databases.

Revision ID: b6d8f0a2c4e6
Revises: a4c6e8b0d2f4
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op

revision = "b6d8f0a2c4e6"
down_revision = "a4c6e8b0d2f4"
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


# (name, code, is_mandatory) for rows seeded before codes existed.
_CODE_BACKFILL = [
    ("PII redaction in transcripts", "pii_redaction", True),
    ("Medical advice boundary", "medical_advice_boundary", False),
    ("Payment collection restriction", "payment_collection_restriction", False),
    ("Competitor mention flag", "competitor_mention_flag", False),
    ("Profanity / abuse de-escalation", "profanity_deescalation", False),
]


def upgrade() -> None:
    op.create_table(
        "guardrail_profiles",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        *_audit_columns(),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(ID_LEN), nullable=True),
    )
    op.create_index("ix_guardrail_profiles_status", "guardrail_profiles", ["status"])

    op.create_table(
        "guardrail_profile_rules",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column(
            "profile_id", sa.String(ID_LEN),
            sa.ForeignKey("guardrail_profiles.id", name="fk_gpr_profile_id"),
            nullable=False,
        ),
        sa.Column(
            "guardrail_id", sa.String(ID_LEN),
            sa.ForeignKey("guardrails.id", name="fk_gpr_guardrail_id"),
            nullable=False,
        ),
        *_audit_columns(),
        sa.UniqueConstraint("profile_id", "guardrail_id", name="uq_profile_guardrail"),
    )
    op.create_index("ix_guardrail_profile_rules_profile_id", "guardrail_profile_rules", ["profile_id"])
    op.create_index("ix_guardrail_profile_rules_guardrail_id", "guardrail_profile_rules", ["guardrail_id"])

    op.create_table(
        "guardrail_triggers",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("tenant_id", sa.String(ID_LEN), nullable=True),
        sa.Column("bot_id", sa.String(ID_LEN), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("guardrail_id", sa.String(ID_LEN), nullable=True),
        sa.Column("guardrail_code", sa.String(50), nullable=False),
        sa.Column("rule_name", sa.String(200), nullable=True),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("detail", sa.String(300), nullable=True),
        sa.Column("profile_id", sa.String(ID_LEN), nullable=True),
        sa.Column("profile_version", sa.Integer, nullable=True),
        sa.Column("channel", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_guardrail_triggers_tenant_time", "guardrail_triggers",
        ["tenant_id", "created_at"],
    )
    op.create_index("ix_guardrail_triggers_code", "guardrail_triggers", ["guardrail_code"])

    op.add_column("guardrails", sa.Column("code", sa.String(50), nullable=True))
    op.create_unique_constraint("uq_guardrails_code", "guardrails", ["code"])
    op.add_column(
        "guardrails",
        sa.Column("is_mandatory", sa.Boolean, nullable=False, server_default=sa.text("0")),
    )

    op.add_column(
        "tenants",
        sa.Column("guardrail_profile_id", sa.String(ID_LEN), nullable=True),
    )

    for name, code, mandatory in _CODE_BACKFILL:
        op.execute(
            sa.text(
                "UPDATE guardrails SET code = :code, is_mandatory = :mand "
                "WHERE name = :name AND code IS NULL"
            ).bindparams(code=code, mand=1 if mandatory else 0, name=name)
        )


def downgrade() -> None:
    op.drop_column("tenants", "guardrail_profile_id")
    op.drop_constraint("uq_guardrails_code", "guardrails", type_="unique")
    op.drop_column("guardrails", "is_mandatory")
    op.drop_column("guardrails", "code")
    op.drop_index("ix_guardrail_triggers_code", table_name="guardrail_triggers")
    op.drop_index("ix_guardrail_triggers_tenant_time", table_name="guardrail_triggers")
    op.drop_table("guardrail_triggers")
    op.drop_index("ix_guardrail_profile_rules_guardrail_id", table_name="guardrail_profile_rules")
    op.drop_index("ix_guardrail_profile_rules_profile_id", table_name="guardrail_profile_rules")
    op.drop_table("guardrail_profile_rules")
    op.drop_index("ix_guardrail_profiles_status", table_name="guardrail_profiles")
    op.drop_table("guardrail_profiles")
