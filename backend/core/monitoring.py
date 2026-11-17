"""Recorded health samples and automatic incident lifecycle.

Only the collector writes observations. Dashboard reads never invent history.
A database lease and fencing token coordinate multiple API/worker processes.
"""

import asyncio
import logging
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import MonitoringCollectorLease, MonitoringSample, MonitoringServiceState, PlatformAlert

logger = logging.getLogger(__name__)
LEASE_ID = "health-collector"
LEASE_SECONDS = 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def service_key(metric: dict) -> str:
    return str(metric.get("key") or re.sub(r"[^a-z0-9]+", "-", metric["name"].lower()).strip("-"))[:96]


def claim_collection(db: Session, owner: str, *, now: datetime | None = None) -> bool:
    """Claim at most one collection per interval, even with concurrent workers."""
    now = now or utcnow()
    if db.get(MonitoringCollectorLease, LEASE_ID) is None:
        db.add(MonitoringCollectorLease(id=LEASE_ID, lease_until=now))
        try:
            db.flush()
        except IntegrityError:
            # Another worker created the singleton. Retry the atomic claim.
            db.rollback()
    result = db.execute(update(MonitoringCollectorLease).where(
        MonitoringCollectorLease.id == LEASE_ID,
        MonitoringCollectorLease.lease_until <= now,
    ).values(owner=owner, lease_until=now + timedelta(seconds=LEASE_SECONDS)))
    db.commit()
    return result.rowcount == 1


def _alert_transition(db: Session, state: MonitoringServiceState, metric: dict, now: datetime, threshold: int) -> None:
    status = metric["status"]
    alert = db.scalar(select(PlatformAlert).where(PlatformAlert.id == state.alert_id).with_for_update()) if state.alert_id else None
    if status == "good":
        state.failure_count = 0
        if alert is not None and not alert.is_deleted and alert.status != "resolved":
            before = {"status": alert.status, "severity": alert.severity}
            alert.status, alert.resolved_at, alert.resolved_by = "resolved", now, None
            record_audit(db, user=None, action="Alert resolved", entity_type="alert", entity_id=alert.id,
                target_label=alert.title, previous_value=before,
                new_value={"status": "resolved", "reason": "Observed service recovery", "serviceKey": state.service_key})
        state.alert_id = None
        return
    if status not in ("warning", "serious", "critical"):
        # Unknown/not observed is not evidence of recovery or an outage.
        state.failure_count = 0
        return
    state.failure_count += 1
    if state.failure_count < max(1, threshold):
        return
    severity = "critical" if status in ("critical", "serious") else "warning"
    if state.alert_id is None:
        alert = PlatformAlert(id=new_id("al"), severity=severity,
            title=f"{metric['name']}: {metric['value']}"[:300], source="Health monitor",
            scope=metric.get("group") if metric.get("group") in ("platform", "ai", "telephony") else "platform",
            status="open", occurred_at=now, service_key=state.service_key)
        db.add(alert)
        state.alert_id = alert.id
        record_audit(db, user=None, action="Alert opened", entity_type="alert", entity_id=alert.id,
            target_label=alert.title, new_value={"status": "open", "severity": severity,
                "serviceKey": state.service_key, "reason": "Consecutive failed health observations"})
    elif alert is not None and not alert.is_deleted and alert.status != "resolved" and alert.severity != severity:
        before = {"severity": alert.severity, "status": alert.status}
        alert.severity = severity
        record_audit(db, user=None, action="Alert severity updated", entity_type="alert", entity_id=alert.id,
            target_label=alert.title, previous_value=before, new_value={"severity": severity, "status": alert.status})
    # A human-resolved incident stays resolved until a healthy observation.
    # Repeated failures must not undo that decision every thirty seconds.


def persist_collection(db: Session, metrics: list[dict], owner: str, *, now: datetime | None = None,
                       poll_seconds: float = 30, retention_hours: int = 48, failure_threshold: int = 2) -> bool:
    now = now or utcnow()
    lease = db.scalar(select(MonitoringCollectorLease).where(MonitoringCollectorLease.id == LEASE_ID).with_for_update())
    if lease is None or lease.owner != owner or lease.lease_until <= now:
        db.rollback()
        return False
    for metric in metrics:
        key = service_key(metric)
        latency = metric.get("latencyMs")
        if not isinstance(latency, (float, int)) or not math.isfinite(latency) or latency < 0:
            latency = None
        db.add(MonitoringSample(id=new_id("hm"), service_key=key, checked_at=now,
            status=metric["status"], latency_ms=latency))
        state = db.get(MonitoringServiceState, key)
        if state is None:
            state = MonitoringServiceState(service_key=key, last_status=metric["status"], failure_count=0, last_seen_at=now)
            db.add(state)
        elif (now - state.last_seen_at).total_seconds() > max(90, poll_seconds * 3):
            state.failure_count = 0
        _alert_transition(db, state, metric, now, failure_threshold)
        state.last_status, state.last_seen_at = metric["status"], now
    db.execute(delete(MonitoringSample).where(MonitoringSample.checked_at < now - timedelta(hours=max(24, retention_hours))))
    lease.last_success_at = now
    lease.owner = None
    lease.lease_until = now + timedelta(seconds=max(10, poll_seconds))
    db.commit()
    return True


def history_for_metrics(db: Session, metrics: list[dict], *, hours: int = 24, now: datetime | None = None) -> list[dict]:
    """Return real observations, compacted into at most 180 time buckets per service."""
    now = now or utcnow()
    hours = min(24, max(1, hours))
    since = now - timedelta(hours=hours)
    keys = [service_key(metric) for metric in metrics]
    samples = db.execute(select(MonitoringSample.service_key, MonitoringSample.checked_at,
        MonitoringSample.status, MonitoringSample.latency_ms).where(
        MonitoringSample.service_key.in_(keys), MonitoringSample.checked_at >= since,
        MonitoringSample.checked_at <= now).order_by(MonitoringSample.checked_at)).all()
    grouped = defaultdict(list)
    for row in samples:
        grouped[row.service_key].append(row)
    result = []
    rank = {"neutral": -1, "good": 0, "warning": 1, "serious": 2, "critical": 3}
    bucket_seconds = hours * 3600 / 180
    for metric in metrics:
        rows = grouped[service_key(metric)]
        buckets = {}
        for row in rows:
            bucket = min(179, int((row.checked_at - since).total_seconds() / bucket_seconds))
            old = buckets.get(bucket)
            if old is None or rank.get(row.status, 1) >= rank.get(old.status, 1):
                buckets[bucket] = row
        history = [{"time": iso(row.checked_at), "status": row.status, "latencyMs": row.latency_ms}
                   for row in buckets.values()]
        known = [row for row in rows if row.status in ("good", "warning", "serious", "critical")]
        result.append({**metric, "key": service_key(metric), "historyAvailable": True,
            "historyWindowHours": hours, "sampleCount": len(rows),
            "knownSampleCount": len(known), "unknownSampleCount": len(rows) - len(known),
            "uptimePercent": round(sum(row.status == "good" for row in known) / len(known) * 100, 2) if known else None,
            "history": history, "collectedAt": iso(rows[-1].checked_at) if rows else None})
    return result


async def add_recorded_history(metrics: list[dict], *, hours: int = 24) -> list[dict]:
    def read():
        with get_sessionmaker()() as db:
            return history_for_metrics(db, metrics, hours=hours)
    try:
        return await asyncio.to_thread(read)
    except SQLAlchemyError as exc:
        logger.warning("Monitoring history unavailable (%s)", type(exc).__name__)
        return [{**metric, "historyAvailable": False, "historyError": "Recorded history is temporarily unavailable.",
                 "historyWindowHours": hours, "sampleCount": 0, "knownSampleCount": 0,
                 "unknownSampleCount": 0, "uptimePercent": None, "history": []} for metric in metrics]
