"""Security permission overrides and durable logout revocation.

Revision ID: 5e9c3a7b1d2f
Revises: 4d8b2f6a9c1e
"""
from alembic import op
import sqlalchemy as sa

revision = "5e9c3a7b1d2f"
down_revision = "4d8b2f6a9c1e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("roles", sa.Column("permissions_customized", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"))
    op.execute(sa.text("UPDATE users SET must_change_password = true WHERE status = 'invited'"))
    op.create_table("revoked_tokens",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False))
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])


def downgrade():
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")
    op.drop_column("roles", "permissions_customized")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "auth_version")
