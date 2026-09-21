"""Migration a1b2c3d4e5f6 (workflow revisions + release pins): upgrade and
downgrade run cleanly on a schema shaped like the tables it touches, and the
script sits on the single Alembic head."""
import importlib.util
import pathlib

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "backend/alembic/versions/a1b2c3d4e5f6_workflow_revisions_and_release_pins.py"


def _module():
    spec = importlib.util.spec_from_file_location("mig_a1b2c3d4e5f6", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _script_directory():
    cfg = Config(str(ROOT / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "backend/alembic"))
    return ScriptDirectory.from_config(cfg)


def test_new_migration_is_the_single_head_on_top_of_the_elevenlabs_revision():
    sd = _script_directory()
    assert sd.get_heads() == ["a1b2c3d4e5f6"]
    assert sd.get_revision("a1b2c3d4e5f6").down_revision == "b5d7f9a1c3e5"
    # Both environments reach the head linearly: dev (at b5d7…) in one step,
    # live (at f3a5…) via the ElevenLabs revision first.
    assert [r.revision for r in sd.iterate_revisions("a1b2c3d4e5f6", "f3a5c7e9b1d4")] == [
        "a1b2c3d4e5f6", "b5d7f9a1c3e5"]


def test_upgrade_then_downgrade_round_trip():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("create table workflows (id varchar(40) primary key, version integer)"))
        conn.execute(sa.text("create table releases (id varchar(40) primary key, stage varchar(20))"))
    mod = _module()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.upgrade()
        insp = sa.inspect(conn)
        assert insp.has_table("workflow_revisions")
        cols = {c["name"] for c in insp.get_columns("workflow_revisions")}
        assert {"id", "workflow_id", "tenant_id", "bot_id", "version", "name", "nodes", "edges",
                "behavior_version", "created_by", "created_at", "updated_at"} <= cols
        assert "pinned_workflows" in {c["name"] for c in insp.get_columns("releases")}
        unique = [i for i in insp.get_indexes("workflow_revisions") if i["name"] == "ix_workflow_revisions_wf_version"]
        assert unique and unique[0]["unique"] and unique[0]["column_names"] == ["workflow_id", "version"]
        # existing rows untouched; new column reads NULL
        conn.execute(sa.text("insert into releases (id, stage) values ('rel_1', 'published')"))
        assert conn.execute(sa.text("select pinned_workflows from releases")).scalar() is None
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mod.downgrade()
        insp = sa.inspect(conn)
        assert not insp.has_table("workflow_revisions")
        assert "pinned_workflows" not in {c["name"] for c in insp.get_columns("releases")}
        assert conn.execute(sa.text("select count(*) from releases")).scalar() == 1
