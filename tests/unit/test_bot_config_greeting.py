"""The call-opening greeting follows the bot's DEFAULT language, not the
authoring order of the greeting variants (cv_10dd1b13a5e1)."""

from shared.bot_config import select_greeting_variant

VARIANTS = [
    {"language": "hi-IN", "content": "नमस्ते! Zepto Support से बोल रहा हूँ।"},
    {"language": "en-IN", "content": "Hello! This is Zepto Support."},
]


def test_english_default_picks_the_english_variant():
    assert select_greeting_variant(VARIANTS, "en-IN") == VARIANTS[1]["content"]


def test_hindi_default_picks_the_hindi_variant():
    assert select_greeting_variant(VARIANTS, "hi-IN") == VARIANTS[0]["content"]


def test_base_language_match_when_locale_differs():
    assert select_greeting_variant(VARIANTS, "en-US") == VARIANTS[1]["content"]
    assert select_greeting_variant(VARIANTS, "hi") == VARIANTS[0]["content"]


def test_unknown_or_missing_language_falls_back_to_the_first_variant():
    assert select_greeting_variant(VARIANTS, "ta-IN") == VARIANTS[0]["content"]
    assert select_greeting_variant(VARIANTS, None) == VARIANTS[0]["content"]


def test_empty_content_variants_are_skipped():
    variants = [{"language": "en-IN", "content": ""}, {"language": "hi-IN", "content": "नमस्ते"}]
    assert select_greeting_variant(variants, "en-IN") == "नमस्ते"
    assert select_greeting_variant([], "en-IN") is None
