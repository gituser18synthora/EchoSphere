"""Release pinning end-to-end on a SQLite control plane: save → revision →
publish pins → later edit → the published release keeps its revision; and
every degraded state (no migration, no revision rows, no pins, no behaviour
declaration) keeps running the latest save exactly as before."""
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

import shared.db.mysql as mysql
import shared.db.schema_features as features
import shared.orchestration.workflow_engine as we
from backend.routers.releases import _pin_workflows
from backend.routers.workflows import _snapshot_revision
from shared.bot_config import _published_workflow_pins
from shared.models import Release, VoiceBot, Workflow, WorkflowRevision
from shared.orchestration.behavior import resolve_behavior

NODES_V1 = [{"id": "n_start", "kind": "start", "config": {}},
            {"id": "n_ask", "kind": "ask", "config": {"question": "City?", "variable": "city", "entityType": "text"}},
            {"id": "n_end", "kind": "end", "config": {}}]
EDGES = [{"id": "e0", "from": "n_start", "to": "n_ask"}, {"id": "e1", "from": "n_ask", "to": "n_end"}]
NODES_V2 = [dict(NODES_V1[0]), {**NODES_V1[1], "config": {**NODES_V1[1]["config"], "question": "Which city?"}}, dict(NODES_V1[2])]


class _User:
    id = "usr_test"


@pytest.fixture()
def plane(monkeypatch):
    """SQLite with the tables the pinning path touches; migration applied."""
    engine = sa.create_engine("sqlite://", connect_args={"check_same_thread": False},
                              poolclass=sa.pool.StaticPool)
    from shared.models.base import Base

    Base.metadata.create_all(engine)  # full schema incl. the migration's table/column
    maker = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(mysql, "get_engine", lambda: engine)
    monkeypatch.setattr(mysql, "get_sessionmaker", lambda: maker)
    monkeypatch.setattr(features, "get_engine", lambda: engine)  # bound at import
    features.reset()
    we._definition_cache.clear()
    yield maker
    features.reset()
    we._definition_cache.clear()


def _seed(maker):
    with maker() as s:
        bot = VoiceBot(id="bot_p", tenant_id="tn_p", name="Pin Bot", status="published", use_case="x")
        wf = Workflow(id="wf_p", tenant_id="tn_p", bot_id="bot_p", name="pin flow", version=1,
                      status="draft", nodes=NODES_V1, edges=EDGES)
        s.add_all([bot, wf])
        s.commit()
    return "bot_p", "wf_p"


def _load(pins):
    return we.load_workflow_definition("tn_p", "bot_p", "pin flow", pins)


class TestEndToEnd:
    def test_save_publish_edit_keeps_the_release_on_its_revision(self, plane):
        bot_id, wf_id = _seed(plane)
        with plane() as s:
            wf = s.get(Workflow, wf_id)
            _snapshot_revision(s, wf, _User())          # save #1 → revision v1
            s.commit()
            assert s.scalar(sa.select(sa.func.count()).select_from(WorkflowRevision)) == 1
            release = Release(id="rel_1", tenant_id="tn_p", bot_id=bot_id, version="v1.0", stage="review")
            s.add(release)
            _pin_workflows(s, release, s.get(VoiceBot, bot_id))   # publish
            release.stage = "published"
            release.published_at = sa.func.now()
            s.commit()
            assert release.pinned_workflows == {wf_id: 1}
            pins = _published_workflow_pins(s, s.get(VoiceBot, bot_id))
        assert pins == {wf_id: 1}

        # Pinned version == latest save: the live row runs, nothing is flagged.
        definition = _load(pins)
        assert definition["version"] == 1 and not definition.get("pinned") and not definition.get("pinMissing")

        # Later edit → v2 saved and snapshotted; the release still pins v1.
        with plane() as s:
            wf = s.get(Workflow, wf_id)
            wf.nodes, wf.version = NODES_V2, 2
            _snapshot_revision(s, wf, _User())
            s.commit()
        we._definition_cache.clear()
        pinned = _load(pins)
        assert pinned["version"] == 1 and pinned["pinned"] is True
        assert pinned["nodes"][1]["config"]["question"] == "City?"
        latest = _load(None)
        assert latest["version"] == 2 and latest["nodes"][1]["config"]["question"] == "Which city?"

        # A new publish re-pins to the current save.
        with plane() as s:
            release = Release(id="rel_2", tenant_id="tn_p", bot_id=bot_id, version="v1.1", stage="review")
            s.add(release)
            _pin_workflows(s, release, s.get(VoiceBot, bot_id))
            s.commit()
            assert release.pinned_workflows == {wf_id: 2}


class TestBackwardCompatibility:
    def test_pin_without_backfilled_revision_runs_latest_and_flags_it(self, plane):
        _, wf_id = _seed(plane)
        with plane() as s:
            wf = s.get(Workflow, wf_id)
            wf.nodes, wf.version = NODES_V2, 3   # saved before revisions existed: no snapshots
            s.commit()
        definition = _load({wf_id: 2})
        assert definition["version"] == 3 and definition["pinMissing"] == 2 and not definition.get("pinned")

    def test_empty_pins_and_release_without_pins_mean_latest(self, plane):
        bot_id, wf_id = _seed(plane)
        with plane() as s:
            s.add(Release(id="rel_old", tenant_id="tn_p", bot_id=bot_id, version="v0.9",
                          stage="published", published_at=sa.func.now()))
            s.commit()
            assert _published_workflow_pins(s, s.get(VoiceBot, bot_id)) == {}
        assert _load({})["version"] == 1 and _load(None)["version"] == 1

    def test_unpublished_bot_has_no_pins(self, plane):
        bot_id, _ = _seed(plane)
        with plane() as s:
            bot = s.get(VoiceBot, bot_id)
            bot.status = "draft"
            s.commit()
            assert _published_workflow_pins(s, bot) == {}

    def test_schema_without_migration_is_a_no_op_everywhere(self, plane, monkeypatch):
        bot_id, wf_id = _seed(plane)
        monkeypatch.setattr(features, "table_exists", lambda t: False)
        monkeypatch.setattr(features, "column_exists", lambda t, c: False)
        with plane() as s:
            wf = s.get(Workflow, wf_id)
            _snapshot_revision(s, wf, _User())
            release = Release(id="rel_x", tenant_id="tn_p", bot_id=bot_id, version="v1", stage="review")
            s.add(release)
            _pin_workflows(s, release, s.get(VoiceBot, bot_id))
            s.commit()
            assert s.scalar(sa.select(sa.func.count()).select_from(WorkflowRevision)) == 0
            assert _published_workflow_pins(s, s.get(VoiceBot, bot_id)) == {}
        assert _load({wf_id: 1})["version"] == 1 and _load({wf_id: 5})["pinMissing"] == 5

    def test_undeclared_definition_runs_behaviour_v1_and_snapshot_records_it(self, plane):
        _, wf_id = _seed(plane)
        with plane() as s:
            wf = s.get(Workflow, wf_id)
            _snapshot_revision(s, wf, _User())
            s.commit()
            rev = s.scalar(sa.select(WorkflowRevision))
            assert rev.behavior_version == 1
        assert resolve_behavior(_load(None)).version == 1
