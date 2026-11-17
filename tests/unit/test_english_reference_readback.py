"""Reference formatting is explicit, language-independent and presentation-only."""
import copy

import pytest

from shared.orchestration.identifier_confirmation import spoken_readback
from shared.orchestration.spoken_numbers import english_identifier_readback
from shared.orchestration.workflow_engine import render_readback


@pytest.mark.parametrize("value,expected", [
    ("1234", "one, two, three, four"),
    ("12345", "one, two, three, four, five"),
    ("1132986A4", "one, one, three, two, nine, eight, six, letter A, four"),
    ("0012345B", "zero, zero, one, two, three, four, five, letter B"),
    ("११३२९८६A", "one, one, three, two, nine, eight, six, letter A"),
    ("+91 0012345678", "plus, nine, one, zero, zero, one, two, three, four, five, six, seven, eight"),
    ("BK-००१/A", "letter B, letter K, hyphen, zero, zero, one, slash, letter A"),
])
def test_general_formatter_preserves_unknown_values_zeroes_letters_and_script_digits(value, expected):
    assert english_identifier_readback(value) == expected
    assert spoken_readback(value) == expected


@pytest.mark.parametrize("language", ["en", "hi", "ta"])
def test_unknown_identifier_never_uses_lookup_table_or_language_for_digits(language):
    localized = {"fields": [{"variable": "tid", "format": "identifier",
                              "template": "Terminal ID {value}.",
                              "values": {"0012345B": "Incorrect table speech"}}],
                 "question": "क्या यह सही है?" if language == "hi" else "Is that correct?"}
    text = render_readback(localized, {"tid": "0012345B"})
    assert text.startswith("Terminal ID zero, zero, one, two, three, four, five, letter B.")
    assert "0012345B" not in text and "Incorrect" not in text


@pytest.mark.parametrize("language", ["en", "hi"])
@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("with_person", [False, True])
def test_contact_field_and_group_templates_use_english_digits_and_keep_raw_conditions(language, grouped, with_person):
    fields = [{"variable": "contact_number", "template": "Contact number {value}."}]
    groups = [
        {"requires": ["contact_person", "contact_number"],
         "template": "Contact person {contact_person}, contact number {contact_number}."},
        {"absent": ["contact_person"], "requires": ["contact_number"],
         "template": "Contact number {contact_number}."},
    ]
    localized = {"formats": {"contact_number": "phone"},
                 "fields": [] if grouped else fields, "groups": groups if grouped else [],
                 "question": "क्या यह सही है?" if language == "hi" else "Is that correct?"}
    slots = {"contact_number": "0012345678"}
    if with_person:
        slots["contact_person"] = "Agent 42"
    frozen = copy.deepcopy(slots)
    text = render_readback(localized, slots)
    assert text.count("zero, zero, one, two, three, four, five, six, seven, eight") == 1
    assert "0012345678" not in text
    if grouped and with_person:
        assert "Agent 42" in text  # Person names and unrelated numbers stay unchanged.
    assert slots == frozen


def test_explicit_format_map_does_not_change_models_amounts_quantities_or_addresses():
    localized = {"formats": {"terminal_id": "identifier", "contact_number": "phone"},
        "groups": [{"requires": ["terminal_id", "contact_number", "amount", "quantity"],
                    "template": "ID {terminal_id}; phone {contact_number}; model {asset_type}; "
                                "amount {amount}; quantity {quantity}; address {address}; diff {diff:amount,paid}."}]}
    slots = {"terminal_id": "0012345B", "contact_number": "9876543210",
             "asset_type": "Linura 7220", "amount": "2500", "paid": "2000",
             "quantity": "12", "address": "Plot 34, Floor 2"}
    frozen = copy.deepcopy(slots)
    text = render_readback(localized, slots)
    assert "model Linura 7220; amount 2500; quantity 12; address Plot 34, Floor 2; diff 500." in text
    assert slots == frozen


def test_unmarked_numeric_fields_preserve_existing_readback_behavior():
    localized = {"fields": [{"variable": "amount", "template": "Amount {value}."}]}
    assert render_readback(localized, {"amount": "1234"}) == "Amount 1234."
