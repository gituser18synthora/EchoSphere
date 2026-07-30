"""End-to-end report registry, authorization, isolation and file tests."""

import csv
import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import delete, func, select

from backend.core.security import create_access_token
from backend.main import app
from backend.reports.registry import REPORT_REGISTRY
from shared.db.mysql import get_sessionmaker
from shared.models import AuditLog, UsageRecord, User, VoiceBot

pytestmark = pytest.mark.integration
API = "/api/v1"


@pytest.fixture(scope="module")
def client():
    db = get_sessionmaker()()
    before = set(
        db.scalars(select(AuditLog.id).where(AuditLog.action == "report.export")).all()
    )
    db.close()
    with TestClient(app) as test_client:
        yield test_client
    db = get_sessionmaker()()
    try:
        created = list(
            db.scalars(
                select(AuditLog.id).where(
                    AuditLog.action == "report.export",
                    AuditLog.id.not_in(before),
                )
            ).all()
        )
        if created:
            db.execute(delete(AuditLog).where(AuditLog.id.in_(created)))
            db.commit()
    finally:
        db.close()


def bearer(email: str) -> dict[str, str]:
    db = get_sessionmaker()()
    try:
        user = db.scalar(select(User).where(User.email == email))
        assert user is not None
        token = create_access_token(
            user_id=user.id,
            role=user.role.code,
            tenant_id=user.tenant_id,
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture(scope="module")
def super_admin():
    return bearer("alex.rivera@aurexion.com")


@pytest.fixture(scope="module")
def tenant_admin():
    return bearer("priya.sharma@meridianhealth.com")


def _csv_rows(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


@pytest.mark.parametrize("report_type", tuple(REPORT_REGISTRY))
def test_every_registered_report_supports_csv(client, super_admin, report_type):
    response = client.get(
        f"{API}/reports/{report_type}/export?format=csv&days=7",
        headers=super_admin,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].endswith('.csv"')
    rows = _csv_rows(response)
    assert rows[0] == [
        column.header for column in REPORT_REGISTRY[report_type].columns
    ]


@pytest.mark.parametrize("report_type", tuple(REPORT_REGISTRY))
def test_every_registered_report_supports_formatted_xlsx(
    client, super_admin, report_type
):
    response = client.get(
        f"{API}/reports/{report_type}/export?format=xlsx&days=7",
        headers=super_admin,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert response.headers["content-disposition"].endswith('.xlsx"')
    workbook = load_workbook(io.BytesIO(response.content))
    sheet = workbook.active
    assert sheet.freeze_panes == "A2"
    assert all(cell.font.bold for cell in sheet[1])
    assert [cell.value for cell in sheet[1]] == [
        column.header for column in REPORT_REGISTRY[report_type].columns
    ]
    assert len(sheet.title) <= 31
    assert not any(character in sheet.title for character in ":\\/?*[]")
    workbook.close()


def test_unauthorized_invalid_report_format_and_filters_rejected(
    client, super_admin
):
    assert client.get(f"{API}/reports/usage/export?format=csv&days=7").status_code == 401
    assert client.get(
        f"{API}/reports/not-real/export?format=csv&days=7", headers=super_admin
    ).status_code == 404
    assert client.get(
        f"{API}/reports/usage/export?format=pdf&days=7", headers=super_admin
    ).status_code == 422
    assert client.get(
        f"{API}/reports/usage/export?format=csv&days=2", headers=super_admin
    ).status_code == 422
    assert client.get(
        f"{API}/reports/revenue/export?format=csv&days=7&botId=bot-101",
        headers=super_admin,
    ).status_code == 422


def test_tenant_scope_is_server_enforced(client, tenant_admin):
    forbidden = client.get(
        f"{API}/reports/usage/export?format=csv&days=7&tenantId=tn-002",
        headers=tenant_admin,
    )
    assert forbidden.status_code == 403

    db = get_sessionmaker()()
    try:
        foreign_bot = db.scalar(
            select(VoiceBot).where(
                VoiceBot.tenant_id != "tn-001",
                VoiceBot.is_deleted.is_(False),
            )
        )
        assert foreign_bot is not None
    finally:
        db.close()
    hidden = client.get(
        f"{API}/reports/usage/export?format=csv&days=7&botId={foreign_bot.id}",
        headers=tenant_admin,
    )
    assert hidden.status_code == 404

    own = client.get(
        f"{API}/reports/usage/export?format=csv&days=7", headers=tenant_admin
    )
    assert own.status_code == 200
    exported_calls = sum(int(row[1]) for row in _csv_rows(own)[1:])

    db = get_sessionmaker()()
    try:
        expected_calls = int(
            db.scalar(
                select(func.coalesce(func.sum(UsageRecord.calls), 0)).where(
                    UsageRecord.tenant_id == "tn-001",
                    UsageRecord.bot_id.is_(None),
                    UsageRecord.date >= func.current_date() - 6,
                    UsageRecord.date <= func.current_date(),
                )
            )
            or 0
        )
    finally:
        db.close()
    assert exported_calls == expected_calls


def test_tenant_cannot_export_platform_revenue(client, tenant_admin):
    response = client.get(
        f"{API}/reports/revenue/export?format=csv&days=7", headers=tenant_admin
    )
    assert response.status_code == 403


def test_full_90_day_dataset_crosses_default_pagination_without_truncation(
    client, super_admin
):
    response = client.get(
        f"{API}/reports/usage/export?format=csv&days=90", headers=super_admin
    )
    assert response.status_code == 200
    rows = _csv_rows(response)
    assert len(rows) == 91  # one header + every day, not a default 50-row page
    assert rows[1][0] < rows[-1][0]  # deterministic oldest-to-newest order


def test_export_creates_sanitized_audit_record(client, super_admin):
    db = get_sessionmaker()()
    before = set(
        db.scalars(
            select(AuditLog.id).where(
                AuditLog.action == "report.export",
                AuditLog.entity_id == "ai_cost",
            )
        ).all()
    )
    db.close()
    response = client.get(
        f"{API}/reports/ai_cost/export?format=xlsx&days=7", headers=super_admin
    )
    assert response.status_code == 200

    db = get_sessionmaker()()
    try:
        audit = db.scalar(
            select(AuditLog)
            .where(
                AuditLog.action == "report.export",
                AuditLog.entity_id == "ai_cost",
                AuditLog.id.not_in(before),
            )
            .order_by(AuditLog.created_at.desc())
        )
        assert audit is not None
        assert audit.new_value["format"] == "xlsx"
        assert audit.new_value["rowCount"] == 7
        assert "token" not in str(audit.new_value).lower()
    finally:
        db.close()
