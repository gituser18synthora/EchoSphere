"""Monitoring history/incident lifecycle against disposable SQLite, no service calls."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.core import monitoring as mon
from shared.models import AuditLog, Base, MonitoringCollectorLease, MonitoringSample, MonitoringServiceState, PlatformAlert

NOW = datetime(2026, 11, 13, 12)


@pytest.fixture()
def database():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False, autoflush=False)
    engine.dispose()


def metric(status="good", **extra):
    return {"key": "redis", "name": "Redis", "status": status, "value": "Up" if status == "good" else "Unavailable",
            "target": "localhost:6379", "group": "platform", "detail": "Checked dependency", "spark": [], "latencyMs": 1.25, **extra}


def collect(factory, status, offset=0, **kwargs):
    when = NOW + timedelta(seconds=offset)
    owner = f"owner-{offset}"
    with factory() as db:
        assert mon.claim_collection(db, owner, now=when)
    with factory() as db:
        assert mon.persist_collection(db, [metric(status)], owner, now=when, **kwargs)


def test_lease_is_exclusive_and_fences_expired_writer(database):
    with database() as db:
        assert mon.claim_collection(db, "first", now=NOW)
    with database() as db:
        assert not mon.claim_collection(db, "second", now=NOW)
    after = NOW + timedelta(seconds=mon.LEASE_SECONDS + 1)
    with database() as db:
        assert mon.claim_collection(db, "second", now=after)
    with database() as db:
        assert not mon.persist_collection(db, [metric("critical")], "first", now=after)
        assert db.scalar(select(func.count()).select_from(MonitoringSample)) == 0
    with database() as db:
        assert mon.persist_collection(db, [metric()], "second", now=after)
        assert not mon.persist_collection(db, [metric()], "second", now=after)
        assert not mon.claim_collection(db, "third", now=after)


def test_fractional_lease_deadline_is_not_rounded_to_next_second(database):
    from sqlalchemy.dialects import mysql
    when = NOW.replace(microsecond=900000)
    lease_column = MonitoringCollectorLease.__table__.c.lease_until
    assert lease_column.type.compile(dialect=mysql.dialect()) == "DATETIME(6)"
    with database() as db:
        assert mon.claim_collection(db, "first", now=when)
        assert mon.persist_collection(db, [metric()], "first", now=when)
        assert mon.claim_collection(db, "next", now=when + timedelta(seconds=30, microseconds=10000))


def test_threshold_deduplicates_alerts_escalates_and_auto_resolves(database):
    collect(database, "warning")
    with database() as db:
        assert db.scalar(select(func.count()).select_from(PlatformAlert)) == 0
    collect(database, "warning", 30)
    collect(database, "critical", 60)
    collect(database, "critical", 90)
    with database() as db:
        rows = db.scalars(select(PlatformAlert)).all()
        assert len(rows) == 1 and rows[0].status == "open" and rows[0].severity == "critical"
        assert rows[0].service_key == "redis"
        assert len(db.scalars(select(AuditLog)).all()) == 2
        rows[0].status = "acknowledged"
        db.commit()
    collect(database, "good", 120)
    with database() as db:
        alert = db.scalar(select(PlatformAlert))
        assert alert.status == "resolved" and alert.resolved_at == NOW + timedelta(seconds=120)
        assert alert.resolved_by is None
        assert db.get(MonitoringServiceState, "redis").alert_id is None
        assert db.scalars(select(AuditLog).where(AuditLog.action == "Alert resolved")).one().new_value["reason"] == "Observed service recovery"
    collect(database, "critical", 150)
    collect(database, "critical", 180)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(PlatformAlert)) == 2


def test_human_resolution_is_not_undone_by_same_outage(database):
    collect(database, "critical", failure_threshold=1)
    with database() as db:
        db.scalar(select(PlatformAlert)).status = "resolved"
        db.commit()
    collect(database, "critical", 30, failure_threshold=1)
    with database() as db:
        assert db.scalars(select(PlatformAlert)).one().status == "resolved"
    collect(database, "good", 60)
    collect(database, "critical", 90, failure_threshold=1)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(PlatformAlert)) == 2


@pytest.mark.parametrize("group", ["ai", "telephony", "platform"])
def test_automatic_alert_scope_matches_health_group(database, group):
    with database() as db:
        assert mon.claim_collection(db, "scope", now=NOW)
        assert mon.persist_collection(db, [metric("critical", group=group)], "scope", now=NOW, failure_threshold=1)
        assert db.scalars(select(PlatformAlert)).one().scope == group


def test_unknown_and_observation_gaps_do_not_count_as_consecutive_failures(database):
    collect(database, "warning")
    collect(database, "warning", 600)
    collect(database, "neutral", 630)
    collect(database, "critical", 660)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(PlatformAlert)) == 0
    collect(database, "critical", 690)
    with database() as db:
        assert db.scalars(select(PlatformAlert)).one().status == "open"
    collect(database, "neutral", 720)
    with database() as db:
        assert db.scalars(select(PlatformAlert)).one().status == "open"


def test_history_is_real_bounded_keeps_failure_in_bucket_and_reports_observed_uptime(database):
    with database() as db:
        for i in range(300):
            db.add(MonitoringSample(id=f"sample-{i}", service_key="redis", checked_at=NOW - timedelta(minutes=299-i),
                                   status="critical" if i == 100 else "good", latency_ms=float(i)))
        db.add(MonitoringSample(id="future", service_key="redis", checked_at=NOW + timedelta(minutes=1), status="critical"))
        db.add(MonitoringSample(id="old", service_key="redis", checked_at=NOW - timedelta(days=2), status="critical"))
        db.add(MonitoringSample(id="unrelated", service_key="mysql", checked_at=NOW, status="critical"))
        db.commit()
        data = mon.history_for_metrics(db, [metric()], now=NOW)[0]
        assert data["sampleCount"] == 300
        assert data["uptimePercent"] == 99.67
        assert 1 < len(data["history"]) <= 180
        assert any(point["status"] == "critical" for point in data["history"])
        assert data["collectedAt"] == "2026-11-13T12:00:00Z"
        # A service with no recorded samples never receives fabricated graph points.
        empty = mon.history_for_metrics(db, [metric(key="new-service")], now=NOW)[0]
        assert empty["history"] == [] and empty["sampleCount"] == 0 and empty["uptimePercent"] is None


def test_retention_only_prunes_monitoring_samples(database):
    with database() as db:
        db.add(MonitoringSample(id="old", service_key="redis", checked_at=NOW - timedelta(hours=49), status="good"))
        db.add(MonitoringSample(id="keep", service_key="redis", checked_at=NOW - timedelta(hours=47), status="good"))
        db.add(PlatformAlert(id="existing", title="Existing alert"))
        db.commit()
    collect(database, "good")
    with database() as db:
        assert db.get(MonitoringSample, "old") is None
        assert db.get(MonitoringSample, "keep") is not None
        assert db.get(PlatformAlert, "existing") is not None


def test_unknown_samples_are_separate_from_rated_availability(database):
    with database() as db:
        for i, status in enumerate(("good", "neutral", "neutral")):
            db.add(MonitoringSample(id=f"m{i}", service_key="redis", checked_at=NOW, status=status))
        db.commit()
        result = mon.history_for_metrics(db, [metric()], now=NOW)[0]
        assert result["sampleCount"] == 3 and result["knownSampleCount"] == 1
        assert result["unknownSampleCount"] == 2 and result["uptimePercent"] == 100


async def test_history_database_error_preserves_fresh_probe_and_reports_unavailable(monkeypatch):
    from sqlalchemy.exc import OperationalError
    def broken():
        raise OperationalError("unavailable", {}, Exception("offline"))
    monkeypatch.setattr(mon, "get_sessionmaker", broken)
    data = await mon.add_recorded_history([metric("critical")])
    assert data[0]["status"] == "critical"
    assert data[0]["historyAvailable"] is False and data[0]["historyError"]
    assert data[0]["history"] == []


async def test_worker_collects_once_without_browser_and_contender_does_not_probe(database, monkeypatch):
    from backend.workers import monitoring as worker
    from unittest.mock import AsyncMock
    monkeypatch.setattr(worker, "get_sessionmaker", lambda: database)
    probe = AsyncMock(return_value=[metric("critical")])
    monkeypatch.setattr(worker, "probe_services", probe)
    assert await worker.collect_once()
    assert not await worker.collect_once()
    assert probe.await_count == 1
    probe.assert_awaited_once_with(in_process_api=False)
    with database() as db:
        assert db.scalar(select(func.count()).select_from(MonitoringSample)) == 1


async def test_dedicated_collector_survives_mongo_outage_and_cleans_up(monkeypatch):
    from backend.workers import monitoring as worker
    from shared.db.mongo import Mongo
    from unittest.mock import AsyncMock
    monkeypatch.setattr("shared.config.validate_settings", lambda *args: None)
    connect = AsyncMock(side_effect=ConnectionError("offline"))
    disconnect, run = AsyncMock(), AsyncMock()
    monkeypatch.setattr(Mongo, "connect", connect)
    monkeypatch.setattr(Mongo, "disconnect", disconnect)
    monkeypatch.setattr(worker, "run_worker", run)
    await worker._main()
    assert run.await_args.kwargs["in_process_api"] is False
    disconnect.assert_awaited_once()


def test_monitoring_migration_up_and_down_preserves_existing_alerts():
    path = Path(__file__).resolve().parents[2] / "backend/alembic/versions/6fa4d8c2b031_monitoring_history.py"
    spec = importlib.util.spec_from_file_location("monitoring_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE platform_alerts (id VARCHAR(40) PRIMARY KEY, title VARCHAR(300))")
        conn.exec_driver_sql("INSERT INTO platform_alerts (id, title) VALUES ('existing', 'Keep this alert')")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert "monitoring_samples" in inspect(conn).get_table_names()
            assert "acknowledged_at" in {col["name"] for col in inspect(conn).get_columns("platform_alerts")}
            precision_path = path.with_name("70b9ec4d2f11_monitoring_clock_precision.py")
            precision_spec = importlib.util.spec_from_file_location("monitoring_precision", precision_path)
            precision = importlib.util.module_from_spec(precision_spec)
            precision_spec.loader.exec_module(precision)
            precision.upgrade()
            precision.downgrade()
            migration.downgrade()
        assert "monitoring_samples" not in inspect(conn).get_table_names()
        assert conn.exec_driver_sql("SELECT title FROM platform_alerts").scalar() == "Keep this alert"
    engine.dispose()
