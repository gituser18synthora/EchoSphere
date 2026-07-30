"""Tenant-wise usage metering: event recording, DB-driven costing,
idempotency, missing-price handling, daily rollups, historical
reproducibility and the usage/currency API permission surface.

Live-app harness; all rows are uniquely suffixed and removed in teardown.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_MODEL = f"billing-test-{_SUFFIX}"
_created: list[tuple[str, str]] = []


def _session():
    from shared.db.mysql import get_sessionmaker

    return get_sessionmaker()()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for tenant_kind, row_id in reversed(_created):
            conn.execute(sa_text(f"DELETE FROM `{tenant_kind}` WHERE id = :id"), {"id": row_id})


@pytest.fixture(scope="module")
def tenants(client):
    """Two isolated tenants with a tenant-admin user each."""
    from shared.ids import new_id
    from shared.models import Role, Tenant, User

    session = _session()
    try:
        role = session.execute(select(Role).where(Role.code == "tenant_admin")).scalar_one()
        out = {}
        for label in ("a", "b"):
            tenant = Tenant(id=new_id("tn"), name=f"Usage {label.upper()} {_SUFFIX}",
                            code=f"usg{label}_{_SUFFIX}",
                            domain=f"usage-{label}-{_SUFFIX}.example.test", status="active")
            session.add(tenant)
            session.flush()
            user = User(id=new_id("usr"), email=f"usage.{label}.{_SUFFIX}@example.test",
                        name=f"Usage Admin {label.upper()}", password_hash="x",
                        role_id=role.id, tenant_id=tenant.id, status="active")
            session.add(user)
            session.commit()
            # Teardown deletes in reverse order — children (users) come last.
            _created.append(("tenants", tenant.id))
            _created.append(("users", user.id))
            out[label] = {
                "id": tenant.id,
                "headers": {"Authorization": f"Bearer {create_access_token(user_id=user.id, role='tenant_admin', tenant_id=tenant.id)}"},
            }
        return out
    finally:
        session.close()


@pytest.fixture(scope="module")
def catalog_model():
    """Register _MODEL in the provider-model catalog: pricing rows are
    validated against it, so synthetic price tests need a real entry."""
    from shared.ids import new_id
    from shared.models import ProviderModel

    session = _session()
    try:
        row = ProviderModel(
            id=new_id("pm"), provider_code="openai", capability="llm",
            code=_MODEL, display_name=f"Billing Test {_SUFFIX}", status="inactive",
        )
        session.add(row)
        session.commit()
        _created.append(("provider_models", row.id))
        return _MODEL
    finally:
        session.close()


@pytest.fixture(scope="module")
def super_admin():
    from shared.models import User

    session = _session()
    try:
        user = session.execute(
            select(User).where(User.email == "admin@aurexion.com")
        ).scalar_one()
        return {"Authorization": f"Bearer {create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)}"}
    finally:
        session.close()


def _track_events(tenant_id: str) -> None:
    """Register this tenant's usage rows for teardown."""
    from shared.models import UsageEvent, UsageRecord

    session = _session()
    try:
        for event_id in session.scalars(
            select(UsageEvent.id).where(UsageEvent.tenant_id == tenant_id)
        ):
            entry = ("usage_events", event_id)
            if entry not in _created:
                _created.append(entry)
        for record_id in session.scalars(
            select(UsageRecord.id).where(UsageRecord.tenant_id == tenant_id)
        ):
            entry = ("usage_records", record_id)
            if entry not in _created:
                _created.append(entry)
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success"), body
    return body["data"]


# ── metering engine ───────────────────────────────────────────────────────────


def test_llm_usage_costed_from_seeded_blended_price(tenants):
    from shared.billing.metering import record_usage_event

    session = _session()
    try:
        event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code="gpt-4o-mini",
            input_tokens=1000, output_tokens=500,
            request_id=f"test:{_SUFFIX}:llm-blended",
        )
        # Seeded blended price: 0.0006 / 1k tokens → 1500 tokens = 0.0009.
        assert event is not None
        assert Decimal(str(event.cost_usd)) == Decimal("0.0009")
        assert event.pricing_status == "priced"
        assert Decimal(event.pricing_snapshot["input_tokens"]["unitPrice"]) == Decimal("0.0006")
        assert event.total_tokens == 1500
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_split_input_output_pricing_beats_blended(tenants, super_admin, client, catalog_model):
    from shared.billing.metering import record_usage_event

    effective = (datetime.utcnow() - timedelta(days=1)).isoformat(timespec="seconds")
    for component, price in (("input_tokens", "2.50"), ("output_tokens", "10.00")):
        row = _data(client.post(f"{API}/master/provider-pricing", headers=super_admin, json={
            "providerCode": "openai", "capability": "llm", "modelCode": _MODEL,
            "component": component, "unit": "per_1m_tokens", "unitPrice": price,
            "currencyCode": "USD", "effectiveFrom": effective,
        }))
        _created.append(("provider_pricing", row["id"]))

    session = _session()
    try:
        event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code=_MODEL,
            input_tokens=1_000_000, output_tokens=100_000,
            request_id=f"test:{_SUFFIX}:llm-split",
        )
        # 1M × $2.50/1M + 0.1M × $10/1M = 2.50 + 1.00
        assert Decimal(str(event.cost_usd)) == Decimal("3.50")
        snapshot = event.pricing_snapshot
        assert snapshot["input_tokens"]["unit"] == "per_1m_tokens"
        assert snapshot["output_tokens"]["cost"] == "1.000000"
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_embedding_usage_costed(tenants):
    from shared.billing.metering import record_usage_event

    session = _session()
    try:
        event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="embedding",
            provider_code="openai", model_code="text-embedding-3-small",
            total_tokens=100_000, request_id=f"test:{_SUFFIX}:embed",
        )
        # Seeded: 0.00002 / 1k tokens → 100k tokens = 0.002.
        assert Decimal(str(event.cost_usd)) == Decimal("0.002")
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_missing_pricing_records_quantity_without_fabricating_cost(tenants):
    # Unpriced synthetic model codes — the seeded catalog models (saaras:v3,
    # eleven_flash_v2_5, …) now ship with real prices.
    from shared.billing.metering import record_usage_event

    session = _session()
    try:
        stt = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="stt",
            provider_code="sarvam", model_code=f"unpriced-stt-{_SUFFIX}",
            audio_seconds=Decimal("42.5"), request_id=f"test:{_SUFFIX}:stt",
        )
        assert stt.pricing_status == "missing_price"
        assert Decimal(str(stt.cost_usd)) == 0
        assert Decimal(str(stt.audio_seconds)) == Decimal("42.5")
        assert "audio_seconds" in stt.pricing_snapshot["missing"]

        tts = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="tts",
            provider_code="elevenlabs", model_code=f"unpriced-tts-{_SUFFIX}",
            voice_code="rachel", characters=640, request_id=f"test:{_SUFFIX}:tts",
        )
        assert tts.pricing_status == "missing_price"
        assert tts.characters == 640
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_duplicate_request_id_not_recorded_twice(tenants):
    from shared.billing.metering import record_usage_event
    from shared.models import UsageEvent

    request_id = f"test:{_SUFFIX}:idempotent"
    session = _session()
    try:
        first = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code="gpt-4o-mini",
            input_tokens=100, output_tokens=100, request_id=request_id,
        )
        assert first is not None
        replay = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code="gpt-4o-mini",
            input_tokens=100, output_tokens=100, request_id=request_id,
        )
        assert replay is None
        count = len(session.scalars(
            select(UsageEvent.id).where(UsageEvent.request_id == request_id)
        ).all())
        assert count == 1
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_rollup_updates_tenant_daily_record(tenants):
    from shared.billing.metering import record_usage_event
    from shared.models import UsageRecord

    session = _session()
    try:
        before = session.execute(
            select(UsageRecord).where(
                UsageRecord.tenant_id == tenants["b"]["id"],
                UsageRecord.bot_id.is_(None),
                UsageRecord.date == datetime.utcnow().date(),
            )
        ).scalar_one_or_none()
        assert before is None

        record_usage_event(
            session, tenant_id=tenants["b"]["id"], capability="llm",
            provider_code="openai", model_code="gpt-4o-mini",
            input_tokens=10_000, output_tokens=0,
            request_id=f"test:{_SUFFIX}:rollup",
        )
        rollup = session.execute(
            select(UsageRecord).where(
                UsageRecord.tenant_id == tenants["b"]["id"],
                UsageRecord.bot_id.is_(None),
                UsageRecord.date == datetime.utcnow().date(),
            )
        ).scalar_one()
        assert Decimal(str(rollup.cost_llm)) == Decimal("0.006")
        assert Decimal(str(rollup.cost_embedding)) == 0
    finally:
        session.close()
    _track_events(tenants["b"]["id"])


def test_historical_costs_survive_price_change(tenants, super_admin, client, catalog_model):
    from shared.billing.metering import record_usage_event
    from shared.models import UsageEvent

    session = _session()
    try:
        original = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code=_MODEL,
            input_tokens=1_000_000, request_id=f"test:{_SUFFIX}:historical",
        )
        original_id, original_cost = original.id, Decimal(str(original.cost_usd))
        assert original_cost == Decimal("2.50")
    finally:
        session.close()

    # Price change: new effective row at double the price.
    row = _data(client.post(f"{API}/master/provider-pricing", headers=super_admin, json={
        "providerCode": "openai", "capability": "llm", "modelCode": _MODEL,
        "component": "input_tokens", "unit": "per_1m_tokens", "unitPrice": "5.00",
        "currencyCode": "USD",
        "effectiveFrom": datetime.utcnow().isoformat(timespec="seconds"),
    }))
    _created.append(("provider_pricing", row["id"]))

    session = _session()
    try:
        new_event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code=_MODEL,
            input_tokens=1_000_000, request_id=f"test:{_SUFFIX}:after-change",
        )
        assert Decimal(str(new_event.cost_usd)) == Decimal("5.00")
        # Yesterday's event still carries yesterday's price and cost.
        unchanged = session.get(UsageEvent, original_id)
        assert Decimal(str(unchanged.cost_usd)) == original_cost
        assert Decimal(unchanged.pricing_snapshot["input_tokens"]["unitPrice"]) == Decimal("2.50")
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


# ── STT/TTS provider costing ─────────────────────────────────────────────────


def test_stt_per_hour_inr_price_converts_via_exchange_rate(tenants, super_admin, client):
    """Sarvam quotes ₹30/hour; the event costs in USD through the configured
    USD→INR rate in force when the usage occurred."""
    from shared.billing.metering import record_usage_event

    rate = _data(client.post(f"{API}/master/exchange-rates", headers=super_admin, json={
        "baseCode": "USD", "targetCode": "INR", "rate": "96.00",
        "effectiveFrom": (datetime.utcnow() - timedelta(minutes=10)).isoformat(timespec="seconds"),
    }))
    _created.append(("exchange_rates", rate["id"]))

    session = _session()
    try:
        event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="stt",
            provider_code="sarvam", model_code="saarika:v2.5",
            audio_seconds=90, request_id=f"test:{_SUFFIX}:stt-inr",
        )
        # 90s = 0.025h × ₹30 = ₹0.75; ₹0.75 / 96.00 = $0.0078125 → $0.007813.
        assert event.pricing_status == "priced"
        assert Decimal(str(event.cost_usd)) == Decimal("0.007813")
        snapshot = event.pricing_snapshot["audio_seconds"]
        assert snapshot["unit"] == "per_hour"
        assert Decimal(snapshot["unitPrice"]) == Decimal("30")
        assert snapshot["currency"] == "INR"
        assert Decimal(snapshot["fxRate"]) == Decimal("96")
        assert snapshot["priceId"]  # exact price row pinned for audit
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_tts_per_1k_characters_costed(tenants):
    from shared.billing.metering import record_usage_event

    session = _session()
    try:
        event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="tts",
            provider_code="elevenlabs", model_code="eleven_flash_v2_5",
            voice_code="monika", characters=640,
            request_id=f"test:{_SUFFIX}:tts-el",
        )
        # Seeded $0.05 / 1K characters → 640 chars = $0.032.
        assert event.pricing_status == "priced"
        assert Decimal(str(event.cost_usd)) == Decimal("0.032")
        assert Decimal(str(event.charge_usd)) == 0  # no selling price configured
        snapshot = event.pricing_snapshot["characters"]
        assert snapshot["unit"] == "per_1k_characters"
        assert snapshot["fxRate"] is None  # native USD price, no conversion
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_selling_price_produces_tenant_charge(tenants, super_admin, client, catalog_model):
    from shared.billing.metering import record_usage_event

    effective = (datetime.utcnow() - timedelta(hours=2)).isoformat(timespec="seconds")
    row = _data(client.post(f"{API}/master/provider-pricing", headers=super_admin, json={
        "providerCode": "openai", "capability": "llm", "modelCode": _MODEL,
        "component": "output_tokens", "unit": "per_1m_tokens",
        "unitPrice": "10.00", "sellingPrice": "15.00",
        "currencyCode": "USD", "effectiveFrom": effective,
    }))
    _created.append(("provider_pricing", row["id"]))
    assert row["sellingPrice"] is not None

    session = _session()
    try:
        event = record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code=_MODEL,
            output_tokens=200_000, request_id=f"test:{_SUFFIX}:charge",
        )
        # Cost 0.2M × $10/1M = $2.00; tenant charge 0.2M × $15/1M = $3.00.
        assert Decimal(str(event.cost_usd)) == Decimal("2.00")
        assert Decimal(str(event.charge_usd)) == Decimal("3.00")
        snapshot = event.pricing_snapshot["output_tokens"]
        assert Decimal(snapshot["sellingPrice"]) == Decimal("15.00")
        assert snapshot["charge"] == "3.000000"
        assert snapshot["priceId"] == row["id"]
    finally:
        session.close()
    _track_events(tenants["a"]["id"])


def test_session_cost_breakdown_keeps_stt_and_tts_auditable(tenants, super_admin, client):
    """One voice call: STT + TTS + LLM events under a session id; the session
    endpoint returns each event separately and the STT+TTS subtotal."""
    from shared.billing.metering import record_usage_event

    session_id = f"sess-{_SUFFIX}"
    session = _session()
    try:
        record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="stt",
            provider_code="openai", model_code="whisper-1",
            session_id=session_id, audio_seconds=90,
            request_id=f"test:{_SUFFIX}:sess:stt",
        )
        record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="tts",
            provider_code="elevenlabs", model_code="eleven_flash_v2_5",
            session_id=session_id, characters=640,
            request_id=f"test:{_SUFFIX}:sess:tts",
        )
        record_usage_event(
            session, tenant_id=tenants["a"]["id"], capability="llm",
            provider_code="openai", model_code="gpt-4o-mini",
            session_id=session_id, input_tokens=1000, output_tokens=500,
            request_id=f"test:{_SUFFIX}:sess:llm",
        )
    finally:
        session.close()
    _track_events(tenants["a"]["id"])

    payload = _data(client.get(
        f"{API}/usage/sessions/{session_id}", headers=tenants["a"]["headers"]
    ))
    assert len(payload["events"]) == 3
    by_cap = {e["capability"]: e for e in payload["events"]}
    # whisper-1 $0.006/min × 1.5min; ElevenLabs $0.05/1K × 640 chars.
    assert by_cap["stt"]["costUsd"] == pytest.approx(0.009)
    assert by_cap["tts"]["costUsd"] == pytest.approx(0.032)
    assert payload["aiVoiceCostUsd"] == pytest.approx(
        by_cap["stt"]["costUsd"] + by_cap["tts"]["costUsd"]
    )
    assert payload["totalCostUsd"] == pytest.approx(
        sum(e["costUsd"] for e in payload["events"])
    )
    assert payload["costByCapability"]["stt"] == pytest.approx(0.009)
    # Every event stays independently auditable.
    assert all(e["pricingSnapshot"] for e in payload["events"])

    # Cross-tenant access is a 404 (existence never leaks); supers see all.
    denied = client.get(f"{API}/usage/sessions/{session_id}", headers=tenants["b"]["headers"])
    assert denied.status_code == 404
    admin_view = _data(client.get(f"{API}/usage/sessions/{session_id}", headers=super_admin))
    assert admin_view["tenantId"] == tenants["a"]["id"]
    missing = client.get(f"{API}/usage/sessions/nope-{_SUFFIX}", headers=super_admin)
    assert missing.status_code == 404


# ── API surface: tenant isolation + permissions ───────────────────────────────


def test_usage_summary_tenant_scoped(client, tenants):
    summary = _data(client.get(f"{API}/usage/summary?days=7", headers=tenants["a"]["headers"]))
    assert summary["tenantId"] == tenants["a"]["id"]
    assert summary["baseCurrency"] == "USD"
    assert summary["capabilities"]["llm"]["inputTokens"] > 0
    # 42.5s unpriced + 90s Sarvam INR + 90s whisper session event.
    assert summary["capabilities"]["stt"]["audioSeconds"] == pytest.approx(222.5)
    # 640 unpriced + 640 ElevenLabs + 640 session event.
    assert summary["capabilities"]["tts"]["characters"] == 1920
    assert summary["missingPriceEvents"] >= 2
    assert summary["totalCostUsd"] > 0
    # The tenant charge aggregate is exposed alongside cost.
    assert summary["capabilities"]["llm"]["chargeUsd"] >= 3.0


def test_usage_summary_rejects_cross_tenant(client, tenants):
    cross = client.get(
        f"{API}/usage/summary?tenantId={tenants['b']['id']}",
        headers=tenants["a"]["headers"],
    )
    assert cross.status_code == 403

    # Tenant B sees only its own events — nothing from tenant A leaks over.
    own = _data(client.get(f"{API}/usage/summary?days=7", headers=tenants["b"]["headers"]))
    assert own["capabilities"]["stt"]["audioSeconds"] == 0
    assert own["capabilities"]["llm"]["inputTokens"] == 10_000


def test_super_admin_platform_usage(client, super_admin, tenants):
    forbidden = client.get(f"{API}/usage/platform", headers=tenants["a"]["headers"])
    assert forbidden.status_code == 403

    payload = _data(client.get(f"{API}/usage/platform?days=7", headers=super_admin))
    tenant_rows = {row["tenantId"]: row for row in payload["byTenant"]}
    assert tenants["a"]["id"] in tenant_rows
    assert tenants["b"]["id"] in tenant_rows
    assert tenant_rows[tenants["a"]["id"]]["costUsd"] > 0
    assert "llm" in payload["byCapability"]
    providers = {(r["capability"], r["provider"]) for r in payload["byProviderModel"]}
    assert ("stt", "sarvam") in providers

    # Super admin can also scope the tenant summary explicitly.
    scoped = _data(client.get(
        f"{API}/usage/summary?tenantId={tenants['b']['id']}", headers=super_admin
    ))
    assert scoped["tenantId"] == tenants["b"]["id"]


def test_summary_conversion_uses_configured_rate(client, super_admin, tenants):
    rate = _data(client.post(f"{API}/master/exchange-rates", headers=super_admin, json={
        "baseCode": "USD", "targetCode": "INR", "rate": "86.50",
        "effectiveFrom": (datetime.utcnow() - timedelta(minutes=5)).isoformat(timespec="seconds"),
    }))
    _created.append(("exchange_rates", rate["id"]))

    summary = _data(client.get(f"{API}/usage/summary?days=7", headers=tenants["a"]["headers"]))
    usd = Decimal(str(summary["totalCostUsd"]))
    inr = Decimal(str(summary["totalCostConverted"]["INR"]))
    assert inr == pytest.approx(usd * Decimal("86.50"), rel=Decimal("0.0001"))

    rates = _data(client.get(f"{API}/currency/rates", headers=tenants["a"]["headers"]))
    assert rates["rates"]["INR"] == pytest.approx(86.50)
