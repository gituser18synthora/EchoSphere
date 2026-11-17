"""Regression evidence from split Hindi/English identifier test calls."""
import pytest

from shared.orchestration.entity_extractor import extract_entity
from shared.orchestration.identifier_confirmation import (
    amend_identifier_suffix,
    confirmation_answer,
    identifier_sequence,
    identifier_suffix,
    spoken_identifier_text,
)


ENTITY = {
    "dataType": "identifier", "preserveAlphanumeric": True,
    "regexPattern": r"(?<![A-Za-z0-9])([A-Za-z]?\d{7,8}[A-Za-z]?)(?![A-Za-z0-9])",
    "synonymPatterns": {"no_tid": [r"I do not have it", r"पता नहीं"]},
}
NODE = {"config": {"entity": ENTITY}}


@pytest.mark.parametrize("text,expected", [
    ("416672 5 MM for mango", "4166725M"),
    ("1234567 and B, B for boy", "1234567B"),
    ("1234567 and B for boy please", "1234567B"),
    ("मेरा टर्मिनल आईडी है १ २ ३ ४ ५ ६ ७ and B, B for boy.", "1234567B"),
    ("A for Alpha one two three four five six seven", "A1234567"),
    ("ए फोर अल्फा एक दो तीन चार पाँच छह सात", "A1234567"),
    ("एक दो तीन चार पाँच छह सात और बी फॉर बॉय", "1234567B"),
    ("चार एक छह छह सात दो पाँच एम फोर मैंगो", "4166725M"),
    ("मेरा टर्मिनल आई टी है वन वन थ्री टू नाइन एट सिक्स ए फोर अल्फा।", "1132986A"),
    ("चार एक छह छह सात दो पाँच एम फोर मैंगो॥", "4166725M"),
])
def test_complete_spelling_is_preserved_before_digit_normalization(text, expected):
    assert identifier_sequence(text) == expected
    assert extract_entity(text, ENTITY)["value"] == expected


@pytest.mark.parametrize("text", [
    "1234", "एक दो तीन चार पाँच", "123456789", "1234567BB",
    "1234567 double B", "1234567 double B for boy", "1234567 and double B",
    "1234567 B for Alpha", "1234567 बी फोर अल्फा",
    "1234567 B, C for Cat",
])
def test_authored_identifier_pattern_cannot_fall_back_or_drop_ambiguous_suffix(text):
    result = extract_entity(text, ENTITY)
    assert not result["matched"] and result["value"] is None


def test_invalid_length_from_actual_call_retains_letter_but_fails_validation():
    text = "मेरा आईडी टर्मिनल आईडी है १ २ ३ ४ and B, B for boy."
    assert identifier_sequence(text) == "1234B"
    assert not extract_entity(text, ENTITY)["matched"]


@pytest.mark.parametrize("text,expected", [
    ("B", "B"), ("बी", "B"), ("and B, B for Boy", "B"),
    ("MM for mango", "M"), ("और एम फोर मैंगो", "M"),
    ("letter A as in Alpha", "A"), ("double B", None), ("double B for boy", None),
    ("B B", None), ("B for Apple", None), ("B, C for Cat", None),
    ("B for Boy?", None), ("M for Mango please", None), ("Mango", None),
    ("no B", None), ("1234567B", None),
])
def test_only_unambiguous_single_letter_continuations_are_suffixes(text, expected):
    assert identifier_suffix(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Okay yes it's correct", "yes"), ("It’s correct.", "yes"),
    ("that's right", "yes"), ("yes, correct", "yes"), ("haan sahi hai", "yes"),
    ("confirmed", "yes"), ("Yes, confirmed", "yes"),
    ("no", "no"), ("not correct", "no"), ("yes, it's not correct", "no"),
    ("okay yes it isn't correct", "no"),
    ("haan nahi", "no"), ("okay", None), ("I think it's correct", None),
    ("no I need help", None), ("yes but the last letter is B", None),
    ("yes actually 1234567B", None), ("is it correct?", None),
    ("isn't it correct", None),
    ("It's correct?", None), ("why is it correct", None), ("yes please book it", None),
])
def test_safe_natural_confirmation_cannot_be_inferred_from_arbitrary_prose(text, expected):
    assert confirmation_answer(text) == expected


@pytest.mark.parametrize("previous,text,expected", [
    ("1234567", "and B, B for Boy", "1234567B"),
    ("4166725A", "MM for mango", "4166725M"),
    ("12345", "B for Boy", None),
    ("1234567AB", "C for Cat", None),
    ("HONASA", "B for Boy", None),
    ("1234567", "B for Alpha", None),
    ("1234567", "double B", None),
])
def test_suffix_amendment_requires_entire_schema_valid_id(previous, text, expected):
    assert amend_identifier_suffix(NODE, "terminal_id", previous, text) == expected


def test_suffix_cannot_change_phone_or_numeric_only_matcher():
    phone = {"config": {"entity": {"dataType": "phone"}}}
    numeric = {"config": {"entity": {"dataType": "identifier", "regexPattern": r"^\d{7}$"}}}
    assert amend_identifier_suffix(phone, "phone", "9876543210", "B for Boy") is None
    assert amend_identifier_suffix(numeric, "booking_id", "1234567", "B for Boy") is None


def test_explicit_double_letters_are_preserved_when_schema_allows_them():
    entity = {"dataType": "identifier", "regexPattern": r"^\d{7}[A-Z]{2}$"}
    assert spoken_identifier_text("1234567 double B for boy") == "1234567BB"
    assert extract_entity("1234567 double B", entity)["value"] == "1234567BB"
    assert extract_entity("1234567 B B", entity)["value"] == "1234567BB"
    assert extract_entity("1234567 and double B", entity)["value"] == "1234567BB"


def test_ordinary_english_subject_after_connector_does_not_become_a_letter():
    assert extract_entity("1234567 and I need help", ENTITY)["value"] == "1234567"


@pytest.mark.parametrize("text", ["I do not have it", "पता नहीं"])
def test_strict_identifier_matching_preserves_explicit_unavailable_outcomes(text):
    assert extract_entity(text, ENTITY)["value"] == "no_tid"


def test_non_identifier_builtin_fallback_behavior_is_unchanged():
    entity = {"dataType": "integer", "regexPattern": r"^never_matches$"}
    assert extract_entity("12345", entity)["value"] == "12345"


@pytest.mark.parametrize("text", ["987654321", "98765432101"])
def test_authored_phone_length_does_not_fall_back_to_generic_phone(text):
    assert not extract_entity(text, {"dataType": "phone", "regexPattern": r"^\d{10}$"})["matched"]
