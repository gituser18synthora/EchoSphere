"""18 November review: 18 DEMO calls after 06:37 UTC (workflow v55 / release v0.1.34).

Calls before 06:55:08 UTC ran commit 55168b83, calls after it 8cfd11b2. Every
caller line is the transcript text of the call named beside it. The saved v54
definition is repaired to v55 (``repair_evening_review``) and v56
(``repair_midday_review``) and run through the real workflow engine with the
real merchant API code, exactly as ``test_evening_call_review`` does. "TTS
text" is the engine reply after the TTS router's own text preparation.

Not simulated: STT, audio timing, barge-in, the LLM and the intent classifier
(its recorded label is passed as ``signal``).

Run:  env/bin/pytest tenant/Wordline/tests/test_midday_call_review.py -q
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from shared.audio.text import sanitize_for_tts
from shared.orchestration.entity_extractor import extract_entity
from shared.orchestration.identifier_confirmation import confirmation_answer
from tenant.Wordline.setup.call_review_repair import (
    ANYTHING_ELSE_HUBS, MORE_HELP, MORE_HELP_ID, repair_evening_review, repair_midday_review,
)
from tenant.Wordline.tests.test_evening_call_review import NO, SNAPSHOT, YES, _choice, replay

ASK_TID = "कृपया अपनी पूरी Terminal ID, उसमें आने वाले letters के साथ बताइए। Letter हो तो A for Apple जैसे बोलिए।"
RAISE_INTRO = "जी, अगर आप SMS या I-V-R के जरिए request नहीं कर पा रहे हैं, तो मैं आपकी तरफ से paper roll की request raise"
GOODBYE = "मुझे खुशी है कि मैं आज आपकी मदद कर सका।"


@pytest.fixture(scope="module")
def v55() -> dict:
    return repair_evening_review(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def v56(v55) -> dict:
    return repair_midday_review(v55)


def _md5(value) -> str:
    return hashlib.md5(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ── the repair itself ───────────────────────────────────────────────────────

def test_local_definitions_are_the_ones_on_demo(v55, v56):
    """Canonical md5 of what DEMO reported for v55 (before) and v56 (after the apply)."""
    assert (_md5(v55["nodes"]), _md5(v55["edges"])) == (
        "21b0c7eb722dd30f7d4fabef56231a01", "01b394c4a416d4feb74cb371636e9c81")
    assert (_md5(v56["nodes"]), _md5(v56["edges"])) == (
        "21b0c7eb722dd30f7d4fabef56231a01", "cbf9a3d31039b6e1ba42b261fce78363")


def test_repair_reorders_five_hubs_and_extends_the_raise_phrases_only(v55, v56):
    frozen = copy.deepcopy(v55)
    assert repair_midday_review(v55) == v56 and v55 == frozen and repair_midday_review(v56) == v56
    assert v56["nodes"] == v55["nodes"] and len(v56["edges"]) == len(v55["edges"])
    before = {e["id"]: e for e in v55["edges"]}
    changed = sorted(e["id"] for e in v56["edges"] if e != before[e["id"]])
    assert changed and all(before[i]["to"] == "n_raise_has_record" or "raise" in i for i in changed)
    for edge_id in changed:
        old, new = before[edge_id]["label"], next(e for e in v56["edges"] if e["id"] == edge_id)["label"]
        assert new.startswith(old + "/") and "खुद कर दो" in new[len(old):]
    for hub in ANYTHING_ELSE_HUBS:
        assert next(e for e in v56["edges"] if e["from"] == hub)["to"] == MORE_HELP_ID
        assert sorted(e["to"] for e in v55["edges"] if e["from"] == hub) == sorted(
            e["to"] for e in v56["edges"] if e["from"] == hub)


def test_repair_refuses_a_definition_without_the_yes_edges():
    with pytest.raises(ValueError):
        repair_midday_review(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


# ── hub routing: (call, hub, caller text, classifier label, v55 target, v56 target) ──

@pytest.mark.parametrize("call,hub,text,signal,before,after", [
    ("cv_f3800137eb10 06:49:05", "n_anything_else", "जी।", YES, "n_end", MORE_HELP_ID),
    ("cv_8bd254559c2f 06:43:03", "n_sms",
     "अच्छा। नहीं, मुझे तुम खुद कर दो, मुझे नहीं करना है। ठीक है सर।", None, "n_end", "n_raise_has_record"),
    ("cv_b9d8bdaf07c6 06:47:32", "n_anything_else", "हाँ।", YES, MORE_HELP_ID, MORE_HELP_ID),
    ("cv_30c601d31d0a 06:53:09", "n_sms", "मुझे आईवीआर का तरीका बताओ।", "question", "n_ivr", "n_ivr"),
    ("cv_cc27070dbea4 07:02:31", "n_ivr", "हांजी। हांजी।", YES, MORE_HELP_ID, MORE_HELP_ID),
    # Closing replies keep closing.
    ("cv_c50d25900364 06:48:15", "n_anything_else", "नहीं।", NO, "n_end", "n_end"),
    ("cv_472c825bac4c 06:43:18", "n_anything_else", "हाँ नहीं जी धन्यवाद।", NO, "n_end", "n_end"),
    ("cv_6e32bf56bb76 06:44:14", "n_anything_else", "हां नहीं। Thank you", None, "n_end", "n_end"),
    ("cv_8b99faef4626 06:48:18", "n_anything_else", "No, no, thank you.", NO, "n_end", "n_end"),
    ("cv_b9d8bdaf07c6 06:47:41", MORE_HELP_ID, "नहीं, किसी में नहीं।", NO, "n_end", "n_end"),
    ("cv_4cecac598f1c 07:02:30", "n_anything_else", "Thank you हाँ, बहुत अच्छा। हांजी।", YES, "n_end", "n_end"),
    ("cv_26dac9f59e0c 07:04:48", "n_anything_else", "हां। हाँ। हांजी। Hello ठीक है।", YES, "n_end", "n_end"),
    ("-", "n_anything_else", "ठीक है।", YES, "n_end", "n_end"),
    ("-", "n_low_anything_else", "जी", YES, "n_low_end", MORE_HELP_ID),
])
def test_hub_routing(v55, v56, call, hub, text, signal, before, after):
    assert _choice(v55, hub, text, signal) == before, call
    assert _choice(v56, hub, text, signal) == after, call


# ── Terminal ID dictations of the 18 calls: (call, text, value captured now) ──

TID_UTTERANCES = [
    ("cv_472c825bac4c", "Do 30 5073 F4 FL", "2305073"),                    # STT lost the letter
    ("cv_472c825bac4c", "No, 262305073 A, A for Apple.", None),
    ("cv_472c825bac4c", "2305073A, A for Apple.", "2305073A"),
    ("cv_a9af6b51d328", "02957818 Capital A", "02957818"),                 # not fixed: "Capital A"
    ("cv_a9af6b51d328", "02957818 A for Apple", "02957818A"),
    ("cv_a9af6b51d328", "Correct ID Correct, it is 02957. 818 A for Apple", "02957818A"),
    ("cv_6e32bf56bb76", "Uh 3318 258 A for Alpha", "3318258A"),
    ("cv_8bd254559c2f", "3137 Five Zero Two ठीक है।", "3137502"),
    ("cv_f3800137eb10", "Two, Five Four Four Zero Eight C A", None),
    ("cv_f3800137eb10", "मैं फिर से बताता हूँ। Two Five Four Four 0 8 3A", "2544083A"),
    ("cv_b9d8bdaf07c6", "3137502 A for Apple", "3137502A"),
    ("cv_8b99faef4626", "1 2 3 1 4 1 0 A ठीक हो रहा है कल।", "1231410A"),
    ("cv_8b99faef4626", "1 2 3 1 Four one 0 A A for Apple", "1231410A"),
    ("cv_c50d25900364", "197 4 8 5 7 A for Ayurveda", "1974857A"),
    ("cv_1ffe2d9793fc", "Hmm Zero Two Nine Five Seven 818 cap Alphabet C A for Apple, sorry.", None),
    ("cv_1ffe2d9793fc", "02957818 Pay for Apple", "02957818"),             # not fixed: "Pay for Apple"
    ("cv_1ffe2d9793fc", "03957818 A for Apple", "03957818A"),
    ("cv_4cecac598f1c", "ओके, आज 2305। Zero Seven Three A Three Four नहीं।", None),
    ("cv_4cecac598f1c", "2305073 AA for aeroplane", "2305073A"),
    ("cv_ae4fdb8f86bd", "A for Aamir Khan 2129007", "2129007"),            # not fixed: leading letter
    ("cv_cd292d6c02cd", "2 5 4408 Three", "2544083"),
    ("cv_cd292d6c02cd", "हाँ जी। नहीं, आईडी सही नहीं है। 2 5 Four Four Zero Okay 3A, A4 Apple", None),
    ("cv_26dac9f59e0c", "2 5 Four Four 0 Eight Three A, A for Apple", "2544083A"),
    ("cv_cc27070dbea4", "1 4 3 2 4 2 1 A for Alpha", "1432421A"),
    ("cv_a3a2e9c1d463", "301134 Six, Eight for Alpha.", "3011346A"),        # was 30113468
    ("cv_a3a2e9c1d463", "One 37317 A for Alpha", None),
    ("cv_a3a2e9c1d463", "1317317 A for Alpha", "1317317A"),
]


@pytest.mark.parametrize("call,text,expected", TID_UTTERANCES)
def test_terminal_id_capture_of_every_reviewed_dictation(v56, call, text, expected):
    ask = next(n for n in v56["nodes"] if n["id"] == "n_ask_tid")
    result = extract_entity(text, {**ask["config"]["entity"], "name": "spoken_terminal_id"})
    assert (result["value"] if result["matched"] else None) == expected, call


# ── replies at the Terminal ID readback: (call, text, answer) ───────────────

@pytest.mark.parametrize("call,text,expected", [
    ("cv_1ffe2d9793fc", "हाँ जी, बिल्कुल सही है।", "yes"), ("cv_1ffe2d9793fc", "हाँ, यह सही है।", "yes"),
    ("cv_cc27070dbea4", "ठीक है।", "yes"), ("cv_6e32bf56bb76", "हाँ, सही है।", "yes"),
    ("cv_472c825bac4c", "हाँ हाँ।", "yes"), ("cv_c50d25900364", "हां सही है।", "yes"),
    ("cv_1ffe2d9793fc", "नहीं।", "no"), ("cv_a9af6b51d328", "No, it's not correct.", "no"),
    ("cv_cd292d6c02cd", "हाँ, बिल्कुल सुन पा रहे हैं।", None),
    ("cv_cd292d6c02cd", "आपने लास्ट में ए नहीं बोला।", None),          # amends the ID instead
    ("cv_cd292d6c02cd", "नहीं। लास्ट में ए है। लास्ट में ए है।", None),   # amends the ID instead
    ("cv_a9af6b51d328", "उसके बाद। A for Apple", None), ("cv_a9af6b51d328", "हेलो। नहीं नहीं मेरा।", None),
    ("cv_a9af6b51d328", "Ki A for F pao", None),
])
def test_confirmation_answer_of_every_reviewed_reply(call, text, expected):
    assert confirmation_answer(text) == expected, call


# ── full replays: what is sent to TTS ───────────────────────────────────────

async def test_cv_6e32bf56bb76_paper_roll_raise_reads_every_number_in_english(monkeypatch, v56):
    turns = [
        ("मनन, मुझे पेपर और रिप्रेस रीलेस करनी है।", None), ("Uh 3318 258 A for Alpha", None),
        ("हाँ, सही है।", YES), ("हाँ, सही है।", YES), ("हाँ।", YES), ("हाँ।", YES),
        ("मेरी तरफ से कर दो।", None), ("हाँ बताओ।", YES), ("हाँ, सही है।", YES),
        ("मेरा वीकली ऑफ़ संडे रहता है, फिर सर वर्किंग टाइम नाइन टू फाइव।", None),
        ("हां सही है।", YES), ("मुझे 8 चाहिए।", None),
        ("जी।", YES),                              # cv_f3800137eb10's reply, here instead of "हां नहीं। Thank you"
        ("नहीं, किसी में नहीं।", NO),
    ]
    said = [text for text, _trace in await replay(monkeypatch, v56, "cv_6e32", turns)]
    assert said[0] == ASK_TID
    assert said[1] == "Terminal ID three, three, one, eight, two, five, eight, letter A. क्या यह सही है?"
    assert said[4] == "क्या आपके terminal का model Antera AL9220U Handheld GPRS है? कृपया हाँ या ना में बताइए।"
    assert said[7] == ("क्या आपके outlet का नाम Westend Entertainment, T-I-D number three, three, one, eight, two, "
                       "five, eight, letter A, और address 209 A 9 Westend Rajendra Nagar, Hukmakhedi A B Road "
                       "Indore Madhya Pradesh है? कृपया confirm करने के लिए हाँ या ना में बताइए।")
    assert said[9] == ("हमारे records के अनुसार contact person Mili Mandhan हैं और contact number seven, nine, "
                       "eight, seven, two, six, one, eight, two, two है। कृपया confirm करने के लिए हाँ या ना में बताइए।")
    assert said[10] == "आपको कितने paper rolls चाहिए? Quantity one से nine के बीच होनी चाहिए।"
    assert said[11] == ("आपकी paper roll request successfully raise हो गई है। कृपया future reference के लिए "
                        "docket number note कर लीजिए। आपका docket number है letter W, letter L, four, five, eight, "
                        "nine, two, one। सर क्या मैं आज आपकी किसी और चीज़ में मदद कर सकता हूँ?")
    assert said[12] == MORE_HELP["hi"]             # "जी।" asks what else — it closed the call on v55
    assert said[13].startswith(GOODBYE)


async def test_cv_cd292d6c02cd_trailing_letter_is_added_instead_of_closing_the_call(monkeypatch, v56):
    """Three re-prompts, then "आपकी Terminal ID confirm नहीं हो पाई" and the call was closed."""
    turns = [
        ("हम्म। मुझे बैटरी के बारे में जानकारी चाहिए। मेरे सिस्टम की बैटरी थोड़ी प्रॉब्लम कर रही है।", None),
        ("2 5 4408 Three", None), ("हाँ, बिल्कुल सुन पा रहे हैं।", YES),
        ("आपने लास्ट में ए नहीं बोला।", None), ("हाँ", YES),
    ]
    said = [text for text, _trace in await replay(monkeypatch, v56, "cv_cd29", turns)]
    assert said[1] == "Terminal ID two, five, four, four, zero, eight, three. क्या यह सही है?"
    # An answer to "can you hear me?" is not a confirmation; the re-prompt reads the ID again.
    assert said[2] == ("Terminal ID two, five, four, four, zero, eight, three. "
                       "ID सही हो तो हाँ बोलिए, वरना पूरी सही ID दोबारा बताइए।")
    assert said[3] == ("मैंने आपका ID two, five, four, four, zero, eight, three, letter A सुना है। "
                       "क्या यह सही है? कृपया हाँ या ना में बताइए।")
    assert "Sadhana" in said[4]                    # 2544083A found


async def test_cv_cd292d6c02cd_second_wording_of_the_same_correction(monkeypatch, v56):
    turns = [("बैटरी के बारे में पूछ रहा था।", None), ("2 5 4408 Three", None),
             ("नहीं। लास्ट में ए है। लास्ट में ए है।", None), ("हाँ", YES)]
    said = [text for text, _trace in await replay(monkeypatch, v56, "cv_cd29b", turns)]
    assert "two, five, four, four, zero, eight, three, letter A" in said[2] and "Sadhana" in said[3]


async def test_cv_a3a2e9c1d463_eight_for_alpha_is_read_back_as_letter_a_and_found(monkeypatch, v56):
    """Was read back "…, six, eight", confirmed by the caller and not found (30113468)."""
    turns = [("हाँ, मुझे पेपर रोल चाहिए।", None), ("301134 Six, Eight for Alpha.", None), ("हाँ, सही है।", YES)]
    said = [text for text, _trace in await replay(monkeypatch, v56, "cv_a3a2", turns)]
    assert said[1] == "Terminal ID three, zero, one, one, three, four, six, letter A. क्या यह सही है?"
    assert "Gaurav Traders" in said[2]


async def test_cv_8bd254559c2f_do_it_yourself_after_the_sms_steps_raises_the_request(monkeypatch, v55, v56):
    turns = [
        ("हाँ। मुझे पेपर के बारे में जानना है, पेपर।", None), ("3137 Five Zero Two ठीक है।", None), ("हां।", YES),
        ("हाँ, सही है।", YES), ("हाँ।", YES), ("लेकिन। हाँ है।", YES),
        ("मुझे एसएमएस का तरीका बता दीजिए या फिर।", None),
        ("अच्छा। नहीं, मुझे तुम खुद कर दो, मुझे नहीं करना है। ठीक है सर।", None),
    ]
    old = [text for text, _trace in await replay(monkeypatch, v55, "cv_8bd2_v55", turns)]
    new = [text for text, _trace in await replay(monkeypatch, v56, "cv_8bd2_v56", turns)]
    assert old[:7] == new[:7] and "five, seven, five, seven, five, eight पर भेज दें" in new[6]
    assert old[7].startswith(GOODBYE)              # what the caller got
    assert new[7].startswith(RAISE_INTRO)


async def test_cv_1ffe2d9793fc_natural_confirmations_and_a_plain_no(monkeypatch, v56):
    turns = [
        ("मुझे ना आप बैटरी के बारे में बता सकते हो।", None), ("03957818 A for Apple", None),
        ("हाँ जी, बिल्कुल सही है।", YES), ("3137502", None), ("नहीं।", NO),
        ("3137502 A for Apple", None), ("हाँ, यह सही है।", None),
    ]
    said = [text for text, _trace in await replay(monkeypatch, v56, "cv_1ffe", turns)]
    assert said[1] == "Terminal ID zero, three, nine, five, seven, eight, one, eight, letter A. क्या यह सही है?"
    assert said[2].startswith("इस ID से terminal record नहीं मिला।")      # 03957818A: the caller confirmed a wrong ID
    assert said[3] == "Terminal ID three, one, three, seven, five, zero, two. क्या यह सही है?"
    assert said[5] == "Terminal ID three, one, three, seven, five, zero, two, letter A. क्या यह सही है?"
    assert "Hotel The Rostral" in said[6]


@pytest.mark.parametrize("reply", ["theek nahi hai", "sahi nahi hai", "barobar nahi", "yah sahi nahi hai",
                                   "ठीक नहीं है", "बरोबर नहीं"])
async def test_a_negated_affirmative_at_the_readback_asks_for_the_id_again(monkeypatch, v56, reply):
    turns = [("बैटरी के बारे में पूछ रहा था।", None), ("2 5 4408 Three", None), (reply, None),
             ("2 5 Four Four 0 Eight Three A, A for Apple", None), ("barobar", YES)]
    said = [text for text, _trace in await replay(monkeypatch, v56, "neg_" + reply, turns)]
    assert said[2] == ASK_TID                      # a no: never the lookup, never a silent accept
    assert said[3] == "Terminal ID two, five, four, four, zero, eight, three, letter A. क्या यह सही है?"
    assert "Sadhana" in said[4]


async def test_cv_4cecac598f1c_pin_code_with_the_tts_safety_net(monkeypatch, v56):
    """Workflow text is unchanged; the opt-in last step spells the PIN, nothing else."""
    turns = [
        ("मुझे बैटरी के बारे में जानना था।", None), ("2305073 AA for aeroplane", None), ("हाँ।", YES),
        ("हाँ सही है।", YES), ("ठीक है।", YES), ("हां।", YES), ("हाँ।", YES), ("Yes", YES),
        ("मैट्रिक फूल ही है। Okay", YES), ("हाँ।", YES),
    ]
    said = [text for text, _trace in await replay(monkeypatch, v56, "cv_4cec", turns)]
    record = ("क्या आपके outlet का नाम Ram Ratan Vastralaya, T-I-D number two, three, zero, five, zero, seven, "
              "three, letter A, और address Ward Number 12 Shop Number 14 Gola Bazar Sherghati Gaya, Bihar {pin} है? "
              "कृपया confirm करने के लिए हाँ या ना में बताइए।")
    assert said[9] == record.format(pin="824211")                           # today
    assert sanitize_for_tts(said[9], english_identifier_digits_enabled=True) == record.format(
        pin="eight, two, four, two, one, one")                              # with the tenant flag
    for text in said:                                                        # nothing else changes
        if text != said[9]:
            assert sanitize_for_tts(text, english_identifier_digits_enabled=True) == text
    assert said[4] == "क्या आपके terminal का model Antera AL9220U Handheld GPRS है? कृपया हाँ या ना में बताइए।"
