"""Monitoring alert workflows against disposable in-memory SQLite only."""

from datetime import datetime, timedelta

import pytest
from fastapi import Depends, FastAPI, Header
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.deps import get_current_user
from backend.routers import platform
from shared.db.mysql import get_db
from shared.errors import ApiError, install_error_handlers
from shared.models import AuditLog, Base, PlatformAlert, Role, Tenant, User

API = "/api/v1"
WHEN = datetime(2026, 11, 13, 10)


@pytest.fixture()
def workspace():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(platform.router, prefix=API)

    def database():
        with Session(engine) as db:
            yield db

    def current_user(x_test_user: str | None = Header(None), db: Session = Depends(get_db)):
        row = db.get(User, x_test_user) if x_test_user else None
        if row is None:
            raise ApiError("Authentication required.", 401)
        return row

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = current_user
    with Session(engine) as db:
        db.add_all([Role(id="root_role", code="super_admin", name="Super Admin", scope="platform"),
                    Role(id="member_role", code="tenant_user", name="Member", scope="tenant")])
        db.add_all([Tenant(id=tenant, name=tenant, domain=tenant + ".example.test") for tenant in ("tn_a", "tn_b")])
        db.flush()
        for uid, tenant in (("root", None), ("member_a", "tn_a"), ("member_b", "tn_b")):
            db.add(User(id=uid, name=uid.title(), email=uid + "@example.test", password_hash="not-used",
                        role_id="root_role" if uid == "root" else "member_role", tenant_id=tenant))
        db.add_all([
            PlatformAlert(id="platform", title="Redis down", severity="critical", source="Redis readiness", scope="platform",
                          occurred_at=WHEN, created_at=WHEN, service_key="redis"),
            PlatformAlert(id="tenant_a", title="Tenant A warning", severity="warning", source="Gateway", scope="tenant",
                          tenant_id="tn_a", status="acknowledged", occurred_at=WHEN, created_at=WHEN),
            PlatformAlert(id="tenant_b", title="Tenant B warning", severity="warning", source="Gateway", scope="tenant",
                          tenant_id="tn_b", occurred_at=WHEN, created_at=WHEN),
            PlatformAlert(id="private_a", title="Private platform detail", severity="critical", scope="platform",
                          tenant_id="tn_a", occurred_at=WHEN, created_at=WHEN),
            PlatformAlert(id="ai", title="Provider recovered", severity="serious", source="Voice Worker", scope="ai",
                          status="resolved", occurred_at=WHEN - timedelta(days=1), created_at=WHEN),
            PlatformAlert(id="fallback", title="100% connection outage", severity="warning", source="Worker", scope="ai",
                          occurred_at=None, created_at=WHEN + timedelta(hours=1)),
            PlatformAlert(id="deleted", title="Hidden deleted alert", severity="critical", scope="tenant", tenant_id="tn_a",
                          is_deleted=True, occurred_at=WHEN, created_at=WHEN),
        ])
        db.commit()
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def headers(uid="root"):
    return {"X-Test-User": uid}


def payload(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_default_response_remains_array_and_caps_legacy_consumers(workspace):
    client, engine = workspace
    with Session(engine) as db:
        db.add_all([PlatformAlert(id=f"bulk_{i:03}", title=f"Warning {i}", occurred_at=WHEN) for i in range(110)])
        db.commit()
    response = client.get(API + "/alerts", headers=headers())
    assert isinstance(payload(response), list)
    assert len(payload(response)) == 100
    assert "meta" not in response.json()
    paged = client.get(API + "/alerts?paginated=true&pageSize=200", headers=headers())
    assert paged.json()["meta"]["total"] == 116
    assert len(payload(paged)) == 116


def test_status_counts_pagination_and_stable_fallback_time(workspace):
    client, _ = workspace
    first = client.get(API + "/alerts?paginated=true&pageSize=2", headers=headers())
    second = client.get(API + "/alerts?paginated=true&pageSize=2&page=2", headers=headers())
    assert payload(first)[0]["id"] == "fallback"
    assert not {row["id"] for row in payload(first)}.intersection(row["id"] for row in payload(second))
    assert first.json()["meta"] == {"page": 1, "pageSize": 2, "total": 6, "totalPages": 3,
                                   "counts": {"open": 4, "acknowledged": 1, "resolved": 1, "total": 6}}
    filtered = client.get(API + "/alerts?paginated=true&status=resolved", headers=headers())
    assert [row["id"] for row in payload(filtered)] == ["ai"]
    assert filtered.json()["meta"]["total"] == 1
    assert filtered.json()["meta"]["counts"]["open"] == 4


def test_alert_filters_escape_search_and_include_entire_utc_day(workspace):
    client, _ = workspace
    response = client.get(API + "/alerts", params={"paginated": "true", "severity": "warning", "scope": "ai",
        "search": "%", "dateFrom": "2026-11-13", "dateTo": "2026-11-13"}, headers=headers())
    assert [row["id"] for row in payload(response)] == ["fallback"]
    assert response.json()["meta"]["counts"] == {"open": 1, "acknowledged": 0, "resolved": 0, "total": 1}
    filtered = client.get(API + "/alerts?scope=tenant&tenantId=tn_b", headers=headers())
    assert [row["id"] for row in payload(filtered)] == ["tenant_b"]
    assert client.get(API + "/alerts?dateFrom=2026-11-14&dateTo=2026-11-13", headers=headers()).status_code == 422
    assert client.get(API + "/alerts?dateFrom=bad", headers=headers()).status_code == 422
    assert client.get(API + "/alerts?dateTo=9999-12-31T23:59:59-14:00", headers=headers()).status_code == 422
    assert client.get(API + "/alerts?severity=made-up", headers=headers()).status_code == 422
    assert client.get(API + "/alerts?paginated=true&pageSize=201", headers=headers()).status_code == 422


def test_tenant_scope_holds_for_lists_counts_and_details(workspace):
    client, _ = workspace
    response = client.get(API + "/alerts?paginated=true", headers=headers("member_a"))
    assert [row["id"] for row in payload(response)] == ["tenant_a"]
    assert response.json()["meta"]["total"] == 1
    assert response.json()["meta"]["counts"]["total"] == 1
    assert client.get(API + "/alerts?tenantId=tn_b", headers=headers("member_a")).status_code == 404
    assert payload(client.get(API + "/alerts?scope=platform", headers=headers("member_a"))) == []
    for alert in ("platform", "private_a", "tenant_b", "deleted", "missing"):
        assert client.get(API + "/alerts/" + alert, headers=headers("member_a")).status_code == 404
    assert payload(client.get(API + "/alerts/tenant_a", headers=headers("member_a")))["tenantId"] == "tn_a"


def test_acknowledge_resolve_reopen_persist_with_actors_times_and_history(workspace):
    client, engine = workspace
    url = API + "/alerts/platform"
    acknowledged = payload(client.patch(url, json={"status": "acknowledged", "expectedStatus": "open", "note": " Investigating Redis "}, headers=headers()))
    assert acknowledged["acknowledgedBy"] == "root"
    assert acknowledged["acknowledgedByName"] == "Root"
    assert acknowledged["acknowledgedAt"].endswith("Z")
    assert acknowledged["resolvedAt"] is None
    resolved = payload(client.patch(url, json={"status": "resolved", "expectedStatus": "acknowledged"}, headers=headers()))
    assert resolved["resolvedBy"] == "root" and resolved["resolvedAt"]
    assert resolved["acknowledgedAt"] == acknowledged["acknowledgedAt"]
    reopened = payload(client.patch(url, json={"status": "open", "expectedStatus": "resolved"}, headers=headers()))
    assert reopened["acknowledgedAt"] is None and reopened["resolvedBy"] is None
    assert reopened["serviceKey"] == "redis"
    detail = payload(client.get(url, headers=headers()))
    assert detail["status"] == "open"
    assert detail["historyCount"] == 3 and detail["historyTruncated"] is False
    assert {entry["action"] for entry in detail["history"]} == {"Alert acknowledged", "Alert resolved", "Alert open"}
    assert next(entry for entry in detail["history"] if entry["action"] == "Alert acknowledged")["note"] == "Investigating Redis"
    with Session(engine) as db:
        assert db.get(PlatformAlert, "platform").status == "open"
        assert len(db.scalars(select(AuditLog).where(AuditLog.entity_id == "platform")).all()) == 3


def test_stale_update_is_conflict_and_repeated_status_is_idempotent(workspace):
    client, engine = workspace
    url = API + "/alerts/platform"
    acked = payload(client.patch(url, json={"status": "acknowledged"}, headers=headers()))
    conflict = client.patch(url, json={"status": "resolved", "expectedStatus": "open"}, headers=headers())
    assert conflict.status_code == 409
    repeated = payload(client.patch(url, json={"status": "acknowledged", "expectedStatus": "acknowledged"}, headers=headers()))
    assert repeated["acknowledgedAt"] == acked["acknowledgedAt"]
    with Session(engine) as db:
        assert db.get(PlatformAlert, "platform").status == "acknowledged"
        assert len(db.scalars(select(AuditLog).where(AuditLog.entity_id == "platform")).all()) == 1


def test_updates_are_super_admin_only_and_tenant_audit_is_scoped(workspace):
    client, engine = workspace
    assert client.get(API + "/alerts").status_code == 401
    assert client.get(API + "/alerts/platform").status_code == 401
    assert client.patch(API + "/alerts/tenant_a", json={"status": "resolved"}, headers=headers("member_a")).status_code == 403
    assert client.patch(API + "/alerts/deleted", json={"status": "resolved"}, headers=headers()).status_code == 404
    assert client.patch(API + "/alerts/missing", json={"status": "resolved"}, headers=headers()).status_code == 404
    assert client.patch(API + "/alerts/platform", json={"status": "invalid"}, headers=headers()).status_code == 422
    assert client.patch(API + "/alerts/platform", json={"status": "resolved", "note": "x" * 2001}, headers=headers()).status_code == 422
    payload(client.patch(API + "/alerts/tenant_a", json={"status": "resolved"}, headers=headers()))
    with Session(engine) as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.entity_id == "tenant_a"))
        assert audit.tenant_id == "tn_a"
        assert audit.previous_value["status"] == "acknowledged"
        assert audit.new_value["status"] == "resolved"


def test_detail_masks_audit_snapshots_and_reports_bounded_history(workspace):
    client, engine = workspace
    with Session(engine) as db:
        db.add_all([AuditLog(id=f"history_{i:03}", action="Auto alert update", entity_type="alert", entity_id="platform",
                            created_at=WHEN + timedelta(seconds=i),
                            previous_value={"nested": {"api-key": "hidden"}}, new_value={"password": "hidden"}) for i in range(202)])
        db.commit()
    detail = payload(client.get(API + "/alerts/platform", headers=headers()))
    assert detail["historyCount"] == 202 and detail["historyTruncated"] is True
    assert len(detail["history"]) == 200
    assert detail["history"][0]["id"] == "history_201"
    assert detail["history"][0]["previousValue"]["nested"]["api-key"] == "***"
    assert detail["history"][0]["newValue"]["password"] == "***"
