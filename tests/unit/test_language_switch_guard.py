"""Low-content utterances cannot switch the conversation language by themselves.

Sarvam labelled a hum ("Hmm hmm try ya") as en-IN on a Hindi call and the
two-word rule flipped the call to English (vs_6Mh-x1iTM4glTaXTqv7KQvSa,
2026-09-18). Fillers and acknowledgement particles now count for nothing: a
switch needs three content words, or two content words that either read as a
known reply ("yes speaking") or follow a turn that already nominated the same
language. Real sentences switch immediately as before.
"""

from tests.unit.test_brain_language import make_brain
from voice_runtime.language_evidence import content_words


class TestContentWords:
    def test_fillers_and_hums_are_dropped(self):
        assert content_words(["hmm", "hmm", "try", "ya"]) == ["try"]
        assert content_words(["ok", "ok", "sir"]) == []
        assert content_words(["हाँ", "जी", "ठीक", "ओके"]) == []
        assert content_words(["हाँ।", "जी,"]) == []  # punctuation, not matras, is stripped
        assert content_words(["mmm", "aaa"]) == []

    def test_real_words_survive(self):
        assert content_words(["yes", "i", "am", "speaking"]) == ["yes", "i", "am", "speaking"]
        assert content_words(["नहीं", "मैं", "नहीं", "करूँगा"]) == ["नहीं", "मैं", "नहीं", "करूँगा"]
        assert content_words(["can", "we", "speak", "in", "english", "please"]) == [
            "can", "we", "speak", "in", "english", "please",
        ]


class TestLowContentSwitchGuard:
    async def test_hum_labelled_english_does_not_switch(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Hmm hmm try ya", "en-IN")
        assert brain._conversation_language == "hi-IN"
        assert brain._pushed == []
        blocked = [d for k, d in brain._recorder.events if k == "language_switch_blocked"]
        assert blocked and blocked[-1]["reason"] == "low_content"
        assert blocked[-1]["content_words"] == 1

    async def test_two_hums_in_a_row_still_do_not_switch(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Hmm hmm try ya", "en-IN")
        await brain._maybe_switch_language("ok ok fine", "en-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_real_sentence_switches_immediately(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Yes, I am speaking.", "en-IN")
        assert brain._conversation_language == "en-IN"

    async def test_recognizable_two_word_reply_switches_at_once(self):
        # "yes speaking" / "Yes please": two content words that read as a
        # known reply keep the pre-2026-09-18 behaviour.
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("yes speaking", "en-IN")
        assert brain._conversation_language == "en-IN"

    async def test_two_content_words_switch_only_when_the_previous_turn_nominated(self):
        # Two content words that are NOT a known reply ("hmm peela darwaza")
        # nominate; the caller staying in that language confirms.
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Can we speak in English please?", "en-IN")
        assert brain._conversation_language == "en-IN"
        await brain._maybe_switch_language("हम्म पीला दरवाज़ा", "hi-IN")  # content: पीला, दरवाज़ा
        assert brain._conversation_language == "en-IN"
        await brain._maybe_switch_language("हम्म नीला दरवाज़ा", "hi-IN")  # nominated → confirmed
        assert brain._conversation_language == "hi-IN"

    async def test_a_turn_in_another_language_in_between_resets_the_nomination(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Can we speak in English please?", "en-IN")
        await brain._maybe_switch_language("हम्म पीला दरवाज़ा", "hi-IN")
        await brain._maybe_switch_language("Please continue in English now", "en-IN")
        await brain._maybe_switch_language("हम्म नीला दरवाज़ा", "hi-IN")
        assert brain._conversation_language == "en-IN"

    async def test_negations_and_yes_no_are_real_words(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Can you help me in English?", "en-IN")
        await brain._maybe_switch_language("नहीं मैं नहीं करूँगा", "hi-IN")
        assert brain._conversation_language == "hi-IN"

    async def test_hindi_hum_on_english_call_does_not_switch_back(self):
        brain = make_brain(language="hi-IN")
        await brain._maybe_switch_language("Can we speak in English please?", "en-IN")
        assert brain._conversation_language == "en-IN"
        await brain._maybe_switch_language("हाँ जी ठीक है", "hi-IN")
        assert brain._conversation_language == "en-IN"
        await brain._maybe_switch_language("नहीं मुझे हिंदी में बताइए", "hi-IN")
        assert brain._conversation_language == "hi-IN"
