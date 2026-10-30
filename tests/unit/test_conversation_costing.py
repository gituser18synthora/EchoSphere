"""Per-conversation costing: provider formulas, currency, and auditability.

The costing chain is: provider-reported usage → `usage_events` (quantities plus
a pricing snapshot of the exact rate applied) → the conversation's cached total
→ what the list and detail views render. A defect anywhere in it shows up as a
wrong number on a bill, so each link is pinned separately:

- the provider-specific formula per capability, computed against real
  configured rates rather than restated constants;
- Decimal exactness and the rounding actually applied;
- currency conversion through the stored rate, never a guessed one;
- replayed events costing once;
- the list total and the detail breakdown being the same number by
  construction;
- outlier detection, so an expensive call is flagged rather than blending in.
"""

from decimal import Decimal

from shared.billing.conversation_cost import HIGH_COST_USD, ConversationCost
from shared.models.billing_models import BASE_CURRENCY, UsageEvent
from shared.ids import new_id

# Rate-table-backed verification of each provider formula lives in
# tests/integration/test_conversation_costing_api.py — it needs the real
# provider_pricing rows. Everything here is pure logic.


# ── breakdown, reconciliation and outliers ──────────────────────────────────


def _event(capability, provider, model, cost, snapshot=None, **quantities):
    """An in-memory usage event; never added to a session."""
    return UsageEvent(
        id=new_id("ue"), tenant_id="tn-x", capability=capability,
        provider_code=provider, model_code=model,
        cost_usd=Decimal(str(cost)),
        pricing_status="priced" if snapshot else "missing_price",
        pricing_snapshot=snapshot,
        **quantities,
    )


def _costing(events) -> ConversationCost:
    """Build a ConversationCost from events without touching the database."""
    from shared.billing.conversation_cost import _lines_from_snapshot, _unpriced_line

    result = ConversationCost(session_id="vs_test")
    result.event_count = len(events)
    for event in events:
        lines = _lines_from_snapshot(event)
        for component in (event.pricing_snapshot or {}).get("missing") or []:
            result.unpriced.append(f"{event.capability}:{event.provider_code}:{component}")
            lines.append(_unpriced_line(event, component))
        result.lines.extend(lines)
        cost = Decimal(str(event.cost_usd))
        result.total_usd += cost
        result.by_capability[event.capability] = (
            result.by_capability.get(event.capability, Decimal(0)) + cost
        )
    result.total_usd = result.total_usd.quantize(Decimal("0.000001"))
    return result


TTS_SNAPSHOT = {
    "characters": {
        "priceId": "pp-1", "unit": "per_1k_characters", "unitPrice": "0.05",
        "currency": "USD", "fxRate": None, "quantity": "3544",
        "cost": "0.177200", "charge": "0",
    }
}
STT_SNAPSHOT = {
    "audio_seconds": {
        "priceId": "pp-2", "unit": "per_hour", "unitPrice": "30",
        "currency": "INR", "fxRate": "96.5", "quantity": "1.728",
        "cost": "0.000149", "charge": "0",
    }
}


class TestBreakdown:
    def test_lines_preserve_the_historical_rate_that_was_applied(self):
        # Re-pricing from today's table would restate history; the snapshot is
        # what makes a months-old cost auditable.
        costing = _costing([
            _event("tts", "elevenlabs", "eleven_flash_v2_5", "0.177200", TTS_SNAPSHOT),
        ])
        (line,) = costing.lines
        assert line.unit_price == Decimal("0.05")
        assert line.unit == "per_1k_characters"
        assert line.quantity == Decimal("3544")
        assert line.cost_usd == Decimal("0.177200")
        assert line.fx_rate is None

    def test_non_usd_rate_reports_both_the_rate_and_the_fx_applied(self):
        costing = _costing([_event("stt", "sarvam", "saaras:v3", "0.000149", STT_SNAPSHOT)])
        (line,) = costing.lines
        assert line.currency == "INR"
        assert line.unit_price == Decimal("30")
        assert line.fx_rate == Decimal("96.5")

    def test_total_is_the_sum_across_capabilities(self):
        costing = _costing([
            _event("tts", "elevenlabs", "eleven_flash_v2_5", "0.177200", TTS_SNAPSHOT),
            _event("stt", "sarvam", "saaras:v3", "0.000149", STT_SNAPSHOT),
        ])
        assert costing.total_usd == Decimal("0.177349")
        assert set(costing.by_capability) == {"tts", "stt"}

    def test_unpriced_usage_is_shown_at_zero_with_a_reason(self):
        # A configuration gap the reader must be able to see — an unpriced
        # component is not a free one.
        event = _event(
            "telephony", "freeswitch", "", "0",
            {"missing": ["call_seconds"]}, audio_seconds=Decimal("215"),
        )
        costing = _costing([event])
        (line,) = costing.lines
        assert line.priced is False
        assert line.quantity == Decimal("215")
        assert line.cost_usd == Decimal(0)
        assert line.note and "No active price" in line.note
        assert costing.unpriced == ["telephony:freeswitch:call_seconds"]

    def test_telephony_is_included_in_the_total_once_priced(self):
        snapshot = {
            "call_seconds": {
                "priceId": "pp-3", "unit": "per_minute", "unitPrice": "0.006",
                "currency": "USD", "fxRate": None, "quantity": "215",
                "cost": "0.021500", "charge": "0",
            }
        }
        costing = _costing([
            _event("tts", "elevenlabs", "eleven_flash_v2_5", "0.177200", TTS_SNAPSHOT),
            _event("telephony", "vaani", "", "0.021500", snapshot),
        ])
        assert costing.total_usd == Decimal("0.198700")
        assert "telephony" in costing.by_capability


class TestCurrencyConversion:
    def test_inr_conversion_uses_the_stored_rate_and_keeps_usd(self):
        costing = _costing([
            _event("tts", "elevenlabs", "eleven_flash_v2_5", "0.177200", TTS_SNAPSHOT),
        ])
        converted, currency = costing.display(currency="INR", rate=Decimal("96.5"))
        assert currency == "INR"
        assert converted == Decimal("17.099800")   # 0.1772 × 96.5, exact
        assert costing.total_usd == Decimal("0.177200")  # base cost unmoved

    def test_missing_rate_falls_back_to_usd_never_a_guess(self):
        costing = _costing([_event("tts", "elevenlabs", "x", "0.10", TTS_SNAPSHOT)])
        for rate in (None, Decimal(0), Decimal("-1")):
            converted, currency = costing.display(currency="EUR", rate=rate)
            assert currency == BASE_CURRENCY
            assert converted == costing.total_usd

    def test_base_currency_needs_no_rate(self):
        costing = _costing([_event("tts", "elevenlabs", "x", "0.10", TTS_SNAPSHOT)])
        converted, currency = costing.display(currency=BASE_CURRENCY, rate=None)
        assert (converted, currency) == (costing.total_usd, BASE_CURRENCY)

    def test_serialized_payload_labels_currency_and_rate(self):
        costing = _costing([_event("stt", "sarvam", "saaras:v3", "0.000149", STT_SNAPSHOT)])
        payload = costing.as_dict(currency="INR", rate=Decimal("96.5"))
        assert payload["baseCurrency"] == "USD"
        assert payload["displayCurrency"] == "INR"
        assert payload["displayRate"] == "96.5"
        assert payload["totalUsd"] == "0.000149"
        # Amounts cross the wire as strings: a float would lose the precision
        # the Decimal pipeline exists to preserve.
        assert isinstance(payload["totalUsd"], str)
        assert isinstance(payload["lines"][0]["unitPrice"], str)


class TestHighCostDetection:
    def test_call_above_the_threshold_is_flagged(self):
        costing = _costing([_event("tts", "elevenlabs", "x", "0.75", TTS_SNAPSHOT)])
        assert costing.high_cost is True
        assert costing.as_dict()["highCost"] is True

    def test_ordinary_call_is_not_flagged(self):
        costing = _costing([_event("tts", "elevenlabs", "x", "0.1811", TTS_SNAPSHOT)])
        assert costing.high_cost is False

    def test_threshold_is_exclusive_and_published(self):
        assert _costing([_event("tts", "e", "x", HIGH_COST_USD, TTS_SNAPSHOT)]).high_cost is False
        payload = _costing([_event("tts", "e", "x", "0.1", TTS_SNAPSHOT)]).as_dict()
        assert payload["highCostThresholdUsd"] == str(HIGH_COST_USD)

    def test_flagging_never_alters_the_arithmetic(self):
        costing = _costing([_event("tts", "elevenlabs", "x", "9.99", TTS_SNAPSHOT)])
        assert costing.high_cost is True
        assert costing.total_usd == Decimal("9.990000")  # not capped, not hidden


class TestEmptyAndMissingLinks:
    def test_conversation_with_no_session_link_returns_empty_costing(self):
        from shared.billing.conversation_cost import conversation_cost

        # No session link (historical row): a data gap, not an error — and it
        # must not touch the database looking for events that cannot exist.
        costing = conversation_cost(None, None)
        assert costing.total_usd == Decimal(0)
        assert costing.event_count == 0
        assert costing.lines == []

    def test_empty_costing_still_serializes_and_converts(self):
        costing = ConversationCost(session_id=None)
        payload = costing.as_dict(currency="INR", rate=Decimal("96.5"))
        assert payload["totalUsd"] == "0"
        assert payload["displayTotal"] == "0.000000"
        assert payload["lines"] == []
        assert payload["highCost"] is False
