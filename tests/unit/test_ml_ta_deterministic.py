"""Malayalam / Tamil deterministic handling (bot_80487d7ce2e9, 2026-09-15).

A bare native "yes"/"no", a hardship statement, a payment commitment and a
spoken amount must take the SAME deterministic paths as their Hindi/English
forms — never a needless LLM round-trip.
"""

import asyncio

import pytest

from shared.orchestration.router import classify_user_signal, leading_affirmation
from shared.orchestration.spoken_numbers import verbalized_digits
from shared.orchestration.entity_extractor import extract_entity
from voice_runtime.call_policy import classify_identity_answer
from voice_runtime.endpointing import is_short_complete_reply
from voice_runtime.transcript_gate import resolve_allowed_languages
from tests.unit.test_opening_affirm_and_language_rescue import (
    _GateStub, _frame_in, make_brain, stub_turn_handler, transcript,
)


@pytest.mark.parametrize("text,signal", [
    ("അതെ", "affirm"), ("അതെ.", "affirm"), ("ശരി", "affirm"), ("ഉവ്വ്", "affirm"),
    ("ஆமாம்", "affirm"), ("ஆமாம்.", "affirm"), ("சரி", "affirm"), ("ஆமா", "affirm"),
    ("athe", "affirm"), ("aamaam", "affirm"),
    ("ഇല്ല", "refusal"), ("വേണ്ട", "refusal"), ("അല്ല", "refusal"),
    ("இல்லை", "refusal"), ("வேண்டாம்", "refusal"), ("இல்ல இல்ல", "refusal"),
    ("illa", "refusal"), ("illai", "refusal"),
    ("എനിക്ക് പണം ഇല്ല", "hardship"), ("ഈ മാസം പണമില്ല", "hardship"),
    ("അടയ്ക്കാൻ പറ്റില്ല", "hardship"),
    ("என்னிடம் பணம் இல்லை", "hardship"), ("இந்த மாதம் கட்ட முடியாது", "hardship"),
    ("ഞാൻ അടുത്ത ആഴ്ച അടയ്ക്കാം", "payment_intent"), ("യുപിഐ വഴി അടയ്ക്കാം", "payment_intent"),
    ("நான் அடுத்த வாரம் கட்டுகிறேன்", "payment_intent"), ("யூபிஐ மூலம் பேமெண்ட் பண்றேன்", "payment_intent"),
    # regressions
    ("हाँ", "affirm"), ("हाँ जी", "affirm"), ("yes", "affirm"), ("okay", "affirm"),
    ("नहीं", "refusal"), ("no", "refusal"), ("nahi nahi", "refusal"),
    ("paise nahi hai", "hardship"), ("i can't pay", "hardship"),
    ("payment kar dunga", "payment_intent"), ("i will pay tomorrow", "payment_intent"),
])
def test_native_yes_no_hardship_and_commitment_signals(text, signal):
    assert classify_user_signal(text) == signal


def test_hardship_wins_over_a_later_commitment_in_every_language():
    for text in (
        "ഈ മാസം എനിക്ക് പണം ഇല്ല, അടുത്ത ആഴ്ച ഞാൻ അടയ്ക്കാം",
        "இந்த மாதம் என்னிடம் பணம் இல்லை, அடுத்த வாரம் கட்டுகிறேன்",
        "paise nahi hai, agle hafte de dunga",
    ):
        assert classify_user_signal(text) == "hardship", text


def test_leading_affirmation_recognizes_native_openers():
    for text in ("അതെ, ഞാൻ ഗൗരവ് ആണ്.", "ശരി, പറയൂ", "ஆமாம், நான் கௌரவ் தான்.", "சரி சொல்லுங்கள்"):
        assert leading_affirmation(text), text
    for text in ("അതെ, പക്ഷേ ഇല്ല വേണ്ട", "ஆமாம் ஆனா வேண்டாம்", "ഇല്ല", "இல்லை"):
        assert not leading_affirmation(text), text


@pytest.mark.parametrize("text,signal", [
    # cv_eb5cdace6a98 / cv_78c1236a5cdf: the colloquial phone-call yes.
    ("ഹാ", "affirm"), ("ആ", "affirm"), ("ആഹ്", "affirm"), ("ഉം", "affirm"), ("ஆங்", "affirm"),
    # A relation answering, wrong number, "not here" → wrong person, never a yes.
    ("അമ്മയാണ്", "wrong_person"), ("ഭാര്യയാണ്", "wrong_person"), ("അയാൾ ഇല്ല", "wrong_person"),
    ("തെറ്റായ നമ്പർ", "wrong_person"), ("அம்மா பேசுறேன்", "wrong_person"), ("தவறான எண்", "wrong_person"),
    # A bare "ആ" inside a word is not a yes.
    ("ആന", None), ("ആണ്", None),
])
def test_colloquial_yes_and_wrong_person_signals(text, signal):
    assert classify_user_signal(text) == signal


@pytest.mark.parametrize("text,signal,answer", [
    ("അതെ.", "affirm", "confirm"), ("അതെ, ഞാൻ ഗൗരവ് ആണ്.", "affirm", "confirm"),
    # The two failed calls: "yes, go ahead, it's Gaurav" / "yes it's Gaurav, go ahead".
    ("ഹാ പറഞ്ഞോളൂ ഗോരവമാണ്", None, "confirm"), ("ആ ഗൗരവമാണ് പറഞ്ഞോളൂ.", None, "confirm"),
    ("ഗൗരവ് ആണ്", None, "confirm"), ("ഗൗരവമാണ്", None, "confirm"), ("ഹാ", "affirm", "confirm"),
    ("ശരി പറയൂ", None, "confirm"), ("ஆமா சொல்லுங்க", None, "confirm"), ("ஆங்", "affirm", "confirm"),
    # Question back / relation / wrong number stay unclear or deny.
    ("എന്താണ്?", "question", "unclear"), ("என்ன?", "question", "unclear"),
    ("അമ്മയാണ്", "wrong_person", "deny"), ("ഭാര്യയാണ്", "wrong_person", "deny"),
    ("അയാൾ ഇല്ല", "wrong_person", "deny"), ("அம்மா பேசுறேன்", "wrong_person", "deny"),
    ("ഞാൻ അല്ല", None, "deny"), ("ആണ്", None, "unclear"), ("പറഞ്ഞോളൂ", None, "unclear"),
    ("ഞാൻ തന്നെ", None, "confirm"), ("ஆமாம்.", "affirm", "confirm"),
    ("ஆமாம், நான் கௌரவ் தான்.", "affirm", "confirm"), ("நான் தான்", None, "confirm"),
    ("അല്ല", "refusal", "deny"), ("இல்லை", "refusal", "deny"), ("അല്ല, ഞാൻ ഗൗരവ് അല്ല", None, "deny"),
    ("ആരാണ്?", None, "unclear"), ("யார்?", None, "unclear"), ("പറയൂ", None, "unclear"),
    ("हाँ बोलो", "affirm", "confirm"), ("yes speaking", None, "confirm"), ("no", "refusal", "deny"),
])
def test_identity_answer_in_malayalam_and_tamil(text, signal, answer):
    assert classify_identity_answer(text, signal) == answer


def test_short_native_replies_get_the_short_endpoint():
    for text in ("അതെ", "ഇല്ല.", "ശരി", "ஆமாம்", "இல்லை", "சரி."):
        assert is_short_complete_reply(text), text
    assert not is_short_complete_reply("അതെ, ഞാൻ ഗൗരവ് ആണ്")


AMOUNT_ENTITY = {
    "name": "partial_amount", "dataType": "text",
    "regexPattern": r"((?:\d[\d,]*(?:\.\d+)?(?!\d))|(?:एक|दो|तीन)\s*(?:सौ|हज़ार|हजार)?)(?!\s*(?:तारीख|tarikh|tareekh|को\b))",
}


@pytest.mark.parametrize("text,expected", [
    ("ഞാൻ അടുത്ത ആഴ്ച രണ്ടായിരം രൂപ അടയ്ക്കാം", "2000"),
    ("അഞ്ഞൂറ് രൂപ", "500"), ("രണ്ട് ആയിരം", "2000"), ("പതിനായിരം", "10000"),
    ("நான் அடுத்த வாரம் இரண்டாயிரம் ரூபாய் கட்டுகிறேன்", "2000"),
    ("ஐந்நூறு ரூபாய்", "500"), ("இரண்டு ஆயிரம்", "2000"), ("பத்தாயிரம்", "10000"),
    ("I will pay 2000 next week", "2000"),
])
def test_native_amounts_reach_the_digit_matcher(text, expected):
    assert extract_entity(text, AMOUNT_ENTITY)["value"] == expected
    assert expected in verbalized_digits(text)


def test_hindi_number_words_unchanged():
    assert verbalized_digits("दो हज़ार रुपये") == "2000 रुपये"
    assert verbalized_digits("nine nine zero") == "9 9 0"


# ── short segment mislabelled as another configured language ────────────────

TAMIL_LONG = "ஆமாம், நான் கௌரவ் பாண்டே தான். சொல்லுங்கள் என்ன விஷயம்."
HINDI_MISLABEL = "आमाम नान कौरव दान"       # Sarvam's Devanagari for a short Tamil turn
TAMIL_RECOVERED = "ஆமாம் நான் கௌரவ் தான்"


def _multilingual_brain(gate=None, batch=None):
    brain = make_brain(gate=gate, batch_transcriber=batch)
    brain._config.languages.extend(["ml-IN", "ta-IN"])
    brain._allowed_stt_languages = resolve_allowed_languages({}, brain._config.languages)
    return brain


class TestShortSegmentRetranscription:
    @pytest.mark.asyncio
    async def test_short_cross_labelled_segment_is_reread_in_the_established_language(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return TAMIL_RECOVERED

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        handled = stub_turn_handler(brain)
        # 1. the caller establishes Tamil with a full sentence
        await _frame_in(brain, transcript(TAMIL_LONG, language="ta-IN", language_code="ta-IN",
                                          language_probability=0.95))
        assert brain._conversation_language == "ta-IN" and brain._caller_language_confirmed
        # 2. a short turn comes back labelled Hindi in Devanagari
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript(HINDI_MISLABEL, language="hi-IN", language_code="hi-IN",
                                          language_probability=0.7))
        assert calls == ["ta-IN"]
        assert handled == [TAMIL_LONG, TAMIL_RECOVERED]
        assert brain._conversation_language == "ta-IN"
        kinds = brain._recorder.event_kinds()
        assert "short_segment_retranscribed" in kinds
        assert "stt_segment_rejected" not in kinds

    @pytest.mark.asyncio
    async def test_long_turn_in_another_language_still_switches(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return TAMIL_RECOVERED

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        handled = stub_turn_handler(brain)
        await _frame_in(brain, transcript(TAMIL_LONG, language="ta-IN", language_code="ta-IN"))
        gate.retained = (b"\x00\x01" * 16000, 16000)
        hindi = "हाँ बोलो, मैं गौरव बोल रहा हूँ, हिंदी में बात करते हैं"
        await _frame_in(brain, transcript(hindi, language="hi-IN", language_code="hi-IN"))
        assert calls == []                                   # never re-read a real switch
        assert handled == [TAMIL_LONG, hindi]
        assert brain._conversation_language == "hi-IN"

    @pytest.mark.asyncio
    async def test_no_reread_before_the_caller_established_a_language(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return TAMIL_RECOVERED

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)   # greeting default hi-IN, nothing confirmed
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript("ஆமாம்", language="ta-IN", language_code="ta-IN"))
        assert calls == [] and handled == ["ஆமாம்"]
        assert not brain._caller_language_confirmed

    @pytest.mark.asyncio
    async def test_short_english_reply_is_never_reread(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return "യെസ്"

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        handled = stub_turn_handler(brain)
        await _frame_in(brain, transcript("അതെ, ഞാൻ ഗൗരവ് പാണ്ഡെ ആണ്. പറയൂ.", language="ml-IN",
                                          language_code="ml-IN"))
        assert brain._conversation_language == "ml-IN"
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript("yes", language="en-IN", language_code="en-IN"))
        assert calls == [] and handled[-1] == "yes"

    @pytest.mark.asyncio
    async def test_failed_reread_keeps_the_original_segment(self):
        async def _batch(pcm, rate, language):
            return "आमाम नान"                              # not Tamil → rejected by the script check

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        handled = stub_turn_handler(brain)
        await _frame_in(brain, transcript(TAMIL_LONG, language="ta-IN", language_code="ta-IN"))
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript(HINDI_MISLABEL, language="hi-IN", language_code="hi-IN"))
        assert handled == [TAMIL_LONG, HINDI_MISLABEL]
        assert "short_segment_retranscribe_failed" in brain._recorder.event_kinds()


ENGLISH_LOOKALIKE = "In law"          # Sarvam's en-IN rendering of a Malayalam "ഇല്ല"
MALAYALAM_NO = "ഇല്ല"


class TestEnglishLookalikeFragments:
    @pytest.mark.asyncio
    async def test_short_unrecognized_latin_fragment_is_reread_in_the_call_language(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return MALAYALAM_NO

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        brain._conversation_language = brain._config.language = "ml-IN"   # Malayalam-default bot
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript(ENGLISH_LOOKALIKE, language="en-IN", language_code="en-IN",
                                          language_probability=0.76))
        assert calls == ["ml-IN"] and handled == [MALAYALAM_NO]
        assert brain._conversation_language == "ml-IN"
        assert "short_segment_retranscribed" in brain._recorder.event_kinds()

    @pytest.mark.asyncio
    async def test_unrecognized_fragment_never_flips_the_call_to_english(self):
        brain = _multilingual_brain()                     # no retained audio → no re-read
        brain._conversation_language = brain._config.language = "ml-IN"
        handled = stub_turn_handler(brain)
        await _frame_in(brain, transcript(ENGLISH_LOOKALIKE, language="en-IN", language_code="en-IN"))
        assert handled == [ENGLISH_LOOKALIKE]
        assert brain._conversation_language == "ml-IN"
        blocked = brain._recorder.events_of("language_switch_blocked")
        assert blocked and blocked[-1]["reason"] == "short_unrecognized_english"

    @pytest.mark.asyncio
    async def test_recognizable_short_english_still_switches_and_is_not_reread(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return MALAYALAM_NO

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        brain._conversation_language = brain._config.language = "ml-IN"
        handled = stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript("yes speaking", language="en-IN", language_code="en-IN",
                                          language_probability=0.9))
        assert calls == [] and handled == ["yes speaking"]
        assert brain._conversation_language == "en-IN"

    @pytest.mark.asyncio
    async def test_english_call_is_never_reread(self):
        calls = []

        async def _batch(pcm, rate, language):
            calls.append(language)
            return "x"

        gate = _GateStub()
        brain = _multilingual_brain(gate, _batch)
        brain._conversation_language = "en-IN"
        brain._caller_language_confirmed = True
        stub_turn_handler(brain)
        gate.retained = (b"\x00\x01" * 16000, 16000)
        await _frame_in(brain, transcript("In law", language="en-IN", language_code="en-IN"))
        assert calls == []


# ── fixed workflow text adaptation on non-Devanagari default languages ───────

def test_other_indic_script_conversations_adapt_authored_text():
    from tests.unit.test_brain_response_modes import _LLMStub, _WorkflowStub, make_brain, wf_result
    stub = _WorkflowStub(wf_result("क्या आप पूरा payment करेंगे?", done=False, node_prompt="क्या आप पूरा payment करेंगे?"))
    for language, expected in (("ml-IN", True), ("ta-IN", True), ("te-IN", True),
                               ("hi-IN", False), ("mr-IN", False), ("en-IN", False)):
        brain = make_brain(stub, _LLMStub(), language=language)
        brain._conversation_language = language
        assert brain._conversation_uses_other_indic_script() is expected, language


def test_identity_question_detector_recognizes_llm_authored_forms():
    from voice_runtime.call_policy import _IDENTITY_QUESTION

    for text in (
        "Yes — are you Gaurav Pandey?",
        "നിങ്ങൾ ഗൗരവ് പെണ്ടെയാണോ എന്ന് ദയവായി ഒരു ശരിയായ ഉത്തരം തീർച്ചപ്പെടുത്തി തരാമോ?",
        "ദയവായി സ്ഥിരീകരിക്കുക — നിങ്ങൾ ഗൗരവ് പാണ്ടേയാണ്‌ എന്നുള്ളത് ശരിയാണ്‌?",
        "ഞാൻ Gaurav Pandey ജിയോടാണോ സംസാരിക്കുന്നത്?",
        "நீங்கள் கௌரவ் பாண்டே தானா?",
        "क्या मैं Gaurav Pandey जी से बात कर रही हूँ?",
    ):
        assert _IDENTITY_QUESTION.search(text), text
    for text in (
        "Are you able to pay today?", "Are you there?", "Are you sure?",
        "क्या आज आप पच्चीस हज़ार रुपये का भुगतान कर पाएँगे?",
        "നിങ്ങൾക്ക് ഇന്ന് പണം അടയ്ക്കാൻ കഴിയുമോ?",
    ):
        assert not _IDENTITY_QUESTION.search(text), text
