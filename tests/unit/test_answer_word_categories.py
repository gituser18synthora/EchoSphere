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
    # the reading of a question that declares answer options
    assert yes_no_polarity(text, with_thanks=True) == "yes"


@pytest.mark.parametrize("text", NO)
def test_a_plain_no_in_every_form(text):
    assert yes_no_polarity(text, with_thanks=True) == "no"


@pytest.mark.parametrize("text", [
    "ठीक है, थैंक यू", "हाँ जी, थैंक यू।", "Okay, thank you.", "ok bye", "नहीं, धन्यवाद", "no thank you", "No thanks.",
])
def test_thanks_beside_a_yes_or_no_is_read_only_where_answer_options_are_declared(text):
    """"okay, thank you" after "anything else?" declines. A question without
    answer options keeps such a reply out of the deterministic reading (the
    decision reads it, as before); a question with options reads it."""
    assert yes_no_polarity(text) is None
    assert yes_no_polarity(text, with_thanks=True) in ("yes", "no")


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
    "कॉल करना बंद करो", "फोन करना बंद कीजिए", "मुझे कॉल नहीं चाहिए आपका।",
    "आगे से कॉल मत कीजिएगा", "dobara phone na karein", "Please don't call again.",
    "stop calling me", "no more calls please", "आज के बाद मुझे कॉल मत करना", "फोन न करें",
    "कॉल्स मत करो", "इतनी कॉल मत किया करो",
    # sentence-final danda, both spellings of "फ़ोन", the polite and the plural forms
    "आप कॉल ही मत करो।", "अब दोबारा \u095eोन ना करें।", "कॉल मत करिएगा।", "कॉल नहीं करिए आप", "आप लोग फोन मत किया करो",
    "कोई कॉल नहीं आना चाहिए", "कॉल आना बंद होना चाहिए", "ये कॉलिंग बंद कीजिए", "ना करें फोन दोबारा",
    "अभी के बाद दोबारा कॉल मत करना", "never call me", "do not contact me", "don't ever ring me",
    # "now" as "from now on" / "immediately" is still a revocation
    "अभी से कॉल मत करना", "कल से फोन मत करना", "stop calling me now", "stop calling me right now",
]
NOT_DNC = [
    "कॉल नहीं आया", "कोई कॉल नहीं आया था", "फोन नहीं उठाया", "टेक्नीशियन ने कॉल नहीं किया", "कॉल मत काटो",
    "फोन मत रखिए", "दोबारा कब कॉल करोगे?", "कॉल आया था लेकिन विज़िट नहीं हुई", "उन्होंने कॉल नहीं की",
    "नहीं, मना नहीं किए हैं।", "सर्विस नहीं चाहिए", "हमने कॉल किया था पर किसी ने नहीं उठाया", "कॉल कर लेना बाद में",
    "no call came", "they did not call me", "कॉल नहीं लग रहा था",
    # "do not hang up" is the opposite request (real DEMO utterances)
    "फोन कट मत करो।", "आपका फ़ोन कट मत करना।", "कॉल मत काटो।", "फोन मत रखना", "रुको, फोन मत काटो, मैं बता रहा हूँ।",
    "फिर जा रुक जा फोन कट मत करना अरे फोन कट मत करना भाई",
    # the call as part of another phrase
    "नहीं, अभी नहीं करिए call transfer.", "कॉल ट्रांसफर मत करो", "नहीं फोन पर करेंगे बाकी नहीं करना।", "फोन पर मत करो",
    "मुझे बात नहीं करना phone cut करो.", "फोन कट जाना चाहिए ना।", "फोन मत उठाना", "मुझे कॉल नहीं करना है",
    # ending THIS call, or a statement that calls stopped
    "कॉल बंद करो", "फोन बंद करो", "कॉल आना बंद हो गया",
    # "not now" asks for another time; it revokes nothing
    "अभी कॉल मत करो, शाम को करना", "abhi call mat karo baad me karna", "आज कॉल मत करो", "कल कॉल मत करना",
    "don't call me now", "don't call right now, call later",
    # no word for the call: left to each bot's own flow, not a platform revocation
    "परेशान मत करो", "don't disturb me", "तंग मत करो यार",
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
    "फोन काट दो।", "मैं फोन काट रहा हूँ।", "ठीक है, रखता हूँ।", "कॉल कट करो",
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
    # the same reports as speech-to-text writes them: with the sentence-final danda
    "टेक्नीशियन ने फोन काट दिया।", "उसने फोन रख दिया था।", "कॉल कट गया था।", "कॉल कट गई।",
    # the line keeps dropping
    "कॉल कट रहे हैं", "कॉल कट रहा है।", "कॉल कट रही है बार बार", "फोन कट जा रहा था", "बार-बार कॉल कट जाता है",
    "call kat raha hai", "हम कट रहे हैं छोड़ रहे हैं",
    # "do not hang up"
    "फोन कट मत करो।", "रुको, फोन मत काटो, मैं बता रहा हूँ।",
])
def test_a_report_of_a_hang_up_does_not_end_this_call(text):
    assert not detect_hangup(text)


@pytest.mark.parametrize("text", ["ठीक है", "सही है", "ok", "ओके", "fine", "हाँ ठीक है", "जी ठीक है", "ठीक है, थैंक यू", "done", "alright"])
def test_agreement_words_are_not_a_plain_yes_at_a_status_question(text):
    assert yes_no_polarity(text, with_thanks=True) == "yes"
    assert yes_no_polarity(text, agreement=False, with_thanks=True) is None


@pytest.mark.parametrize("text", ["हाँ", "जी।", "हाँ जी मैम", "yes", "बिल्कुल", "जरूर", "sure", "जी, जी, जी।"])
def test_explicit_yes_words_stay_a_plain_yes_everywhere(text):
    assert yes_no_polarity(text, agreement=False) == "yes"


@pytest.mark.parametrize("text", [
    "हेलो?", "हेलो हेलो", "hello", "Hello.", "हैलो जी", "हाँ जी, हेलो", "सुनिए", "suniye madam", "एलो",
])
def test_a_contact_check_may_cut_the_bots_speech_with_one_word(text):
    from shared.orchestration.deterministic_answers import attention_only, contact_check_only
    assert contact_check_only(text) and attention_only(text)


@pytest.mark.parametrize("text", [
    "बोलिए", "हाँ बोलिए", "जी बताइए", "कहिए", "हम्म", "hmm", "नमस्ते", "नमस्कार जी", "हाँ?", "good morning",
])
def test_an_invitation_to_go_on_is_attention_but_never_cuts_the_bot(text):
    """"boliye", a hum and a returned greeting react without answering (the
    question is asked again), but they ask the bot to continue: one such word
    must not stop the bot mid-sentence the way "hello?" does."""
    from shared.orchestration.deterministic_answers import attention_only, contact_check_only
    assert attention_only(text) and not contact_check_only(text)


@pytest.mark.parametrize("text", ["हेलो, सर्विस चाहिए", "हेलो कौन बोल रहा है", "नहीं", "हाँ जी"])
def test_an_answer_or_a_question_is_no_contact_check(text):
    from shared.orchestration.deterministic_answers import contact_check_only
    assert not contact_check_only(text)


_YES_NO_EDGES = [{"tokens": ["haan"], "signals": ["affirm"]}, {"tokens": ["nahi"], "signals": ["refusal"]}]


@pytest.mark.parametrize("text, answer", [
    ("थैंक यू मैडम, ठीक है।", "yes"), ("जी, जी, बिल्कुल ठीक है सर, धन्यवाद", "yes"), ("हाँ हाँ ठीक है ठीक है जी", "yes"),
    ("okay okay fine, thank you so much", "yes"), ("नहीं नहीं मैडम, बिल्कुल नहीं, थैंक यू", "no"), ("no no, thank you", "no"),
])
def test_a_plain_yes_or_no_of_any_length_is_that_answer_where_options_are_declared(text, answer):
    from shared.orchestration.deterministic_answers import resolve_plain_yes_no
    resolved = resolve_plain_yes_no(text, _YES_NO_EDGES)
    assert resolved is not None and resolved.value == answer


@pytest.mark.parametrize("text", [
    "ठीक है, लेकिन कल भेजना", "हाँ पर charges कितने हैं", "ठीक है?", "नहीं, अब ठीक है", "हाँ नहीं", "थैंक यू", "हेलो",
])
def test_anything_more_than_a_plain_yes_or_no_is_left_to_the_decision(text):
    from shared.orchestration.deterministic_answers import resolve_plain_yes_no
    assert resolve_plain_yes_no(text, _YES_NO_EDGES) is None


def test_a_plain_yes_needs_an_edge_for_it_and_respects_a_state_question():
    from shared.orchestration.deterministic_answers import resolve_plain_yes_no
    assert resolve_plain_yes_no("हाँ जी", [{"tokens": [], "signals": ["refusal"]}]) is None
    assert resolve_plain_yes_no("ठीक है", _YES_NO_EDGES, agreement=False) is None
    assert resolve_plain_yes_no("हाँ जी", _YES_NO_EDGES, agreement=False).value == "yes"
