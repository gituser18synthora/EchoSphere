"""Canned phrases cover every language a bot may be configured for.

Malayalam and Tamil callers used to hear the English silence ladder and
English clarifications because only ``hi``/``en`` renderings existed.
"""

import re

from shared.guardrails.engine import _GUARDRAIL_PHRASES
from shared.orchestration.phrases import _PHRASES, canned, entry_question_retry
from voice_runtime.call_policy import _COLLECTIONS_FALLBACKS, canned as collections_fallback

MALAYALAM = re.compile(r"[ഀ-ൿ]")
TAMIL = re.compile(r"[஀-௿]")
PLACEHOLDER = re.compile(r"\{\w+\}")


def _tables():
    return {"phrases": _PHRASES, "collections": _COLLECTIONS_FALLBACKS, "guardrails": _GUARDRAIL_PHRASES}


def test_every_phrase_has_hindi_english_malayalam_and_tamil():
    for name, table in _tables().items():
        for key, entry in table.items():
            assert {"en", "hi", "ml", "ta"} <= set(entry), (name, key, sorted(entry))
            assert MALAYALAM.search(entry["ml"]), (name, key)
            assert TAMIL.search(entry["ta"]), (name, key)


def test_placeholders_survive_translation():
    for name, table in _tables().items():
        for key, entry in table.items():
            expected = set(PLACEHOLDER.findall(entry["en"]))
            for lang in ("ml", "ta"):
                assert set(PLACEHOLDER.findall(entry[lang])) == expected, (name, key, lang)


def test_locale_lookup_is_plain_and_falls_back_to_english():
    assert MALAYALAM.search(canned("silence_check_1", "ml-IN"))
    assert TAMIL.search(canned("silence_check_1", "ta-IN"))
    assert canned("silence_check_1", "ml") == canned("silence_check_1", "ml-IN")
    assert canned("silence_check_1", "mr-IN") == canned("silence_check_1", "en")   # no Marathi row yet
    assert canned("silence_check_1", "hi-IN").startswith("Hello")


def test_entry_retry_prefix_follows_the_locale():
    greeting = "Hello. Am I speaking with Ravi?"
    assert entry_question_retry(greeting, "ta-IN").endswith("Am I speaking with Ravi?")
    assert TAMIL.search(entry_question_retry(greeting, "ta-IN"))
    assert MALAYALAM.search(entry_question_retry(greeting, "ml-IN"))
    assert entry_question_retry(greeting, "hi-IN").startswith("माफ़ कीजिए")
    assert entry_question_retry(greeting, "en-IN").startswith("Sorry, I couldn't understand that.")


def test_collections_fallback_speaks_malayalam_and_tamil():
    hi = collections_fallback("collections_identity_reask", "hi-IN") if callable(collections_fallback) else None
    if hi is not None:
        assert MALAYALAM.search(collections_fallback("collections_identity_reask", "ml-IN"))
        assert TAMIL.search(collections_fallback("collections_identity_reask", "ta-IN"))
