"""Conversation AI-summary serializer: structured fields come out in the bot's
configured order with display labels — never in MySQL's normalised JSON key
order (by length, then alphabetically), which put "call cx" first."""

from types import SimpleNamespace

from backend.serializers import _ordered_structured_fields, serialize_conversation_memory

STORED = {  # what MySQL hands back: keys sorted by length, not by config
    "call_cx": None,
    "hand_over_to": "mother",
    "call_customer": "Yes",
    "hand_over_product": "Yes",
    "reach_customer_location": "Yes",
}
SPECS = [
    {"name": "reach_customer_location", "label": "Reached customer location"},
    {"name": "call_customer", "label": "Called the customer"},
    {"name": "hand_over_product", "label": "Product handed over"},
    {"name": "hand_over_to", "label": "Handed over to"},
    {"name": "call_cx", "label": "CX support call received"},
]


def _memory_row(**overrides):
    base = dict(
        status="completed", call_outcome="delivered", summary="ok",
        memory={"structured_fields": STORED,
                "structured_field_sources": {"call_customer": "workflow"}},
        next_best_action=None, next_action=None, follow_up_required=False,
        follow_up_at=None, confidence=None, generated_at=None, error=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_configured_order_wins_over_stored_key_order():
    fields, labels = _ordered_structured_fields({"structured_fields": STORED}, SPECS)
    assert list(fields) == [s["name"] for s in SPECS]
    assert fields["hand_over_to"] == "mother" and fields["call_cx"] is None
    assert labels["call_cx"] == "CX support call received"


def test_fields_dropped_from_config_keep_their_values_at_the_end():
    fields, labels = _ordered_structured_fields(
        {"structured_fields": STORED}, SPECS[:2]
    )
    assert list(fields)[:2] == ["reach_customer_location", "call_customer"]
    assert set(fields) == set(STORED)          # nothing recorded is lost
    assert set(labels) == {"reach_customer_location", "call_customer"}


def test_configured_field_missing_from_the_row_is_not_invented():
    fields, _labels = _ordered_structured_fields(
        {"structured_fields": {"call_cx": "No"}}, SPECS
    )
    assert fields == {"call_cx": "No"}


def test_serializer_without_specs_keeps_stored_order_and_no_labels():
    payload = serialize_conversation_memory(_memory_row())
    assert list(payload["structuredFields"]) == list(STORED)
    assert payload["structuredFieldLabels"] == {}


def test_serializer_with_specs_orders_and_labels():
    payload = serialize_conversation_memory(_memory_row(), SPECS)
    assert list(payload["structuredFields"]) == [s["name"] for s in SPECS]
    assert payload["structuredFieldLabels"]["hand_over_to"] == "Handed over to"
    assert payload["structuredFieldSources"] == {"call_customer": "workflow"}


def test_malformed_specs_are_ignored():
    fields, labels = _ordered_structured_fields(
        {"structured_fields": STORED}, [None, "x", {"label": "no name"}, {"name": "  "}]
    )
    assert set(fields) == set(STORED) and labels == {}


DATE_SPECS = [
    {"name": "appointment_date", "label": "Pickup date", "type": "date"},
    {"name": "appointment_time", "label": "Pickup time", "type": "time", "timezone": "Asia/Kolkata"},
    {"name": "note", "label": "Note", "type": "text"},
]


def test_serializer_adds_display_forms_for_normalized_date_time_fields():
    stored = {"appointment_date": "2026-10-20", "appointment_time": "17:00", "note": "2026-10-20"}
    payload = serialize_conversation_memory(_memory_row(memory={"structured_fields": stored}), DATE_SPECS)
    assert payload["structuredFields"] == stored                       # values stay machine-readable
    assert payload["structuredFieldDisplay"] == {"appointment_date": "20 Oct 2026",
                                                 "appointment_time": "05:00 PM IST"}


def test_display_is_empty_without_specs_or_values():
    assert serialize_conversation_memory(_memory_row())["structuredFieldDisplay"] == {}
    payload = serialize_conversation_memory(
        _memory_row(memory={"structured_fields": {"appointment_date": None, "appointment_time": "evening"}}),
        DATE_SPECS)
    assert payload["structuredFieldDisplay"] == {"appointment_time": "Evening (time window)"}
