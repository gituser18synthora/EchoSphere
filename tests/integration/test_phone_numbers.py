"""Phone number management (platform admin): editing, the active/inactive
gate, assignment guards, audit — and how deactivation interacts with channel
claims and how bot Calls/Month + Cost/Call are derived from metered usage.

Runs against the live app + local databases. Every row the module creates
(numbers, the throwaway bot and its children, usage records) is removed in
teardown — demo data is never mutated.
"""

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from backend.core.security import create_access_token
from backend.main import app

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
# Unique, valid E.164 numbers per run (uuid-derived digits).
_DIGITS = f"{int(_SUFFIX, 16) % 10**7:07d}"
NUMBER_A = f"+9198{_DIGITS}1"
NUMBER_B = f"+9198{_DIGITS}2"
NUMBER_C = f"+9198{_DIGITS}3"
NUMBER_D = f"+9198{_DIGITS}4"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def _bearer(email: str) -> dict:
    from sqlalchemy import select

    from shared.db.mysql import get_sessionmaker
    from shared.models import User

    session = get_sessionmaker()()
    try:
        user = session.execute(select(User).where(User.email == email)).scalar_one()
        token = create_access_token(user_id=user.id, role=user.role.code,
                                    tenant_id=user.tenant_id)
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


@pytest.fixture(scope="module")
def super_admin():
    return _bearer("admin@aurexion.com")


@pytest.fixture(scope="module")
def tenant_admin():
    return _bearer("priya.sharma@meridianhealth.com")  # tenant_admin of tn-001


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        for prefix in (f"+9198{_DIGITS}%", f"+9197{_DIGITS}%"):
            conn.execute(sa_text(
                "DELETE FROM audit_logs WHERE entity_type='phone_number' AND entity_id IN "
                "(SELECT id FROM phone_numbers WHERE number LIKE :n)"
            ), {"n": prefix})
            conn.execute(sa_text("DELETE FROM phone_numbers WHERE number LIKE :n"),
                         {"n": prefix})


@pytest.fixture(scope="module")
def test_bot(client, tenant_admin):
    """A published throwaway tn-001 bot for channel-claim and metrics tests."""
    created = _data(client.post(f"{API}/bots", headers=tenant_admin, json={
        "name": f"Number Test Bot {_SUFFIX}", "useCase": "numbers",
        "languages": ["hi-IN"],
    }))
    bot_id = created["id"]

    from shared.db.mysql import get_sessionmaker
    from shared.models import VoiceBot

    session = get_sessionmaker()()
    try:
        bot = session.get(VoiceBot, bot_id)
        bot.status = "published"
        bot.live_version = "v1.0.0"
        session.commit()
    finally:
        session.close()

    yield {"id": bot_id}

    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(sa_text("DELETE FROM audit_logs WHERE entity_id IN "
                             "(SELECT id FROM channel_configs WHERE bot_id = :b)"), {"b": bot_id})
        for table in ("channel_configs", "voice_bot_settings", "voice_bot_readiness",
                      "bot_languages", "workflows", "prompts", "usage_records",
                      "phone_numbers"):
            conn.execute(sa_text(f"DELETE FROM `{table}` WHERE bot_id = :b"), {"b": bot_id})
        conn.execute(sa_text("DELETE FROM voice_bots WHERE id = :b"), {"b": bot_id})


def _create(client, super_admin, number, **extra):
    return _data(client.post(f"{API}/phone-numbers", headers=super_admin, json={
        "number": number, "country": "IN", "provider": "vaani", **extra,
    }))


def _get_row(client, super_admin, number_id):
    rows = _data(client.get(f"{API}/phone-numbers", headers=super_admin))
    return next(r for r in rows if r["id"] == number_id)


# ── listing & serializer ─────────────────────────────────────────────────────


def test_list_reports_active_flag(client, super_admin):
    rows = _data(client.get(f"{API}/phone-numbers", headers=super_admin))
    assert rows, "seeded numbers expected"
    assert all(isinstance(r["isActive"], bool) for r in rows)


def test_listing_requires_super_admin(client, tenant_admin):
    assert client.get(f"{API}/phone-numbers", headers=tenant_admin).status_code == 403


# ── create validation ────────────────────────────────────────────────────────


def test_create_rejects_invalid_e164(client, super_admin):
    response = client.post(f"{API}/phone-numbers", headers=super_admin, json={
        "number": "not-a-number", "country": "IN",
    })
    assert response.status_code == 422


def test_create_rejects_duplicate_in_any_formatting(client, super_admin):
    # Seeded pn-01 stores "+1 (415) 555-0119" — the bare form must conflict.
    response = client.post(f"{API}/phone-numbers", headers=super_admin, json={
        "number": "+14155550119", "country": "US",
    })
    assert response.status_code == 409


# ── editing ──────────────────────────────────────────────────────────────────


def test_edit_updates_fields_and_persists(client, super_admin):
    row = _create(client, super_admin, NUMBER_A, monthlyCost=1.0)
    updated = _data(client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin, json={
        "country": "US", "provider": "twilio", "monthlyCost": 2.5, "status": "porting",
    }))
    assert (updated["country"], updated["provider"]) == ("US", "twilio")
    assert updated["monthlyCost"] == 2.5
    assert updated["status"] == "porting"
    # Persisted — a fresh list read returns the same values.
    fresh = _get_row(client, super_admin, row["id"])
    assert (fresh["country"], fresh["provider"], fresh["monthlyCost"]) == ("US", "twilio", 2.5)

    from sqlalchemy import text as sa_text

    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        audits = conn.execute(sa_text(
            "SELECT COUNT(*) FROM audit_logs WHERE entity_type='phone_number' "
            "AND entity_id=:i AND action='Updated phone number'"), {"i": row["id"]}).scalar()
    assert audits >= 1


def test_edit_number_itself(client, super_admin):
    row = _create(client, super_admin, NUMBER_B)
    updated = _data(client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin,
                                 json={"number": NUMBER_C}))
    assert updated["number"] == NUMBER_C


def test_clearing_bot_assignment_also_clears_tenant(
    client, super_admin, test_bot,
):
    row = _create(client, super_admin, NUMBER_D,
                  tenantId="tn-001", botId=test_bot["id"])
    assert row["tenant"] is not None and row["bot"] is not None

    released = _data(client.patch(
        f"{API}/phone-numbers/{row['id']}", headers=super_admin,
        json={"botId": None},
    ))
    assert released["tenant"] is None
    assert released["bot"] is None
    assert released["status"] == "available"


def test_edit_rejects_invalid_e164(client, super_admin):
    row = _get_first_test_row(client, super_admin)
    response = client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin,
                            json={"number": "12"})
    assert response.status_code == 422


def test_edit_rejects_duplicate_number(client, super_admin):
    row = _get_first_test_row(client, super_admin)
    response = client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin,
                            json={"number": "+1 (415) 555-0184"})  # seeded pn-02
    assert response.status_code == 409


def test_edit_requires_super_admin(client, tenant_admin, super_admin):
    row = _get_first_test_row(client, super_admin)
    assert client.patch(f"{API}/phone-numbers/{row['id']}", headers=tenant_admin,
                        json={"country": "GB"}).status_code == 403


def test_edit_unknown_number_404(client, super_admin):
    assert client.patch(f"{API}/phone-numbers/pn_missing", headers=super_admin,
                        json={"country": "GB"}).status_code == 404


def test_assigned_status_requires_tenant(client, super_admin):
    row = _get_first_test_row(client, super_admin)
    response = client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin,
                            json={"status": "assigned"})
    assert response.status_code == 422


def _get_first_test_row(client, super_admin):
    rows = _data(client.get(f"{API}/phone-numbers", headers=super_admin))
    return next(r for r in rows if r["number"].startswith(f"+9198{_DIGITS}"))


# ── activate / deactivate ────────────────────────────────────────────────────


def test_deactivate_and_activate_persist(client, super_admin):
    row = _get_first_test_row(client, super_admin)
    off = _data(client.post(f"{API}/phone-numbers/{row['id']}/deactivate", headers=super_admin))
    assert off["isActive"] is False
    assert _get_row(client, super_admin, row["id"])["isActive"] is False
    # Idempotent second call.
    again = _data(client.post(f"{API}/phone-numbers/{row['id']}/deactivate", headers=super_admin))
    assert again["isActive"] is False
    on = _data(client.post(f"{API}/phone-numbers/{row['id']}/activate", headers=super_admin))
    assert on["isActive"] is True
    assert _get_row(client, super_admin, row["id"])["isActive"] is True


def test_toggle_requires_super_admin(client, tenant_admin, super_admin):
    row = _get_first_test_row(client, super_admin)
    assert client.post(f"{API}/phone-numbers/{row['id']}/deactivate",
                       headers=tenant_admin).status_code == 403


def test_deactivation_preserves_existing_assignment(client, super_admin, test_bot):
    row = _create(client, super_admin, f"+9197{_DIGITS}1",
                  tenantId="tn-001", botId=test_bot["id"])
    assert row["status"] == "assigned"
    off = _data(client.post(f"{API}/phone-numbers/{row['id']}/deactivate", headers=super_admin))
    assert off["isActive"] is False
    assert off["status"] == "assigned"  # assignment untouched
    assert off["bot"] is not None


def test_inactive_number_rejects_new_assignment(client, super_admin):
    row = _create(client, super_admin, f"+9197{_DIGITS}2")
    _data(client.post(f"{API}/phone-numbers/{row['id']}/deactivate", headers=super_admin))
    response = client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin,
                            json={"tenantId": "tn-001"})
    assert response.status_code == 409
    assert "inactive" in response.json()["message"].lower()


# ── channel claims ───────────────────────────────────────────────────────────


def test_channel_claim_rejects_inactive_number(client, super_admin, tenant_admin, test_bot):
    number = f"+9197{_DIGITS}3"
    row = _create(client, super_admin, number)
    _data(client.post(f"{API}/phone-numbers/{row['id']}/deactivate", headers=super_admin))

    response = client.put(f"{API}/bots/{test_bot['id']}/channels/voice", headers=tenant_admin, json={
        "config": {"phoneNumber": number, "telephonyProvider": "freeswitch"},
    })
    assert response.status_code == 409
    assert "deactivated" in response.json()["message"].lower()

    # Reactivated → the claim goes through and the row is assigned to the bot.
    _data(client.post(f"{API}/phone-numbers/{row['id']}/activate", headers=super_admin))
    saved = _data(client.put(f"{API}/bots/{test_bot['id']}/channels/voice", headers=tenant_admin, json={
        "config": {"phoneNumber": number, "telephonyProvider": "freeswitch"},
    }))
    assert saved["status"] == "configured"
    claimed = _get_row(client, super_admin, row["id"])
    assert claimed["status"] == "assigned" and claimed["bot"] is not None


def test_existing_claim_survives_deactivation(client, super_admin, tenant_admin, test_bot):
    """Deactivating a claimed number must not break editing that channel."""
    number = f"+9197{_DIGITS}3"  # claimed by test_bot in the previous test
    row = _get_row_by_number(client, super_admin, number)
    _data(client.post(f"{API}/phone-numbers/{row['id']}/deactivate", headers=super_admin))
    saved = _data(client.put(f"{API}/bots/{test_bot['id']}/channels/voice", headers=tenant_admin, json={
        "config": {"phoneNumber": number, "telephonyProvider": "freeswitch",
                   "language": "hi"},
    }))
    assert saved["config"]["language"] == "hi"


def test_claimed_number_cannot_be_edited_from_admin(client, super_admin, test_bot):
    number = f"+9197{_DIGITS}3"
    row = _get_row_by_number(client, super_admin, number)
    for body in ({"number": f"+9197{_DIGITS}9"}, {"tenantId": None}):
        response = client.patch(f"{API}/phone-numbers/{row['id']}", headers=super_admin, json=body)
        assert response.status_code == 409
        assert "channels tab" in response.json()["message"].lower()


def _get_row_by_number(client, super_admin, number):
    rows = _data(client.get(f"{API}/phone-numbers", headers=super_admin))
    return next(r for r in rows if r["number"] == number)


# ── bot metrics (Calls/Month + Cost/Call) ────────────────────────────────────


def test_bot_metrics_derive_from_metered_usage(client, tenant_admin, test_bot):
    from shared.db.mysql import get_sessionmaker
    from shared.ids import new_id
    from shared.models import UsageRecord

    session = get_sessionmaker()()
    try:
        today = date.today()
        # One rollup row per tenant+bot+date (unique key), as metering writes it.
        session.add(UsageRecord(
            id=new_id("ur"), tenant_id="tn-001", bot_id=test_bot["id"], date=today,
            calls=4, minutes=8, cost_llm=0.04, cost_tts=0.06, cost_stt=0.02,
            cost_telephony=0, cost_embedding=0,
        ))
        session.commit()
    finally:
        session.close()

    bots = _data(client.get(f"{API}/bots?tenantId=tn-001&pageSize=100", headers=tenant_admin))
    bot = next(b for b in bots if b["id"] == test_bot["id"])
    assert bot["callsMonth"] == 4
    # (0.03+0.05+0.02+0.01+0.01) / 4 calls = 0.03 — from usage_records, not
    # the static voice_bots.avg_cost_per_call column (which is 0 here).
    assert bot["avgCostPerCall"] == pytest.approx(0.03, abs=1e-6)
    assert bot["callsToday"] == 4
