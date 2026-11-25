"""Word categories a reply is read with: every form, not one call's wording.

DEMO review 2026-11-25 (haier/qa/REPORT_2026-11-25.md). Each list holds the
forms real callers used plus the rest of the same category: other spellings,
Hindi / English / Hinglish, with sir / ma'am / ji, and speech-to-text
renderings. The negative lists are the near misses that must stay out.
"""
from __future__ import annotations

import pytest

from shared.orchestration.deterministic_answers import (
    attention_only, closing_reaction, reaction_only, thanks_only, yes_no_polarity,
)
from shared.orchestration.router import detect_do_not_call, detect_hangup

YES = [
    "जी।", "हाँ", "हां", "हाँ जी", "जी हाँ", "हाँ जी मैम", "जी मैडम।", "जी सर", "Yes ma'am.", "Yes.", "yes sir",
    "ठीक है", "ठीक है।", "theek hai ji", "ओके", "ok", "okay", "ok ji", "ओके मैडम।", "यस", "बिल्कुल", "जी बिल्कुल",
    "जरूर", "ज़रूर", "सही है", "पक्का", "sure", "done", "चलेगा।", "हाँ हाँ", "जी, जी, जी।", "ठीक है चलो",
    "ठीक है, थैंक यू", "हाँ जी, थैंक यू।", "हाँ सर प्लीज़", "alright", "definitely",
]
NO = [
    "नहीं", "नही", "ना", "नो", "No.", "nope", "nahi", "जी नहीं", "नहीं जी", "नहीं मैम", "no sir", "नहीं नहीं",
    "बिल्कुल नहीं", "नहीं, धन्यवाद", "no thank you", "nah",
]
NOT_AN_ANSWER = [
    "hmm", "हम्म।", "हेलो", "hello?", "थैंक यू।", "thank you", "धन्यवाद", "अच्छा", "हाँ बोलिए", "जी बताइए",
    "हाँ कर दो", "हाँ नहीं", "ठीक नहीं", "कोई विजिट नहीं", "हाँ, हेलो।", "जी, थैंक यू", "धन्यवाद जी", "bye", "",
]


@pytest.mark.parametrize("text", YES)
def test_a_plain_yes_in_every_form(text):
    assert yes_no_polarity(text) == "yes"


@pytest.mark.parametrize("text", NO)
def test_a_plain_no_in_every_form(text):
    assert yes_no_polarity(text) == "no"


@pytest.mark.parametrize("text", NOT_AN_ANSWER)
def test_reactions_and_mixed_replies_are_neither(text):
    assert yes_no_polarity(text) is None


@pytest.mark.parametrize("text", [
    "hello", "Hello!", "हेलो।", "हैलो", "हलो।", "हेलो हेलो हेलो", "hello ji", "जी हेलो", "हाँ, हेलो।", "हाँ जी हेलो?",
    "hmm", "हम्म।", "हम्म हम्म", "हाँ बोलिए।", "जी बताइए", "बोलिए", "नमस्ते", "नमस्कार जी", "good morning",
    "सुनिए", "हाँ?", "जी?", "hello?",
])
def test_contact_checks_answer_nothing(text):
    assert attention_only(text) and reaction_only(text)


@pytest.mark.parametrize("text", [
    "जी।", "हाँ", "ठीक है", "हेलो, हाँ चाहिए", "हेलो कौन बोल रहा है", "hello I need service", "थैंक यू", "नहीं",
    "हाँ बोलिए क्या काम है", "",
])
def test_an_answer_or_a_question_is_not_a_contact_check(text):
    assert not attention_only(text)


@pytest.mark.parametrize("text", [
    "thank you", "Thank you so much.", "thanks", "थैंक यू।", "थैंक्यू मैडम", "धन्यवाद", "शुक्रिया", "बहुत धन्यवाद",
    "जी, थैंक यू", "धन्यवाद जी", "thank you sir", "bye", "बाय", "good night", "टाटा",
])
def test_thanks_and_goodbyes_answer_nothing(text):
    assert thanks_only(text) and reaction_only(text)


@pytest.mark.parametrize("text", ["ठीक है, थैंक यू", "हाँ जी, थैंक यू।", "no thank you", "नहीं, धन्यवाद", "ok bye", "thank you लेकिन कब आएगा"])
def test_thanks_beside_a_yes_or_no_is_that_answer(text):
    assert not thanks_only(text)


@pytest.mark.parametrize("text", [
    "ठीक है", "ठीक।", "ठीक। हाँ।", "ओके", "ok ji", "अच्छा", "अच्छा जी", "हाँ जी मैम", "जी", "थैंक यू", "धन्यवाद",
    "ठीक है बस", "ok thank you", "बाय", "ok bye", "हम्म", "हेलो", "आपका भी दिन शुभ रहे", "same to you", "नमस्ते",
    "जय श्री राम", "ठीक है धन्यवाद मैडम",
])
def test_reactions_to_a_farewell(text):
    assert closing_reaction(text)


@pytest.mark.parametrize("text", [
    "नहीं", "नहीं नहीं", "कब आएगा?", "रिजेक्ट नहीं करना है", "ठीक है लेकिन कब आएगा", "अरे, वो सही कब होगा ये?",
    "हाँ यार, ये हमारे पास है।", "कर दो ना", "",
])
def test_anything_with_content_after_a_farewell_is_answered(text):
    assert not closing_reaction(text)


DNC = [
    "मैम ऑलरेडी हो चुकी है, आपसे रिक्वेस्ट है कि अब दोबारा फ़ोन ना करें।", "आप कॉल ही मत करो, कोई ज़रूरत नहीं।",
    "दोबारा फोन मत करना।", "कॉल मत करो", "call mat karna", "मत करना कॉल", "बार-बार फोन मत करो यार।",
    "कॉल करना बंद करो", "फोन करना बंद कीजिए", "परेशान मत करो", "मुझे कॉल नहीं चाहिए आपका।",
    "आगे से कॉल मत कीजिएगा", "dobara phone na karein", "Please don't call again.",
    "stop calling me", "don't disturb me", "no more calls please", "आज के बाद मुझे कॉल मत करना", "फोन न करें",
    "कॉल्स मत करो", "इतनी कॉल मत किया करो",
]
NOT_DNC = [
    "कॉल नहीं आया", "कोई कॉल नहीं आया था", "फोन नहीं उठाया", "टेक्नीशियन ने कॉल नहीं किया", "कॉल मत काटो",
    "फोन मत रखिए", "दोबारा कब कॉल करोगे?", "कॉल आया था लेकिन विज़िट नहीं हुई", "उन्होंने कॉल नहीं की",
    "नहीं, मना नहीं किए हैं।", "सर्विस नहीं चाहिए", "हमने कॉल किया था पर किसी ने नहीं उठाया", "कॉल कर लेना बाद में",
    "no call came", "they did not call me", "कॉल नहीं लग रहा था",
]


@pytest.mark.parametrize("text", DNC)
def test_stop_calling_in_every_form(text):
    assert detect_do_not_call(text)


@pytest.mark.parametrize("text", NOT_DNC)
def test_a_statement_about_a_call_is_not_stop_calling(text):
    assert not detect_do_not_call(text)


@pytest.mark.parametrize("text", [
    "ठीक है, रखता हूँ।", "चलो रखती हूँ", "फोन रखता हूँ", "रखता हूँ",
    "फोन काट दो", "काट रहा हूँ", "फोन कट कर दो", "कॉल बंद करो", "फोन रख दो",
])
def test_the_caller_ending_the_call_in_every_form(text):
    assert detect_hangup(text)


@pytest.mark.parametrize("text", [
    "मैं बिल रखता हूँ घर पर", "फोन मत रखना", "कॉल मत काटो", "थैंक यू", "ठीक है", "हाँ रखा है बिल",
    # a bare goodbye stays small talk for every bot (frozen router corpus)
    "bye", "ok bye", "बाय",
    # "remove my number from the list" is the business request of some bots, not consent revocation
    "मेरा नंबर हटा दो",
    "technician ne phone rakh diya tha", "टेक्नीशियन ने फोन काट दिया", "उसने कॉल काट दी", "कॉल कट गया था",
    "फोन कट जाता है बार बार", "उन्होंने फोन रख दिया था",
])
def test_a_report_of_a_hang_up_does_not_end_this_call(text):
    assert not detect_hangup(text)


@pytest.mark.parametrize("text", ["ठीक है", "सही है", "ok", "ओके", "fine", "हाँ ठीक है", "जी ठीक है", "ठीक है, थैंक यू", "done", "alright"])
def test_agreement_words_are_not_a_plain_yes_at_a_status_question(text):
    assert yes_no_polarity(text) == "yes"
    assert yes_no_polarity(text, agreement=False) is None


@pytest.mark.parametrize("text", ["हाँ", "जी।", "हाँ जी मैम", "yes", "बिल्कुल", "जरूर", "sure", "जी, जी, जी।"])
def test_explicit_yes_words_stay_a_plain_yes_everywhere(text):
    assert yes_no_polarity(text, agreement=False) == "yes"
