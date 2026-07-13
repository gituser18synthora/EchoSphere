"""Tests for TTS text sanitization (Indic-safe)."""

from voicebot.audio.tts_text import (
    sanitize_for_tts,
    truncate_at_sentence_boundary,
)


def test_hindi_unchanged_except_zero_width():
    raw = "नमस्ते\u200b आप कैसे हैं"
    out = sanitize_for_tts(raw)
    assert "\u200b" not in out
    assert "नमस्ते" in out
    assert "आप" in out


def test_tamil_preserved():
    text = "வணக்கம், எப்படி இருக்கிறீர்கள்?"
    assert sanitize_for_tts(text) == text


def test_ensure_terminal_punct_devanagari():
    out = sanitize_for_tts("यह एक वाक्य है", ensure_terminal_punct=True)
    assert out.endswith("।")


def test_ensure_terminal_punct_english():
    out = sanitize_for_tts("Hello there", ensure_terminal_punct=True)
    assert out.endswith(".")


def test_no_extra_punct_when_present():
    assert sanitize_for_tts("Hello?", ensure_terminal_punct=True) == "Hello?"


def test_truncate_at_sentence_boundary():
    long = "One. Two. " + "word " * 120
    out = truncate_at_sentence_boundary(long, max_words=100)
    assert out.endswith(".")
    assert len(out.split()) <= 100
