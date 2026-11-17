"""Store valid multilingual prompts without MySQL TEXT byte-limit failures.

Revision ID: 71ca3e5f7b92
Revises: 70b9ec4d2f11

Local/Demo databases may already have larger columns. Inspect both columns
before making changes, preserve their attributes, and never narrow capacity.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT

revision = "71ca3e5f7b92"
down_revision = "70b9ec4d2f11"
branch_labels = None
depends_on = None

_COLUMNS = ("full_prompt", "compiled_prompt")


def _changes(bind):
    """Preflight the entire change before MySQL's non-transactional DDL."""
    columns = {c["name"]: c for c in sa.inspect(bind).get_columns("prompt_versions")}
    attributes = {row["COLUMN_NAME"]: row for row in bind.execute(sa.text(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_SET_NAME, COLLATION_NAME "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='prompt_versions' "
        "AND COLUMN_NAME IN ('full_prompt','compiled_prompt')"
    )).mappings()}
    changes = []
    for name in _COLUMNS:
        if name not in columns or name not in attributes:
            raise RuntimeError(f"Missing prompt_versions.{name}; inspect schema before migrating")
        column, attrs = columns[name], attributes[name]
        dtype = attrs["DATA_TYPE"].lower()
        if dtype in {"mediumtext", "longtext"}:
            continue
        if dtype != "text":
            raise RuntimeError(f"Unexpected type {dtype} for prompt_versions.{name}; no changes applied")
        changes.append((name, {
            "existing_type": column["type"],
            "type_": MEDIUMTEXT(charset=attrs["CHARACTER_SET_NAME"], collation=attrs["COLLATION_NAME"]),
            "existing_nullable": column["nullable"],
            "existing_server_default": column["default"],
            "existing_comment": column.get("comment"),
        }))
    return changes


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name not in {"mysql", "mariadb"}:
        return  # PostgreSQL/SQLite TEXT already support the accepted prompts.
    if op.get_context().as_sql:
        raise RuntimeError("Run this migration online to preserve existing prompt column attributes and larger types")
    for name, kwargs in _changes(bind):
        op.alter_column("prompt_versions", name, **kwargs)


def downgrade():
    if op.get_bind().dialect.name in {"mysql", "mariadb"}:
        raise RuntimeError(
            "Prompt capacity is not reduced automatically: it could truncate data "
            "or undo a pre-existing larger column. Review any shrink separately."
        )
