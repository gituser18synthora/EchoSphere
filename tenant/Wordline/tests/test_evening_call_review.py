"""17 November evening review: 25 DEMO calls on workflow v54 / release v0.1.33.

Every caller line below is the transcript text of the call named beside it.
The saved v54 definition (``data/workflow_demo_v54_snapshot.json``) is repaired
with ``repair_evening_review`` and run through the real workflow engine; the
api node posts to the real merchant API code (FastAPI TestClient) with the
real connection mapping, exactly as ``test_visit_details`` does. What the test
calls "TTS text" is the engine reply after the TTS router's own text
preparation (``sanitize_for_tts``).

Not simulated: STT, audio timing, barge-in, the LLM and the intent classifier
(its recorded label is passed as ``signal``).

Run:  env/bin/pytest tenant/Wordline/tests/test_evening_call_review.py -q
"""

from __future__ import annotations

import copy
import json
import pathlib
import re

import pytest

from shared.audio.text import sanitize_for_tts
from shared.orchestration.entity_extractor import extract_entity
from shared.orchestration.identifier_confirmation import confirmation_answer
from shared.orchestration.workflow_engine import (
    _choose_intent_edge_detailed, _edge_meta, _node_config,
)
from tenant.Wordline.setup.call_review_repair import (
    ANYTHING_ELSE_HUBS, DIGIT_RULE_NEW, MORE_HELP, MORE_HELP_ID, repair_anything_else_routing,
    repair_evening_review, repair_prompt_digit_rule,
)
from tenant.Wordline.tests.test_visit_details import CONTEXT, _ApiExecutor, _engine

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "data" / "workflow_demo_v54_snapshot.json"
TENANT, BOT, WORKFLOW = "tn_ccc488c2c47d", "bot_a9d4caca70dc", "wf_0a6550d061e6"
YES, NO = "affirm", "refusal"
DIGIT_WORDS = "zero one two three four five six seven eight nine".split()


@pytest.fixture(scope="module")
def live() -> dict:
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def repaired(live) -> dict:
    return repair_evening_review(live)


def spelled(value: str) -> str:
    return ", ".join(DIGIT_WORDS[int(c)] if c.isdigit() else "letter " + c for c in value)


async def replay(monkeypatch, definition, session, turns, lang="hi-IN"):
    """[(caller text, classifier signal)] → [(TTS text, trace)] per turn."""
    engine, _executor = _engine(monkeypatch, definition, _ApiExecutor)
    spoken = []
    for text, signal in turns:
        result = await engine.handle_turn_detailed(
            session_id=session, tenant_id=TENANT, bot_id=BOT, workflow_name=WORKFLOW,
            user_text=text, language=lang, context_values=CONTEXT, signal=signal)
        spoken.append((sanitize_for_tts(result.get("reply") or ""), result.get("trace") or []))
    return spoken


# ── the repair itself ───────────────────────────────────────────────────────


def test_repair_only_adds_and_is_idempotent(live, repaired):
    frozen = copy.deepcopy(live)
    assert repair_evening_review(live) == repaired and live == frozen
    assert repair_evening_review(repaired) == repaired
    before = {n["id"]: n for n in live["nodes"]}
    after = {n["id"]: n for n in repaired["nodes"]}
    assert set(after) - set(before) == {MORE_HELP_ID}
    assert [n["id"] for n in repaired["nodes"]][:len(live["nodes"])] == [n["id"] for n in live["nodes"]]
    changed = {name for name in before if before[name] != after[name]}
    assert changed == {
        "n_ask_tid__confirm", "n_tid_retry__confirm", "n_bat_tid__confirm", "n_bat_tid_retry__confirm",
        "n_bat_contact", "n_bat_visit_contact", "n_pr_visit_contact",
        "n_bat_visit_record", "n_pr_visit_record", "n_bat_docket", "n_low_docket", "n_pr_docket",
        "n_sms", "n_ivr", "n_both",
    }
    for name in changed:  # position, kind and label never move
        assert {k: v for k, v in before[name].items() if k != "config"} == \
               {k: v for k, v in after[name].items() if k != "config"}
    assert repaired["edges"][:len(live["edges"])] == live["edges"]
    added = {(e["from"], e["to"]) for e in repaired["edges"][len(live["edges"]):]}
    assert added == {
        (MORE_HELP_ID, "n_end"), (MORE_HELP_ID, "n_bat_switch_off"),
        (MORE_HELP_ID, "n_pr_again"), (MORE_HELP_ID, "n_bat_low80_confirm"),
        *((hub, MORE_HELP_ID) for hub in ANYTHING_ELSE_HUBS),
        ("n_sms", "n_ivr"), ("n_sms", "n_raise_has_record"),
        ("n_ivr", "n_sms"), ("n_ivr", "n_raise_has_record"), ("n_both", "n_raise_has_record"),
    }


def test_confirmation_gates_lose_the_lookup_table_and_keep_the_template(live, repaired):
    for gate in ("n_ask_tid__confirm", "n_tid_retry__confirm", "n_bat_tid__confirm", "n_bat_tid_retry__confirm"):
        config = next(n for n in repaired["nodes"] if n["id"] == gate)["config"]
        old = next(n for n in live["nodes"] if n["id"] == gate)["config"]["readback"]
        new = config["readback"]
        for locale in ("en", "hi"):
            assert len(old[locale]["fields"][0]["values"]) == 209
            assert new[locale]["fields"] == [{
                "template": "Terminal ID {value}.", "variable": config["confirmsIdentifier"],
                "format": "identifier"}]
            assert new[locale]["question"] == old[locale]["question"]


def test_unrecognised_workflow_is_refused(live):
    broken = {**live, "nodes": [n for n in live["nodes"] if n["id"] != "n_both"]}
    with pytest.raises(ValueError):
        repair_anything_else_routing(broken)


def test_prompt_rule_is_tightened_once():
    old = ("# X\nWhenever generating a reply that reads an ID, reference, docket, phone number or SMS\n"
           "destination, say every digit separately in English, including in Hindi replies: zero, one,\n"
           "two, three, four, five, six, seven, eight, nine. Name alphabetic characters explicitly,\n"
           "for example letter A. Keep leading zeroes. This rule does not change the pronunciation\n"
           "of amounts, quantities, percentages, durations or terminal model numbers such as Linura 7220.\n")
    new = repair_prompt_digit_rule(old)
    assert DIGIT_RULE_NEW in new and "never\ngroup digits" in new
    assert "terminal model numbers such as Linura 7220" in new
    assert repair_prompt_digit_rule(new) == new
    with pytest.raises(ValueError):
        repair_prompt_digit_rule("no such rule")


# ── Terminal ID capture: every dictation of the 25 calls + the 4 earlier ones ─

TID_UTTERANCES = [
    # (call, transcript text, expected capture or None = ask again)
    ("cv_40ec07f737d3", "6 5 0 79917", "65079917"),
    ("cv_40ec07f737d3", "हाँ। 6 5 0", None),
    ("cv_d0e7a288dfa3", "A for S one", None),
    ("cv_d0e7a288dfa3", "Yes 0701014 One is for Apple", None),
    ("cv_d0e7a288dfa3", "Yes 2894 358 A for Apple", "2894358A"),
    ("cv_c337303f00bb", "बैटरी के बारे में जानकारी चाहिए, मैं लेटर बताता हूँ। 309 Spice One A, A for Apple.", None),
    ("cv_c337303f00bb", "309 Double Six 5 1 A, A for Apple", "3096651A"),
    ("cv_2e70dfaa6716", "मेरी आईडी है 254408। Three A, A for Aamir Khan", "2544083A"),
    ("cv_928e8a640565", "2305073 A for Apple", "2305073A"),
    ("cv_ae9c04c2b34d", "Hmm 65079917", "65079917"),
    ("cv_b4826d0ec96b", "ID hai 0710141 A for Apple", "0710141A"),
    ("cv_b14a8c89cb55", "1317317 A for Apple", "1317317A"),
    ("cv_074c830e7ff8", "हाँ लिखो 30685। 40A for Apple", "3068540A"),
    ("cv_30d9cfe2b71c", "Okay, 32577872", "32577872"),
    ("cv_c5a83af7fa57", "अच्छा, ठीक है। Size Zero Seven Double Nine One Seven", None),
    ("cv_c5a83af7fa57", "मुझे पेपर की जानकारी चाहिए, मेरा आईडी है थ्री वन। Three Seven Five Zero Two A, A for Apple. हम।", "3137502A"),
    ("cv_b79eba6aa27f", "हाँ लिखो 3022। Six Five Zero For Act.", "3022650"),
    ("cv_b79eba6aa27f", "3022650 A for Apple", "3022650A"),
    ("cv_8358a2aef66b", "हाँ लिखो वन थ्री वन सेवन थ्री वन सेवन। A for Alpha", "1317317A"),
    ("cv_8358a2aef66b", "हां जी। 1964939 A for Alpha", "1964939A"),          # was G1964939A
    ("cv_a0078ff4229b", "हां लिखो वन तीन नाइन सेवन नाइन। 2604 Five Three", None),
    ("cv_a0078ff4229b", "79 2604 Five Three", "79260453"),
    ("cv_c6b2d1b8daac", "Yes, 1964939 A for Alpha", "1964939A"),
    ("cv_49d1b4053aa9", "3137502A", "3137502A"),
    ("cv_b32f1a582299", "Three 5 7 7 8 7 2", "3577872"),
    ("cv_a778fd1545af", "1 4 3 2 4 2 1 A for Alpha", "1432421A"),
    ("cv_02de2834362b", "A for Alpha 2129007", "A2129007"),
    ("cv_69b90d667569", "8 2 1 29007", "82129007"),
    ("cv_69b90d667569", "A 2 1 2 9 0 0 7", "A2129007"),
    # the four calls reviewed earlier the same day
    ("cv_411751e0bb12", "1 4 3 2 4 2 1 A for Apple", "1432421A"),
    ("cv_8fececf625d0", "मेरा टर्मिनल आई टी है वन वन थ्री टू नाइन एट सिक्स ए फोर अल्फा।", "1132986A"),
    ("cv_8fececf625d0", "हाँ। Permanent ID hai 1132986 A for Apple.", "1132986A"),
    ("cv_e6bcef78b811", "416672 5 MM for mango", "4166725M"),
    ("cv_e6bcef78b811", "4166725 M for Mango", "4166725M"),
    ("cv_6af0286fd750", "मेरा आईडी टर्मिनल आईडी है १ २ ३ ४ and B, B for boy.", None),
    ("cv_6af0286fd750", "एक दो तीन चार पाँच", None),
]


@pytest.mark.parametrize("call,text,expected", TID_UTTERANCES)
def test_terminal_id_capture_of_every_reviewed_dictation(live, call, text, expected):
    ask = next(n for n in live["nodes"] if n["id"] == "n_ask_tid")
    entity = {**ask["config"]["entity"], "name": "spoken_terminal_id"}
    result = extract_entity(text, entity)
    assert (result["value"] if result["matched"] else None) == expected, call


@pytest.mark.parametrize("text", [
    "हां जी। 1964939 A for Alpha", "हाँ जी 1964939 A", "जी हाँ, 1964939 A", "जी। 1964939 A",
    "हाँ जी जी 1964939 A", "ठीक है जी 1964939 A",
])
def test_honorific_ji_is_never_the_letter_g(live, text):
    ask = next(n for n in live["nodes"] if n["id"] == "n_ask_tid")
    result = extract_entity(text, {**ask["config"]["entity"], "name": "spoken_terminal_id"})
    assert result["value"] == "1964939A"


def test_a_spelled_g_is_still_captured(live):
    ask = next(n for n in live["nodes"] if n["id"] == "n_ask_tid")
    entity = {**ask["config"]["entity"], "name": "spoken_terminal_id"}
    assert extract_entity("जी फॉर गोल्फ 1234567", entity)["value"] == "G1234567"
    assert extract_entity("G for Golf 1234567", entity)["value"] == "G1234567"


# ── confirmation answers heard at the ID readback ───────────────────────────

CONFIRMATIONS = [
    ("cv_c337303f00bb", "हाँ, यह सही है।", "yes"),                 # was re-prompted
    ("cv_40ec07f737d3", "बरोबर.", "yes"),                          # was re-prompted
    ("cv_40ec07f737d3", "हां।", "yes"),
    ("cv_d0e7a288dfa3", "Yes", "yes"),
    ("cv_2e70dfaa6716", "हाँ, सही है।", "yes"),
    ("cv_b14a8c89cb55", "जी हाँ।", "yes"),
    ("cv_30d9cfe2b71c", "हांजी हांजी।", "yes"),
    ("cv_b79eba6aa27f", "हाँ जी सही है।", "yes"),
    ("cv_ae9c04c2b34d", "हांजी।", "yes"),
    ("cv_a0078ff4229b", "हाँ जी। हाँ, हाँ, सही है।", "yes"),
    ("cv_b79eba6aa27f", "नहीं।", "no"),
    ("cv_69b90d667569", "नहीं।", "no"),
    ("cv_8358a2aef66b", "हेलो", None),
    ("cv_40ec07f737d3", "सात नौ नौ एक। Seven", None),
    # earlier calls
    ("cv_411751e0bb12", "Okay Yes, it's correct.", "yes"),
    ("cv_8fececf625d0", "It's correct.", "yes"),
    ("cv_411751e0bb12", "क्या? Yes लगा दीजिए।", None),
    ("cv_6af0286fd750", "नहीं, ये सही नहीं है।", "no"),
    # natural forms, and what must still not confirm
    ("-", "ठीक है", "yes"), ("-", "theek hai", "yes"), ("-", "यही है", "yes"),
    ("-", "बिल्कुल करेक्ट आपने कहा है। यही है।", "yes"),
    ("-", "Okay", None), ("-", "it is", None), ("-", "सही है क्या?", None),
    ("-", "हाँ लेकिन last में A है", None), ("-", "ठीक नहीं है", None),
]


@pytest.mark.parametrize("call,text,expected", CONFIRMATIONS)
def test_confirmation_answer_of_every_reviewed_reply(call, text, expected):
    assert confirmation_answer(text) == expected, call


# ── full replays: what is sent to TTS ───────────────────────────────────────


async def test_cv_2e70dfaa6716_battery_visit_reads_every_number_in_english(monkeypatch, repaired):
    turns = [
        ("हाँ मनन, मेरी बैटरी फूलती जा रही है, मेरी बैटरी में बहुत प्रॉब्लम हो रहा है।", None),
        ("मेरी आईडी है 254408। Three A, A for Aamir Khan", None),
        ("हाँ, सही है।", YES), ("हाँ। हा हा हा।", YES), ("Yes", YES), ("हां।", YES),
        ("हाँ हाँ।", YES), ("हो गया।", YES), ("फूल गया है।", None), ("हाँ।", YES), ("हाँ।", YES),
        ("मेरा वीकली ऑफ सैटरडे संडे है और प्रेफर्ड टाइम है सुबह नौ बजे से दस बजे तक", None),
        ("हां हां।", YES), ("ठीक है।", YES),
    ]
    said = [text for text, _trace in await replay(monkeypatch, repaired, "cv_2e70", turns)]
    assert said[1] == "Terminal ID two, five, four, four, zero, eight, three, letter A. क्या यह सही है?"
    assert said[5] == ("क्या आपका registered contact number nine, eight, eight, zero, nine, zero, five, "
                       "one, nine, five है? कृपया हाँ या ना में बताइए।")
    assert said[9] == ("क्या आपके outlet का नाम Sadhana, T-I-D number two, five, four, four, zero, eight, "
                        "three, letter A, और address M S Sadhana Ganadhi Quare, Karnataka 570001 है? "
                        "कृपया confirm करने के लिए हाँ या ना में बताइए।")
    assert said[11] == ("हमारे records के अनुसार contact person Diliplkumar Jain हैं और contact number nine, "
                        "eight, eight, zero, nine, zero, five, one, nine, five है। "
                        "कृपया confirm करने के लिए हाँ या ना में बताइए।")
    # The docket of the call data, not dropped and not numerals.
    assert said[13].startswith(
        "सर, आपका service docket successfully raise हो गया है। आपका docket number है "
        "letter W, letter L, four, five, eight, nine, two, one। कृपया इसे future reference के लिए note कर लीजिए।")
    assert said[13].endswith("सर क्या मैं आज आपकी किसी और चीज़ में मदद कर सकता हूँ?")
    # Model numbers keep their normal form.
    assert said[4] == "क्या आपके terminal का model Antera AL9220U Handheld GPRS है? कृपया हाँ या ना में बताइए।"
    for index in (1, 5, 11, 13):
        assert not re.search(r"\d", said[index]), said[index]


async def test_same_readbacks_in_english(monkeypatch, repaired):
    turns = [("battery issue", None), ("2544083 A for Apple", None), ("yes", YES), ("yes", YES), ("yes", YES),
             ("yes", YES)]
    said = [text for text, _trace in await replay(monkeypatch, repaired, "en_readback", turns, lang="en-IN")]
    assert said[1] == "Terminal ID two, five, four, four, zero, eight, three, letter A. Is that correct?"
    assert said[5] == ("Is your registered contact number nine, eight, eight, zero, nine, zero, five, one, "
                       "nine, five? Please answer yes or no.")


async def test_cv_40ec07f737d3_paper_roll_raise(monkeypatch, repaired):
    turns = [
        ("रोल चाहिए भाई।", None), ("6 5 0 79917", None),
        ("बरोबर.", YES),                                   # was: "ID सही हो तो हाँ बोलिए…"
        ("हाँ। हाँ, सही है।", YES), ("हांजी।", YES), ("हां।", YES),
        ("मेरी तरफ़ से रिक्वेस्ट है।", None), ("हो जाता है। सही है।", YES), ("हाँ। हाँ।", YES),
        ("Saturday Sunday off hai and weekly working hours hain Five Two Morning 5 to 12", None),
        ("हाँ।", YES), ("Eight", None),
        ("हाँ, बताइए।", YES),                              # was: goodbye (cv_c5a83af7fa57 turn 41)
        ("नहीं नहीं नहीं।", NO),
    ]
    spoken = await replay(monkeypatch, repaired, "cv_40ec", turns)
    said = [text for text, _trace in spoken]
    assert said[1] == "Terminal ID six, five, zero, seven, nine, nine, one, seven. क्या यह सही है?"
    assert "n_api_lookup" in spoken[2][1] and "La Mode Fashions Private Limited" in said[2]
    assert said[7] == ("क्या आपके outlet का नाम La Mode Fashions Private Limited, T-I-D number six, five, zero, "
                       "seven, nine, nine, one, seven, और address A1 307, Safdarjung Enclave, New Delhi है? "
                       "कृपया confirm करने के लिए हाँ या ना में बताइए।")
    assert said[9] == ("हमारे records के अनुसार contact person Gaurav Gupta हैं और contact number nine, eight, "
                        "nine, nine, zero, six, zero, seven, one, zero है। "
                        "कृपया confirm करने के लिए हाँ या ना में बताइए।")
    assert said[10] == "आपको कितने paper rolls चाहिए? Quantity one से nine के बीच होनी चाहिए।"
    assert said[11] == ("आपकी paper roll request successfully raise हो गई है। कृपया future reference के लिए "
                        "docket number note कर लीजिए। आपका docket number है letter W, letter L, four, five, "
                        "eight, nine, two, one। सर क्या मैं आज आपकी किसी और चीज़ में मदद कर सकता हूँ?")
    assert said[12] == MORE_HELP["hi"] and spoken[12][1] == ["n_anything_else", MORE_HELP_ID]
    assert spoken[13][1] == [MORE_HELP_ID, "n_end"]


async def test_cv_8358a2aef66b_unheard_readback_is_read_again_and_ji_is_not_g(monkeypatch, repaired):
    turns = [
        ("मनन मुझे पेपर रोल रेस करना है।", None),
        ("हाँ लिखो वन थ्री वन सेवन थ्री वन सेवन। A for Alpha", None),
        ("हेलो", None),                                    # the readback had produced no audio
        ("हां जी। 1964939 A for Alpha", None),             # was read back as "letter G, one, nine, …"
        ("हाँ, सही है।", YES), ("हाँ, सही है।", YES), ("हाँ, सही है।", YES), ("हाँ, सही है।", YES),
        ("आई वी आर।", None),
        ("Yes", YES),                                      # was: goodbye
    ]
    spoken = await replay(monkeypatch, repaired, "cv_8358", turns)
    said = [text for text, _trace in spoken]
    assert said[2] == ("Terminal ID one, three, one, seven, three, one, seven, letter A. "
                       "ID सही हो तो हाँ बोलिए, वरना पूरी सही ID दोबारा बताइए।")
    assert said[3] == "Terminal ID one, nine, six, four, nine, three, nine, letter A. क्या यह सही है?"
    assert "WYT Hospitality Services" in said[4]
    assert spoken[8][1] == ["n_options", "n_ivr"]
    assert said[9] == MORE_HELP["hi"] and spoken[9][1] == ["n_ivr", MORE_HELP_ID]


async def test_cv_c337303f00bb_natural_confirmation_reaches_the_lookup(monkeypatch, repaired):
    turns = [("हाँ। मुझे बैटरी के बारे में जानकारी चाहिए।", None),
             ("309 Double Six 5 1 A, A for Apple", None), ("हाँ, यह सही है।", YES)]
    spoken = await replay(monkeypatch, repaired, "cv_c337", turns)
    assert spoken[1][0] == "Terminal ID three, zero, nine, six, six, five, one, letter A. क्या यह सही है?"
    assert "n_bat_api_lookup" in spoken[2][1] and "Labham Supermarket" in spoken[2][0]


async def test_cv_49d1b4053aa9_request_to_raise_after_sms_no_longer_ends_the_call(monkeypatch, repaired):
    turns = [("आप मुझे पेपर रोल के बारे में बताइए।", None), ("3137502A", None), ("हाँ।", YES),
             ("हाँ, सही है।", YES), ("हाँ।", YES), ("हाँ।", YES), ("हाँ, एसएमएस का तरीक़ा बताइए।", None),
             ("हाँ जी। हाँ, मुझे समझ ही नहीं आया क्या आप रिक्वेस्ट राइज कर सकते हो आप खुद ही।", None)]
    spoken = await replay(monkeypatch, repaired, "cv_49d1", turns)
    assert spoken[6][1] == ["n_options", "n_sms"]
    assert "five, seven, five, seven, five, eight" in spoken[6][0]
    assert spoken[7][1] == ["n_sms", "n_raise_has_record", "n_raise"]


async def test_cv_b79eba6aa27f_ivr_after_sms_is_explained_not_refused(monkeypatch, repaired):
    turns = [("मैम मुझे पेपर रोल्स। चाहिए मेरा पेपर रोल खत्म हो गया है।", None),
             ("हाँ लिखो 3022। Six Five Zero For Act.", None), ("नहीं।", NO), ("3022650 A for Apple", None),
             ("हाँ जी सही है।", YES), ("हाँ, सही है।", YES), ("हाँ, सही है।", YES), ("हाँ, सही है।", YES),
             ("SMS", None), ("आईवीआर बताओ।", "question")]
    spoken = await replay(monkeypatch, repaired, "cv_b79e", turns)
    assert spoken[1][0] == "Terminal ID three, zero, two, two, six, five, zero. क्या यह सही है?"
    assert spoken[2][1][-1] == "n_ask_tid"
    assert spoken[9][1] == ["n_sms", "n_ivr"]
    assert "One eight six zero, two three three two, three three two पर call कीजिए।" in spoken[9][0]


# ── hub routing: what changes and what must not ─────────────────────────────


def _choice(definition, hub, text, signal):
    nodes = {n["id"]: n for n in definition["nodes"]}
    meta = _edge_meta([e for e in definition["edges"] if e["from"] == hub], {})
    edge, _why, _token = _choose_intent_edge_detailed(
        meta, text, signal, option_hub=_node_config(nodes[hub]).get("optionHub") is True)
    return edge["to"] if edge else None


@pytest.mark.parametrize("call,hub,text,signal,before,after", [
    ("cv_c5a83af7fa57", "n_anything_else", "हाँ, बताइए।", YES, "n_end", MORE_HELP_ID),
    ("cv_8358a2aef66b", "n_ivr", "Yes", YES, "n_end", MORE_HELP_ID),
    ("cv_c6b2d1b8daac", "n_anything_else", "हाँ जी।", YES, "n_end", MORE_HELP_ID),
    ("cv_49d1b4053aa9", "n_sms",
     "हाँ जी। हाँ, मुझे समझ ही नहीं आया क्या आप रिक्वेस्ट राइज कर सकते हो आप खुद ही।", None,
     "n_end", "n_raise_has_record"),
    ("cv_b79eba6aa27f", "n_sms", "आईवीआर बताओ।", "question", None, "n_ivr"),
    ("cv_b79eba6aa27f", "n_sms", "नहीं, मुझे आईबीआर वाला जानना है।", NO, "n_end", "n_end"),  # STT wrote आईबीआर
    ("-", "n_sms", "नहीं, IVR बताओ", NO, "n_end", "n_ivr"),
    ("-", "n_low_anything_else", "Yes", YES, "n_low_end", MORE_HELP_ID),
    # closings are untouched
    ("cv_40ec07f737d3", "n_anything_else", "नहीं नहीं नहीं।", NO, "n_end", "n_end"),
    ("cv_d0e7a288dfa3", "n_anything_else", "नहीं। Thank you", NO, "n_end", "n_end"),
    ("cv_074c830e7ff8", "n_anything_else", "नहीं, ठीक है।", NO, "n_end", "n_end"),
    ("cv_b4826d0ec96b", "n_sms", "नहीं।", NO, "n_end", "n_end"),
    ("cv_a0078ff4229b", "n_anything_else", "No no thank you", NO, "n_end", "n_end"),
    ("-", "n_anything_else", "ठीक है", YES, "n_end", "n_end"),
    # a second request still starts its flow
    ("cv_c6b2d1b8daac", "n_ivr", "हाँ, मेरी बैटरी की भी समस्या है, बैटरी सही नहीं है।", None,
     "n_bat_switch_off", "n_bat_switch_off"),
    ("cv_a778fd1545af", "n_sms", "एक्चुअली मेरी बैटरी में भी इशू है।", None,
     "n_bat_switch_off", "n_bat_switch_off"),
])
def test_anything_else_hub_routing(live, repaired, call, hub, text, signal, before, after):
    assert _choice(live, hub, text, signal) == before, call
    assert _choice(repaired, hub, text, signal) == after, call


def test_more_help_never_closes_on_a_second_yes(repaired):
    assert _choice(repaired, MORE_HELP_ID, "हाँ", YES) is None
    assert _choice(repaired, MORE_HELP_ID, "Yes", YES) is None
    assert _choice(repaired, MORE_HELP_ID, "नहीं, बस", NO) == "n_end"
    assert _choice(repaired, MORE_HELP_ID, "पेपर रोल चाहिए", None) == "n_pr_again"
    assert _choice(repaired, MORE_HELP_ID, "बैटरी की समस्या है", None) == "n_bat_switch_off"
