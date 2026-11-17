"""Exercise prompt widening and failure guards on disposable local MySQL."""
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DataError

from shared.db.mysql import get_engine
from shared.orchestration.prompt_compiler import compile_source
from tests.unit.test_prompt_text_capacity import migration

pytestmark = pytest.mark.integration


def attributes(conn):
    return [dict(r) for r in conn.exec_driver_sql(
        "SELECT COLUMN_NAME,DATA_TYPE,IS_NULLABLE,COLUMN_DEFAULT,CHARACTER_SET_NAME,COLLATION_NAME,COLUMN_COMMENT "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME='prompt_versions' AND COLUMN_NAME IN ('full_prompt','compiled_prompt') ORDER BY COLUMN_NAME"
    ).mappings()]


def test_valid_multibyte_prompt_survives_migration_with_attributes_and_existing_data(disposable_mysql_database):
    module = migration()
    with get_engine().connect() as conn:
        conn.exec_driver_sql("SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
        conn.exec_driver_sql("CREATE TABLE prompt_versions (id INTEGER PRIMARY KEY, "
            "full_prompt TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL COMMENT 'Author source', "
            "compiled_prompt TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'Runtime prompt')")
        conn.exec_driver_sql("INSERT INTO prompt_versions VALUES (1,'Existing source','Existing runtime'),(2,NULL,'Keep null source')")
        conn.commit()
        before = attributes(conn)
        prompt = "अ" * 35_000
        errors, compiled = compile_source("full", full_prompt=prompt)
        assert not errors and len(compiled.encode("utf-8")) > 65_535
        with pytest.raises(DataError):
            conn.execute(text("INSERT INTO prompt_versions VALUES (3,:value,:value)"), {"value":compiled})
        conn.rollback()
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        after = attributes(conn)
        assert after == [{**row, "DATA_TYPE":"mediumtext"} for row in before]
        assert list(conn.exec_driver_sql("SELECT * FROM prompt_versions ORDER BY id")) == [
            (1,"Existing source","Existing runtime"),(2,None,"Keep null source")]
        conn.execute(text("INSERT INTO prompt_versions VALUES (3,:value,:value)"), {"value":compiled})
        conn.commit()
        assert conn.exec_driver_sql("SELECT full_prompt,compiled_prompt FROM prompt_versions WHERE id=3").one() == (compiled,compiled)
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()  # already-widened databases are unchanged
            with pytest.raises(RuntimeError, match="not reduced automatically"):
                module.downgrade()
        assert attributes(conn) == after


def test_preexisting_larger_columns_are_never_narrowed(disposable_mysql_database):
    module = migration()
    with get_engine().connect() as conn:
        conn.exec_driver_sql("CREATE TABLE prompt_versions (full_prompt MEDIUMTEXT, compiled_prompt LONGTEXT)")
        conn.execute(text("INSERT INTO prompt_versions VALUES (:v,:v)"), {"v":"x" * 70_000})
        conn.commit()
        before = conn.exec_driver_sql("SHOW CREATE TABLE prompt_versions").one()[1]
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
        assert conn.exec_driver_sql("SHOW CREATE TABLE prompt_versions").one()[1] == before
        assert conn.exec_driver_sql("SELECT LENGTH(full_prompt) FROM prompt_versions").scalar() == 70_000


def test_unexpected_second_type_fails_before_either_column_changes(disposable_mysql_database):
    module = migration()
    with get_engine().connect() as conn:
        conn.exec_driver_sql("CREATE TABLE prompt_versions (full_prompt TEXT, compiled_prompt VARCHAR(100))")
        before = conn.exec_driver_sql("SHOW CREATE TABLE prompt_versions").one()[1]
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="Unexpected type varchar"):
                module.upgrade()
        assert conn.exec_driver_sql("SHOW CREATE TABLE prompt_versions").one()[1] == before
