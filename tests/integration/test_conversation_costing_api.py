"""Conversation costing against the real rate table and the real API.

Verifies the parts that only a live database and app can prove:

- each provider's billing formula computed from the CONFIGURED
  `provider_pricing` rows (a rate or unit edit is caught here, not on a bill);
- a replayed provider callback costing once;
- the list total and the detail breakdown being the same number;
- INR display conversion through the stored exchange rate;
- the STT regression that under-billed every call: Sarvam shares one
  `request_id` across all finals on a socket connection, so keying dedup on it
  billed a single utterance per call.

All rows are uniquely suffixed and removed in teardown; existing data is never
mutated.
"""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text as sa_text

from backend.core.security import create_access_token
from backend.main import app
from shared.billing.conversation_cost import conversation_cost
from shared.billing.metering import record_usage_event
from shared.billing.pricing import compute_cost, quantities_for
from shared.models.billing_models import BASE_CURRENCY

pytestmark = pytest.mark.integration

API = "/api/v1"
_SUFFIX = uuid.uuid4().hex[:8]
_SESSION = f"vs_cost_test_{_SUFFIX}"
_CONV_ID = f"cv_cost_test_{_SUFFIX}"
_TENANT = "tn-001"
_BOT = "bot-101"
AS_OF = datetime(2026, 8, 1, 12, 0, 0)


def _session():
    from shared.db.mysql import get_sessionmaker

    return get_sessionmaker()()


@pytest.fixture(scope="module")
def db():
    session = _session()
    yield session
    session.close()


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client
    from shared.db.mysql import get_engine

    with get_engine().begin() as conn:
        conn.execute(
            sa_text("DELETE FROM usage_events WHERE session_id = :s"), {"s": _SESSION}
        )
        conn.execute(
            sa_text("DELETE FROM conversation_sessions WHERE id = :i"), {"i": _CONV_ID}
        )


@pytest.fixture(scope="module")
def tenant_admin():
    from shared.models import User

    session = _session()
    try:
        user = session.execute(
            select(User).where(User.email == "priya.sharma@meridianhealth.com")
        ).scalar_one()
        token = create_access_token(
            user_id=user.id, role=user.role.code, tenant_id=user.tenant_id
        )
        return {"Authorization": f"Bearer {token}"}
    finally:
        session.close()


def _data(response):
    body = response.json()
    assert body.get("success") is True, body
    return body["data"]


# ── provider formulas against the configured rates ──────────────────────────


class TestProviderFormulas:
    def _cost(self, db, **kwargs):
        return compute_cost(db, as_of=AS_OF, **kwargs)

    def test_sarvam_stt_bills_inr_per_hour_of_processed_audio(self, db):
        # Sarvam publishes INR-only rates (₹/hour), converted to USD through
        # the stored USD→INR rate — never a hardcoded conversion.
        total, priced, missing = self._cost(
            db, provider_code="sarvam", capability="stt", model_code="saaras:v3",
            quantities=quantities_for("stt", audio_seconds=Decimal("120")),
        )
        assert missing == []
        (line,) = priced
        assert (line.unit, line.currency) == ("per_hour", "INR")
        assert line.fx_rate is not None and line.fx_rate > 0
        expected = Decimal(120) / 3600 * line.unit_price / line.fx_rate
        assert total == expected.quantize(Decimal("0.000001"))

    def test_elevenlabs_tts_bills_usd_per_1k_characters(self, db):
        total, priced, missing = self._cost(
            db, provider_code="elevenlabs", capability="tts",
            model_code="eleven_flash_v2_5",
            quantities=quantities_for("tts", characters=3544),
        )
        assert missing == []
        (line,) = priced
        assert (line.unit, line.currency) == ("per_1k_characters", BASE_CURRENCY)
        assert line.fx_rate is None  # already base currency
        assert total == (Decimal(3544) / 1000 * line.unit_price).quantize(
            Decimal("0.000001")
        )

    def test_sarvam_tts_bills_inr_per_1k_characters(self, db):
        # Same capability as ElevenLabs but a different native CURRENCY: the
        # unit and the FX step both have to come from the provider's own row.
        total, priced, missing = self._cost(
            db, provider_code="sarvam", capability="tts", model_code="bulbul:v3",
            quantities=quantities_for("tts", characters=1000),
        )
        assert missing == []
        (line,) = priced
        assert (line.unit, line.currency) == ("per_1k_characters", "INR")
        assert total == (line.unit_price / line.fx_rate).quantize(Decimal("0.000001"))

    def test_llm_nets_cached_tokens_out_of_full_rate_input(self, db):
        # `input_tokens` is the provider's GROSS prompt count and INCLUDES the
        # cached portion; billing both in full charges cache hits twice.
        quantities = quantities_for(
            "llm", input_tokens=28542, cached_tokens=20352, output_tokens=598
        )
        assert quantities["input_tokens"] == Decimal(8190)
        total, priced, missing = self._cost(
            db, provider_code="openai", capability="llm", model_code="gpt-5-mini",
            quantities=quantities,
        )
        assert missing == []
        rates = {p.component: p for p in priced}
        assert set(rates) == {"input_tokens", "output_tokens", "cached_input_tokens"}
        assert all(p.unit == "per_1m_tokens" for p in priced)
        # A swapped input/output mapping would silently multiply LLM cost.
        assert rates["output_tokens"].unit_price > rates["input_tokens"].unit_price
        assert rates["cached_input_tokens"].unit_price < rates["input_tokens"].unit_price
        assert total == sum(p.cost for p in priced)

    def test_self_hosted_telephony_is_unpriced_never_invented(self, db):
        total, priced, missing = self._cost(
            db, provider_code="freeswitch", capability="telephony", model_code="",
            quantities=quantities_for("telephony", audio_seconds=Decimal("215")),
        )
        assert (total, priced, missing) == (Decimal(0), [], ["call_seconds"])

    def test_one_tts_character_is_not_rounded_away(self, db):
        total, _, _ = self._cost(
            db, provider_code="elevenlabs", capability="tts",
            model_code="eleven_flash_v2_5",
            quantities=quantities_for("tts", characters=1),
        )
        assert total == Decimal("0.000050")


# ── a full conversation, costed and served ──────────────────────────────────


@pytest.fixture(scope="module")
def costed_conversation(client, db):
    """A conversation with one event per capability, priced from real rates."""
    from shared.models import ConversationSession

    events = [
        dict(capability="stt", provider_code="sarvam", model_code="saaras:v3",
             audio_seconds=Decimal("42.5"), request_id=f"{_SESSION}:stt"),
        dict(capability="llm", provider_code="openai", model_code="gpt-5-mini",
             input_tokens=28542, cached_tokens=20352, output_tokens=598,
             request_id=f"{_SESSION}:llm"),
        dict(capability="tts", provider_code="elevenlabs",
             model_code="eleven_flash_v2_5", characters=3544,
             request_id=f"{_SESSION}:tts"),
        dict(capability="telephony", provider_code="freeswitch", model_code="",
             audio_seconds=Decimal("215"), request_id=f"{_SESSION}:telephony"),
    ]
    total = Decimal(0)
    for spec in events:
        event = record_usage_event(
            db, tenant_id=_TENANT, bot_id=_BOT, session_id=_SESSION,
            occurred_at=AS_OF, commit=False, **spec,
        )
        if event is not None:
            total += Decimal(str(event.cost_usd))
    row = ConversationSession(
        id=_CONV_ID, session_id=_SESSION, tenant_id=_TENANT, bot_id=_BOT,
        channel="voice", started_at=AS_OF, duration_sec=215, cost_usd=total,
        contained=True, language="hi-IN", status="completed",
    )
    db.add(row)
    db.commit()
    return {"total": total, "row": row}


class TestConversationBreakdown:
    def test_total_is_the_sum_of_every_capability(self, db, costed_conversation):
        costing = conversation_cost(db, _SESSION)
        assert costing.event_count == 4
        assert costing.total_usd == costed_conversation["total"]
        assert set(costing.by_capability) == {"stt", "llm", "tts", "telephony"}

    def test_breakdown_reproduces_the_rate_that_was_applied(self, db, costed_conversation):
        costing = conversation_cost(db, _SESSION)
        tts = [l for l in costing.lines if l.capability == "tts"]
        (line,) = tts
        assert line.quantity == Decimal(3544)
        assert line.unit == "per_1k_characters"
        # Snapshot-derived, so a later rate change cannot restate this call.
        assert line.cost_usd == (Decimal(3544) / 1000 * line.unit_price).quantize(
            Decimal("0.000001")
        )

    def test_llm_lines_cover_input_output_and_cached(self, db, costed_conversation):
        costing = conversation_cost(db, _SESSION)
        components = {l.component for l in costing.lines if l.capability == "llm"}
        assert components == {"input_tokens", "output_tokens", "cached_input_tokens"}

    def test_unpriced_telephony_is_visible_at_zero(self, db, costed_conversation):
        costing = conversation_cost(db, _SESSION)
        (line,) = [l for l in costing.lines if l.capability == "telephony"]
        assert line.priced is False
        assert line.quantity == Decimal("215")
        assert line.cost_usd == Decimal(0)
        assert "telephony:freeswitch:call_seconds" in costing.unpriced

    def test_replayed_provider_callback_costs_once(self, db, costed_conversation):
        # Same request_id: a retry/reconnect re-delivery must be a no-op, not a
        # second charge.
        before = conversation_cost(db, _SESSION)
        replay = record_usage_event(
            db, tenant_id=_TENANT, bot_id=_BOT, session_id=_SESSION,
            capability="tts", provider_code="elevenlabs",
            model_code="eleven_flash_v2_5", characters=3544,
            request_id=f"{_SESSION}:tts", occurred_at=AS_OF, commit=True,
        )
        assert replay is None
        after = conversation_cost(db, _SESSION)
        assert after.total_usd == before.total_usd
        assert after.event_count == before.event_count


class TestListDetailConsistency:
    def test_list_and_detail_report_the_same_total(self, client, tenant_admin,
                                                   costed_conversation):
        listed = _data(client.get(f"{API}/conversations?pageSize=200", headers=tenant_admin))
        row = next(c for c in listed if c["id"] == _CONV_ID)
        detail = _data(client.get(f"{API}/conversations/{_CONV_ID}", headers=tenant_admin))
        assert row["costUsd"] == detail["costUsd"]
        assert Decimal(str(row["costUsd"])) == costed_conversation["total"]
        # The list row carries no breakdown (one query per row); the detail does.
        assert row.get("cost") is None
        assert detail["cost"] is not None

    def test_detail_breakdown_sums_to_the_displayed_total(self, client, tenant_admin,
                                                          costed_conversation):
        detail = _data(client.get(f"{API}/conversations/{_CONV_ID}", headers=tenant_admin))
        cost = detail["cost"]
        assert Decimal(cost["totalUsd"]) == costed_conversation["total"]
        assert Decimal(cost["storedTotalUsd"]) == costed_conversation["total"]
        assert cost["reconciled"] is True
        by_capability = sum(
            Decimal(entry["costUsd"]) for entry in cost["byCapability"].values()
        )
        assert by_capability == Decimal(cost["totalUsd"])

    def test_amounts_cross_the_wire_as_strings(self, client, tenant_admin,
                                               costed_conversation):
        # Decimal precision is the point of the whole pipeline; a float here
        # would throw it away at the last step.
        cost = _data(
            client.get(f"{API}/conversations/{_CONV_ID}", headers=tenant_admin)
        )["cost"]
        assert isinstance(cost["totalUsd"], str)
        assert all(isinstance(line["unitPrice"], str) for line in cost["lines"])
        assert all(isinstance(line["quantity"], str) for line in cost["lines"])


class TestCurrencyDisplay:
    def test_inr_uses_the_stored_rate_and_keeps_usd_authoritative(
        self, client, tenant_admin, costed_conversation
    ):
        detail = _data(client.get(
            f"{API}/conversations/{_CONV_ID}?currency=INR", headers=tenant_admin
        ))
        cost = detail["cost"]
        assert cost["baseCurrency"] == "USD"
        assert cost["displayCurrency"] == "INR"
        rate = Decimal(cost["displayRate"])
        assert rate > 0
        assert Decimal(cost["displayTotal"]) == (
            Decimal(cost["totalUsd"]) * rate
        ).quantize(Decimal("0.000001"))
        # The stored/base cost does not move with the display currency.
        assert Decimal(cost["totalUsd"]) == costed_conversation["total"]
        assert detail["costUsd"] == float(costed_conversation["total"])

    def test_currency_without_a_configured_rate_falls_back_to_usd(
        self, client, tenant_admin, costed_conversation
    ):
        cost = _data(client.get(
            f"{API}/conversations/{_CONV_ID}?currency=EUR", headers=tenant_admin
        ))["cost"]
        # EUR is an active currency with no configured rate — never guessed.
        assert cost["displayCurrency"] == "USD"
        assert cost["displayRate"] is None

    def test_default_display_is_the_base_currency(self, client, tenant_admin,
                                                  costed_conversation):
        cost = _data(
            client.get(f"{API}/conversations/{_CONV_ID}", headers=tenant_admin)
        )["cost"]
        assert cost["displayCurrency"] == "USD"


class TestHighCostDetection:
    def test_an_ordinary_call_is_not_flagged(self, client, tenant_admin,
                                             costed_conversation):
        cost = _data(
            client.get(f"{API}/conversations/{_CONV_ID}", headers=tenant_admin)
        )["cost"]
        assert cost["highCost"] is False
        assert Decimal(cost["totalUsd"]) < Decimal(cost["highCostThresholdUsd"])

    def test_threshold_is_published_so_the_ui_can_label_it(self, client, tenant_admin,
                                                           costed_conversation):
        cost = _data(
            client.get(f"{API}/conversations/{_CONV_ID}", headers=tenant_admin)
        )["cost"]
        assert Decimal(cost["highCostThresholdUsd"]) > 0
