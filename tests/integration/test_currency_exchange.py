"""Currency + exchange-rate configuration under Platform Configuration:

- currency CRUD, ISO validation, base-currency protection
- exchange-rate CRUD, pair/rate/effective-date validation
- effective-date selection and inactive-rate exclusion
- permissions (super admin manages; tenant admin is rejected)
- audit rows and active-first ordering

Live-app harness; every created row is uniquely suffixed and removed in the
module teardown.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_created: list[tuple[str, str]] = []  # (table, id)


def _purge_test_currency() -> None:
    """XTS (the ISO test currency) is only ever created by this module —
    remove any leftovers from an aborted previous run before/after tests."""
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text(
            "DELETE FROM exchange_rates WHERE base_code = 'XTS' OR target_code = 'XTS'"
        ))
        conn.execute(sa_text("DELETE FROM provider_pricing WHERE currency_code = 'XTS'"))
        conn.execute(sa_text("DELETE FROM currencies WHERE code = 'XTS'"))


@pytest.fixture(scope="module")
def client():
    _purge_test_currency()
    with TestClient(app) as test_client:
        yield test_client
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    engine = get_engine()
    for table, row_id in reversed(_created):
        # Per-row transactions: one failed delete must not strand the rest.
        try:
            with engine.begin() as conn:
                conn.execute(sa_text(f"DELETE FROM `{table}` WHERE id = :id"), {"id": row_id})
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass
    _purge_test_currency()


@pytest.fixture(scope="module")
def super_admin():
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(
            select(User).where(User.email == "admin@aurexion.com")
        ).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


@pytest.fixture(scope="module")
def tenant_admin(client):
    """A real tenant-admin user created for this module (no demo-seed reliance)."""
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.ids import new_id
    from shared.models import Role, Tenant, User

    session = get_sessionmaker()()
    try:
        role = session.execute(select(Role).where(Role.code == "tenant_admin")).scalar_one()
        tenant = Tenant(id=new_id("tn"), name=f"FX Test Tenant {_SUFFIX}",
                        code=f"fxt_{_SUFFIX}", domain=f"fx-{_SUFFIX}.example.test",
                        status="active")
        session.add(tenant)
        session.flush()
        user = User(
            id=new_id("usr"), email=f"fx.admin.{_SUFFIX}@example.test",
            name="FX Tenant Admin", password_hash="x", role_id=role.id,
            tenant_id=tenant.id, status="active",
        )
        session.add(user)
        session.commit()
        # Teardown deletes in reverse order — children (users) must come last.
        _created.append(("tenants", tenant.id))
        _created.append(("users", user.id))
        token = create_access_token(user_id=user.id, role="tenant_admin", tenant_id=tenant.id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success"), body
    return body["data"]


def _field_errors(response) -> dict[str, str]:
    body = response.json()
    return {e["field"]: e["message"] for e in body.get("errors", [])}


# ── currencies ────────────────────────────────────────────────────────────────


def test_seeded_currencies_present(client, super_admin):
    rows = _data(client.get(f"{API}/master/currencies?pageSize=100", headers=super_admin))
    codes = {r["code"] for r in rows}
    assert {"USD", "INR", "EUR", "GBP"} <= codes
    usd = next(r for r in rows if r["code"] == "USD")
    assert usd["isBase"] is True and usd["symbol"] == "$"
    inr = next(r for r in rows if r["code"] == "INR")
    assert inr["symbol"] == "₹"


def test_currency_create_validation(client, super_admin):
    bad = client.post(f"{API}/master/currencies", headers=super_admin,
                      json={"name": "Bad", "code": "TOOLONG", "symbol": "?"})
    assert bad.status_code == 422
    assert "ISO 4217" in _field_errors(bad)["code"]

    dup = client.post(f"{API}/master/currencies", headers=super_admin,
                      json={"name": "Dollar Again", "code": "usd", "symbol": "$"})
    assert dup.status_code == 422
    assert "already exists" in _field_errors(dup)["code"]

    no_symbol = client.post(f"{API}/master/currencies", headers=super_admin,
                            json={"name": "No Symbol", "code": "XTS"})
    assert no_symbol.status_code == 422
    assert "symbol" in _field_errors(no_symbol)


def test_currency_create_and_status(client, super_admin):
    created = _data(client.post(f"{API}/master/currencies", headers=super_admin,
                                json={"name": f"Test Currency {_SUFFIX}", "code": "xts",
                                      "symbol": "¤", "decimalPlaces": 2}))
    _created.append(("currencies", created["id"]))
    assert created["code"] == "XTS"  # uppercased ISO code
    assert created["isBase"] is False

    deactivated = _data(client.post(
        f"{API}/master/currencies/{created['id']}/status",
        headers=super_admin, json={"status": "inactive"},
    ))
    assert deactivated["status"] == "inactive"
    # Reactivate — the exchange-rate tests below need it active.
    client.post(f"{API}/master/currencies/{created['id']}/status",
                headers=super_admin, json={"status": "active"})


def test_base_currency_cannot_be_deactivated_or_deleted(client, super_admin):
    rows = _data(client.get(f"{API}/master/currencies?pageSize=100", headers=super_admin))
    usd = next(r for r in rows if r["code"] == "USD")
    off = client.post(f"{API}/master/currencies/{usd['id']}/status",
                      headers=super_admin, json={"status": "inactive"})
    assert off.status_code == 422
    gone = client.delete(f"{API}/master/currencies/{usd['id']}", headers=super_admin)
    assert gone.status_code == 409


# ── exchange rates ────────────────────────────────────────────────────────────


def _make_rate(client, headers, *, rate="100.0", days_ago=1, target="XTS"):
    effective = (datetime.utcnow() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    return client.post(f"{API}/master/exchange-rates", headers=headers, json={
        "baseCode": "USD", "targetCode": target, "rate": rate,
        "effectiveFrom": effective,
    })


def test_exchange_rate_validation(client, super_admin):
    same = _make_rate(client, super_admin, target="USD")
    assert same.status_code == 422
    assert "must differ" in _field_errors(same)["targetCode"]

    zero = client.post(f"{API}/master/exchange-rates", headers=super_admin,
                       json={"baseCode": "USD", "targetCode": "XTS", "rate": 0})
    assert zero.status_code == 422
    assert "greater than zero" in _field_errors(zero)["rate"]

    negative = client.post(f"{API}/master/exchange-rates", headers=super_admin,
                           json={"baseCode": "USD", "targetCode": "XTS", "rate": -5})
    assert negative.status_code == 422
    assert "greater than zero" in _field_errors(negative)["rate"]

    non_base = client.post(f"{API}/master/exchange-rates", headers=super_admin,
                           json={"baseCode": "INR", "targetCode": "XTS", "rate": 1.5})
    assert non_base.status_code == 422
    assert "base currency" in _field_errors(non_base)["baseCode"].lower()

    unknown = client.post(f"{API}/master/exchange-rates", headers=super_admin,
                          json={"baseCode": "USD", "targetCode": "ZZZ", "rate": 2})
    assert unknown.status_code == 422
    assert "not an active currency" in _field_errors(unknown)["targetCode"]

    far_future = client.post(f"{API}/master/exchange-rates", headers=super_admin, json={
        "baseCode": "USD", "targetCode": "XTS", "rate": 2,
        "effectiveFrom": (datetime.utcnow() + timedelta(days=800)).isoformat(),
    })
    assert far_future.status_code == 422
    assert "year in the future" in _field_errors(far_future)["effectiveFrom"]


def test_exchange_rate_inactive_currency_rejected(client, super_admin):
    rows = _data(client.get(f"{API}/master/currencies?pageSize=100&search={_SUFFIX}",
                            headers=super_admin))
    xts = next(r for r in rows if r["code"] == "XTS")
    client.post(f"{API}/master/currencies/{xts['id']}/status",
                headers=super_admin, json={"status": "inactive"})
    rejected = _make_rate(client, super_admin, days_ago=30)
    assert rejected.status_code == 422
    assert "not an active currency" in _field_errors(rejected)["targetCode"]
    client.post(f"{API}/master/currencies/{xts['id']}/status",
                headers=super_admin, json={"status": "active"})


def test_exchange_rate_lifecycle_and_effective_selection(client, super_admin):
    older = _data(_make_rate(client, super_admin, rate="100.0", days_ago=2))
    _created.append(("exchange_rates", older["id"]))
    newer = _data(_make_rate(client, super_admin, rate="200.0", days_ago=1))
    _created.append(("exchange_rates", newer["id"]))
    assert older["name"] == "USD → XTS"

    duplicate = client.post(f"{API}/master/exchange-rates", headers=super_admin, json={
        "baseCode": "USD", "targetCode": "XTS", "rate": "300",
        "effectiveFrom": newer["effectiveFrom"].replace("Z", ""),
    })
    assert duplicate.status_code == 422
    assert "already exists" in _field_errors(duplicate)["effectiveFrom"]

    def _current_rate() -> Decimal | None:
        # Fresh session per read — REPEATABLE READ would otherwise pin the
        # first snapshot and hide the API's committed changes.
        from shared.billing.currency import effective_rate
        from shared.db.mysql import get_sessionmaker

        session = get_sessionmaker()()
        try:
            return effective_rate(session, "XTS")
        finally:
            session.close()

    assert _current_rate() == Decimal("200")

    # A future-dated rate must not take effect yet.
    future = _data(client.post(f"{API}/master/exchange-rates", headers=super_admin, json={
        "baseCode": "USD", "targetCode": "XTS", "rate": "999",
        "effectiveFrom": (datetime.utcnow() + timedelta(days=30)).isoformat(timespec="seconds"),
    }))
    _created.append(("exchange_rates", future["id"]))
    assert _current_rate() == Decimal("200")

    # Deactivating the newest active rate falls back to the older one.
    off = client.post(f"{API}/master/exchange-rates/{newer['id']}/status",
                      headers=super_admin, json={"status": "inactive"})
    assert off.status_code == 200
    assert _current_rate() == Decimal("100")

    # Active-first ordering: the deactivated rate sinks below active ones.
    rows = _data(client.get(f"{API}/master/exchange-rates?pageSize=100&search=XTS",
                            headers=super_admin))
    statuses = [r["status"] for r in rows]
    assert "inactive" in statuses
    assert statuses.index("inactive") > 0
    first_inactive = statuses.index("inactive")
    assert all(s == "inactive" for s in statuses[first_inactive:])


def test_exchange_rate_edit_and_audit(client, super_admin):
    created = _data(_make_rate(client, super_admin, rate="86.50", days_ago=3))
    _created.append(("exchange_rates", created["id"]))
    assert Decimal(created["rate"]) == Decimal("86.50")

    updated = _data(client.patch(f"{API}/master/exchange-rates/{created['id']}",
                                 headers=super_admin, json={"rate": "87.25"}))
    assert Decimal(updated["rate"]) == Decimal("87.25")

    audit = _data(client.get(f"{API}/master/exchange-rates/{created['id']}/audit",
                             headers=super_admin))
    actions = [a["action"] for a in audit]
    assert any("Created exchange rate" in a for a in actions)
    assert any("Updated exchange rate" in a for a in actions)


def test_tenant_admin_cannot_manage_rates_or_currencies(client, super_admin, tenant_admin):
    assert _make_rate(client, tenant_admin, days_ago=10).status_code == 403
    assert client.post(f"{API}/master/currencies", headers=tenant_admin,
                       json={"name": "Nope", "code": "XXA", "symbol": "?"}).status_code == 403

    rows = _data(client.get(f"{API}/master/exchange-rates?pageSize=1", headers=super_admin))
    if rows:
        assert client.patch(f"{API}/master/exchange-rates/{rows[0]['id']}",
                            headers=tenant_admin, json={"rate": "1"}).status_code == 403


def test_currency_rates_endpoint_for_tenant_roles(client, tenant_admin):
    payload = _data(client.get(f"{API}/currency/rates", headers=tenant_admin))
    assert payload["baseCurrency"] == "USD"
    codes = {c["code"] for c in payload["currencies"]}
    assert {"USD", "INR"} <= codes
    usd = next(c for c in payload["currencies"] if c["code"] == "USD")
    assert usd["isBase"] is True and usd["hasRate"] is True
