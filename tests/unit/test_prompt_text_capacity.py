"""Prompt storage supports the accepted character limit across SQL dialects."""
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.dialects.mysql.mariadb import MariaDBDialect

from shared.models import PromptVersion


def migration():
    path = Path(__file__).parents[2] / "backend/alembic/versions/71ca3e5f7b92_prompt_text_capacity.py"
    spec = importlib.util.spec_from_file_location("prompt_capacity_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("dialect,expected", [
    (mysql.dialect(), "MEDIUMTEXT"), (MariaDBDialect(), "MEDIUMTEXT"),
    (postgresql.dialect(), "TEXT"), (sqlite.dialect(), "TEXT"),
])
def test_new_prompt_tables_have_sufficient_capacity(dialect, expected):
    for name in ("full_prompt", "compiled_prompt"):
        assert PromptVersion.__table__.c[name].type.compile(dialect=dialect) == expected


def test_sqlite_migration_keeps_portable_text_and_existing_content():
    module = migration()
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE prompt_versions (full_prompt TEXT, compiled_prompt TEXT)")
        conn.exec_driver_sql("INSERT INTO prompt_versions VALUES ('Preserve this', NULL)")
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.downgrade()
        assert conn.exec_driver_sql("SELECT * FROM prompt_versions").one() == ("Preserve this", None)
        assert all(str(c["type"]) == "TEXT" for c in inspect(conn).get_columns("prompt_versions"))
    engine.dispose()
