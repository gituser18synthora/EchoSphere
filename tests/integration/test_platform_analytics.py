"""Super Admin platform analytics: top tenants carry their average call length."""

from datetime import date
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.security import create_access_token
from backend.main import app
from shared.db.mysql import get_sessionmaker
from shared.ids import new_id
from shared.models import Tenant, UsageRecord, User


pytestmark = pytest.mark.integration

API = "/api/v1"


def _super_admin_headers() -> dict[str, str]:
    db = get_sessionmaker()()
    try:
        root = next(
            u for u in db.scalars(select(User).where(User.is_deleted.is_(False))).all()
            if u.role.code == "super_admin"
        )
        token = create_access_token(user_id=root.id, role="super_admin", tenant_id=None)
        return {"Authorization": f"Bearer {token}"}
    finally:
        db.close()


@pytest.fixture()
def busy_tenant():
    """A tenant whose call volume guarantees a spot in the top-6 list:
    100 000 calls totalling 115 000 talk minutes → 69 s per call."""
    suffix = uuid.uuid4().hex[:10]
    session = get_sessionmaker()()
    tenant = Tenant(
        id=new_id("tn"), name=f"Platform Analytics {suffix}", code=f"platform_{suffix}",
        domain=f"platform-{suffix}.example.test", status="active",
    )
    try:
        session.add(tenant)
        session.flush()
        session.add(UsageRecord(
            id=new_id("ur"), tenant_id=tenant.id, date=date.today(),
            calls=100_000, minutes=115_000.0,
        ))
        session.commit()
        yield {"tenant_id": tenant.id, "name": tenant.name}
    finally:
        session.rollback()
        session.execute(delete(UsageRecord).where(UsageRecord.tenant_id == tenant.id))
        session.execute(delete(Tenant).where(Tenant.id == tenant.id))
        session.commit()
        session.close()


def test_top_tenants_by_calls_include_average_call_duration(busy_tenant):
    with TestClient(app) as client:
        response = client.get(
            f"{API}/analytics/platform?days=30", headers=_super_admin_headers(),
        )

    assert response.status_code == 200, response.text
    rows = response.json()["data"]["topTenantsByCalls"]
    row = next(r for r in rows if r["label"] == busy_tenant["name"])
    assert row["value"] == 100_000
    assert row["avgDurationSec"] == 69
    assert row["avgDuration"] == "01:09"
    # Every listed tenant has calls, so every row carries a real clock value.
    assert all(r["avgDuration"] != "—" for r in rows)
