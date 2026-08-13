"""Number-language vs conversation-language separation (P0 regression bank).

A caller reading a UTR/OTP/amount in English digit words during a Hindi/
Hinglish call — "UTR number hai nine nine zero one two three" — has NOT
switched languages. Digit words, IDs, and code-switched business terms are
numeric/technical payload: they must not flip the conversation language, the
per-language TTS voice, or the reply-language instruction. Normalization of
the numeric payload itself is covered alongside (words → digits), and the
raw transcript is never rewritten.
"""

from shared.orchestration.spoken_numbers import (
    meaningful_language_words,
    verbalized_digits,
)
from voice_runtime.call_policy import (
    extract_transaction_reference,
    normalize_reference_text,
)
from tests.unit.test_brain_language import make_brain


class TestLanguageStability:
    async def test_english_digit_words_keep_hindi_conversation(self):
        # Bare digit read-out, mislabelled en-IN: previously this SWITCHED
        # the call to English (all-Latin script, no lexicon counterweight).
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("nine nine zero one two three", "en-IN")
        assert brain._conversation_language == "hi-IN"
        assert any(
            event == "language_switch_blocked"
            and data.get("reason") == "numeric_or_technical_payload"
            for event, data in brain._recorder.events
        )

    async def test_utr_sentence_with_english_digits_keeps_hindi(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language(
            "UTR number hai nine nine zero one two three", "en-IN"
        )
        assert brain._conversation_language == "hi-IN"

    async def test_utr_sentence_with_english_copula_keeps_hindi(self):
        # "is" alone is not a conversation switch when everything else in the
        # utterance is numeric/technical payload.
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language(
            "UTR number is nine nine zero one two three", "en-IN"
        )
        assert brain._conversation_language == "hi-IN"

    async def test_technical_terms_alone_keep_hindi(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("UPI transaction ID", "en-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_no_voice_switch_frame_for_numeric_payload(self):
        # The TTS voice must not flap either: no SwitchVoiceLanguageFrame is
        # pushed when the numeric payload blocks the switch.
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language(
            "UTR number hai nine nine zero one two three", "en-IN"
        )
        assert brain._pushed == []

    async def test_english_conversation_with_english_digits_stays_english(self):
        brain = make_brain(language="en-IN")
        await brain._maybe_switch_language(
            "the number is nine nine zero one two three", "en-IN"
        )
        assert brain._conversation_language == "en-IN"

    async def test_genuine_english_sentence_still_switches(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language(
            "Actually, can you explain why this amount is higher?", "en-IN"
        )
        assert brain._conversation_language == "en-IN"

    async def test_hindi_digit_words_keep_hindi(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("नौ नौ शून्य एक दो तीन", "hi-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_mixed_digit_words_keep_hindi(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language(
            "nine nine शून्य one two three", "en-IN"
        )
        assert brain._conversation_language == "hi-IN"


class TestNumericNormalization:
    def test_english_digit_words(self):
        assert extract_transaction_reference(
            "UTR number hai nine nine zero one two three"
        ) == "990123"

    def test_devanagari_digit_words(self):
        assert extract_transaction_reference("नौ नौ शून्य एक दो तीन") == "990123"

    def test_mixed_hindi_english_digit_words(self):
        assert extract_transaction_reference(
            "nine nine शून्य one two three"
        ) == "990123"

    def test_double_prefix(self):
        assert extract_transaction_reference(
            "double nine zero one two three"
        ) == "9990123"[1:]  # double expands to two nines

    def test_compound_hindi_groups(self):
        # "नौ सौ निन्यानवे चार सौ छत्तीस" → 999 436 → 999436
        assert extract_transaction_reference(
            "नौ सौ निन्यानवे चार सौ छत्तीस"
        ) == "999436"

    def test_grouped_digits_with_pauses(self):
        assert extract_transaction_reference("1234 5678 9012") == "123456789012"

    def test_raw_transcript_not_required_to_change(self):
        # The derived normalization never feeds back into the raw text.
        raw = "मेरा UTR नौ नौ शून्य एक दो तीन है"
        normalized = normalize_reference_text(raw)
        assert "990123" in normalized
        assert raw.startswith("मेरा")  # untouched original

    def test_verbalizer_leaves_prose_alone(self):
        assert verbalized_digits("Actually, can you explain this?") == (
            "Actually, can you explain this?"
        )


class TestMeaningfulWords:
    def test_numeric_payload_strips_to_nothing(self):
        assert meaningful_language_words("nine nine zero one two three") == []

    def test_business_terms_strip(self):
        assert meaningful_language_words(
            "UTR number hai nine nine zero one two three"
        ) == ["hai"]

    def test_real_speech_survives(self):
        words = meaningful_language_words(
            "Actually, can you explain why this amount is higher?"
        )
        assert "explain" in words and "amount" not in words
