"""Exact dictation and control turns from Demo call cv_0a537c60c4d4.

Recording retranscription variants exercise equivalent scripts, without
claiming that a second STT pass establishes the recording's ground truth.
"""
import pytest

from shared.orchestration.entity_extractor import extract_entity
from shared.orchestration.identifier_confirmation import (
    confirmation_answer,
    identifier_dictation,
    identifier_sequence,
    identifier_suffix,
    identifier_suffix_repeated,
    spoken_identifier_text,
)


ENTITY = {
    "dataType": "identifier", "preserveAlphanumeric": True,
    "regexPattern": r"(?<![A-Za-z0-9])([A-Za-z]?\d{7,8}[A-Za-z]?)(?![A-Za-z0-9])",
    "synonymPatterns": {"no_tid": [r"I do not have it", r"पता नहीं"]},
}


@pytest.mark.parametrize("text,expected", [
    ("मेरी ID है 123 B, B for boy.", "123B"),
    ("मेरी आईडी है १ २ ३ बी, बी फॉर बॉय।", "123B"),
    ("मैंने अभी बोला ना बी फॉर बॉल।", "B"),
    ("मैंने अभी बोला ना बी फॉर बॉल", "B"),
    ("One Two Three Four B, B for ball.", "1234B"),
    ("वन टू थ्री फोर बी बी फॉर बॉल", "1234B"),
    ("One", "1"),
    ("एक दो तीन चार B for ball, paanch, chha, saat.", "1234B567"),
    ("एक दो तीन चार बी फॉर बॉल पांच छह सात", "1234B567"),
])
def test_exact_call_and_retranscribed_dictation_is_preserved(text, expected):
    assert identifier_dictation(text)
    assert identifier_sequence(text) == expected
    # These are partial/invalid IDs in the configured schema. A successful
    # dictation parse must not waive the 7–8 digits and outer-letter rules.
    assert not extract_entity(text, ENTITY)["matched"]


@pytest.mark.parametrize("text", [
    "कौन सा नोट किया है फिर से बोलिए एक बार।",
    "क्या नोट किया है बताओ तो इसे",
    "Wait", "बस इतने ही हैं।",
    "एक मिनट रुकिए", "एक दो रुकिए", "one two wait",
    "one two help", "please read one two back", "फिर से बोलिए एक बार।",
    "read that back", "जी", "yes", "no", "नहीं, ये सही नहीं है।",
])
def test_questions_and_control_prose_cannot_be_buffered_as_identifier_digits(text):
    assert not identifier_dictation(text)
    assert identifier_suffix(text) is None
    assert not identifier_suffix_repeated(text)


@pytest.mark.parametrize("text", ["बोलिए", "बताओ", "रुकिए", "समझाइए", "सुनाइए", "कहिए", "आइए"])
def test_hindi_letter_names_never_match_inside_words_or_vowel_marks(text):
    assert spoken_identifier_text(text) == text
    assert identifier_sequence(text) == ""
    assert not identifier_dictation(text)


@pytest.mark.parametrize("text,suffix", [
    ("मैंने अभी बोला ना बी फॉर बॉल।", "B"),
    ("मैंने अभी बोला ना बी फॉर बॉल", "B"),
    ("मैंने कहा बी", "B"),
    ("I just said B, B for Boy", "B"),
    ("maine abhi bola na B for Boy", "B"),
])
def test_single_letter_repetition_requires_an_explicit_carrier_and_clear_suffix(text, suffix):
    assert identifier_suffix(text) == suffix
    assert identifier_suffix_repeated(text)
    assert identifier_dictation(text)
    assert identifier_sequence(text) == suffix


@pytest.mark.parametrize("text,expected", [
    ("double B", "BB"), ("triple B", "BBB"), ("B B", "BB"), ("बी बी", "BB"),
    ("मैंने अभी बोला ना double B", "BB"),
    ("मैंने अभी बोला ना डबल बी फॉर बॉय", "BB"),
])
def test_explicit_double_letters_are_not_collapsed_as_a_repeated_single_suffix(text, expected):
    assert identifier_sequence(text) == expected
    assert identifier_dictation(text)
    assert identifier_suffix(text) is None
    assert not identifier_suffix_repeated(text)


@pytest.mark.parametrize("text", [
    "मैंने अभी बोला ना B for Apple", "I just said B, C for Cat",
    "मैंने अभी बोला ना मुझे मदद चाहिए", "I just said wait one minute",
    "I just said B for Boy?", "B for Boy", "बी", "HONASA",
])
def test_repetition_marker_is_not_inferred_from_mismatches_questions_or_unrelated_prose(text):
    assert not identifier_suffix_repeated(text)


@pytest.mark.parametrize("text", [
    "start again one two three four B for ball five six seven",
    "फिर से एक दो तीन चार बी फॉर बॉल पांच छह सात",
    "wrong — one two three four B for ball five six seven",
])
def test_explicit_restart_carrier_keeps_the_redictated_payload(text):
    assert identifier_dictation(text)
    assert identifier_sequence(text) == "1234B567"
    assert not extract_entity(text, ENTITY)["matched"]


@pytest.mark.parametrize("text,expected", [
    ("my booking ID is one two three", "123"),
    ("मेरी शिकायत आईडी है एक दो तीन", "123"),
    ("मेरा टर्मिनल आई टी है वन वन थ्री टू नाइन एट सिक्स ए फोर अल्फा।", "1132986A"),
    ("yes, um one two", "12"),
    ("1234567 जी", "1234567G"),
    ("जी फॉर गोट", "G"),
])
def test_anchored_identifier_intros_and_explicit_spelling_remain_dictation(text, expected):
    assert identifier_dictation(text)
    assert identifier_sequence(text) == expected


def test_bare_hindi_affirmative_is_not_mistaken_for_the_letter_g():
    assert confirmation_answer("जी") == "yes"
    assert identifier_suffix("जी") is None
    assert not identifier_dictation("जी")
    assert identifier_suffix("जी फॉर गोट") == "G"


def test_unavailable_semantic_answers_and_all_letter_ids_remain_schema_driven():
    assert extract_entity("I do not have it", ENTITY)["value"] == "no_tid"
    assert extract_entity("पता नहीं", ENTITY)["value"] == "no_tid"
    alpha = {"dataType": "identifier", "regexPattern": r"^(HONASA)$"}
    assert extract_entity("HONASA", alpha)["value"] == "HONASA"
    assert not identifier_dictation("HONASA")


@pytest.mark.parametrize("text,expected", [
    ("one two and three", "123"), ("एक दो और तीन", "123"),
    ("one and two and three", "123"), ("एक और दो और तीन", "123"),
    ("one two please", "12"), ("one two three sir", "123"),
    ("please one two", "12"), ("कृपया एक दो", "12"),
    ("sir one two please", "12"), ("one two please sir", "12"),
    ("one two S for Sir", "12S"), ("one two S for Sir please", "12S"),
    ("one two M for Madam", "12M"), ("one two M for Madam sir", "12M"),
    ("one two S, S for Sir", "12S"),
])
def test_bounded_connectors_and_courtesy_preserve_legitimate_dictation(text, expected):
    assert identifier_dictation(text)
    assert identifier_sequence(text) == expected


@pytest.mark.parametrize("text", [
    "one two and help", "one two and wait", "one two and sir",
    "one two because three", "one two and and three",
    "one two help please", "sir one two wait", "one two no sir",
    "one two please?", "कृपया एक बार फिर से बोलिए",
    "one two S for Madam please", "one two M for Sir please",
])
def test_connectors_or_courtesy_cannot_hide_questions_control_prose_or_mismatched_spelling(text):
    assert not identifier_dictation(text)
