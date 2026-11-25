"""An intent hub with ``answerOptions`` reads what a reply MEANS.

Regression for the DEMO review of 2026-11-25 (haier/qa/REPORT_2026-11-25.md):
word lists decided every answer, so "हाँ जी मैम, कर दो" was re-asked, a bare
"नहीं" inside a complaint cancelled a work order, and "hello" at a
clarification closed the call as unconfirmed.
"""
from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from shared.orchestration.workflow_engine import _awaiting_options, build_definition_graph

QUESTION = "क्या आपको service की requirement है?"
CLARIFY = "एक बार confirm कर दूँ, क्या आपको service की requirement है?"
OPTIONS = [
    {"id": "yes", "meaning": "They need the service", "to": "m_yes", "signal": "affirm"},
    {"id": "no", "meaning": "They do not need the service", "to": "m_no", "signal": "refusal"},
    {"id": "later", "meaning": "They want it later", "to": "m_later"},
    {"id": "broken", "meaning": "Leads nowhere", "to": "missing_node"},
]


def definition(*, options=True, captures=True, proposal=True):
    """A hub shaped like the reviewed ones: yes/no label edges, an else edge to
    a clarification, and a catch-all route capture behind a correction edge."""
    hub_config = {"prompt": QUESTION, "textByLanguage": {"hi": QUESTION}, "elseIsAnswer": True}
    if captures:
        hub_config.update({
            "correctionCaptureVariable": "route",
            "alsoCapture": [
                {"variable": "route", "overwrite": True, "entity": {
                    "dataType": "text", "matchCanonicalValues": False,
                    "synonymPatterns": {"unclear": [r"[\s\S]+$"]}}},
                {"variable": "route", "overwrite": True, "entity": {
                    "dataType": "text", "matchCanonicalValues": False,
                    "synonymPatterns": {"stop": [r"कॉल मत करो"]}}},
            ],
        })
    if options:
        hub_config["answerOptions"] = OPTIONS
        if proposal:
            hub_config["answerAgreementWords"] = True
    clarify_config = {"prompt": CLARIFY, "textByLanguage": {"hi": CLARIFY}, "elseIsAnswer": False}
    if options:
        clarify_config["answerOptions"] = OPTIONS[:2]
    nodes = [
        {"id": "start", "kind": "start"},
        {"id": "hub", "kind": "intent", "config": hub_config},
        {"id": "clarify", "kind": "intent", "config": clarify_config},
        {"id": "route_stop", "kind": "condition", "config": {
            "variable": "route", "operator": "equals", "value": "stop"}},
        *[{"id": node_id, "kind": "message", "config": {
            "silent": True, "terminalOutcome": True, "setSlots": {"outcome": outcome}}}
          for node_id, outcome in (("m_yes", "yes"), ("m_no", "no"), ("m_later", "later"),
                                   ("m_stop", "stop"), ("m_unverified", "unverified"))],
        {"id": "end", "kind": "end", "config": {"text": "धन्यवाद।", "textByLanguage": {"hi": "धन्यवाद।"}}},
    ]
    edges = [
        {"id": "e0", "from": "start", "to": "hub"},
        {"id": "e_yes", "from": "hub", "to": "m_yes", "label": "yes/haan/हाँ/ठीक/chahiye/चाहिए"},
        {"id": "e_no", "from": "hub", "to": "m_no", "label": "no/nahi/नहीं"},
        {"id": "e_else", "from": "hub", "to": "clarify", "label": "else"},
        {"id": "c_yes", "from": "clarify", "to": "m_yes", "label": "yes/haan/हाँ"},
        {"id": "c_no", "from": "clarify", "to": "m_no", "label": "no/nahi/नहीं"},
        {"id": "c_else", "from": "clarify", "to": "m_unverified", "label": "else"},
        {"id": "r_true", "from": "route_stop", "to": "m_stop", "label": "true"},
        {"id": "r_false", "from": "route_stop", "to": "clarify", "label": "false"},
        *[{"id": f"x_{n}", "from": n, "to": "end"}
          for n in ("m_yes", "m_no", "m_later", "m_stop", "m_unverified")],
    ]
    if captures:
        edges.append({"id": "e_corr", "from": "hub", "to": "route_stop", "label": "correction"})
    return {"id": "wf_options", "name": "options", "nodes": nodes, "edges": edges}


_SERIAL = iter(range(10_000))


def turn(graph, text, *, hub="hub", signal=None, option=None, judged=False, retries=None,
         authoritative=True, thread=None):
    return asyncio.run(graph.ainvoke({
        "current_node": hub, "awaiting": hub, "user_text": text, "language": "hi-IN",
        "signal_override": signal, "signal_authoritative": authoritative,
        "option_override": option, "option_judged": judged,
        "slots": {}, "context_values": {}, "status": "collecting",
        "node_retries": dict(retries or {}), "audit": [],
    }, {"configurable": {"thread_id": thread or f"options-{next(_SERIAL)}"}}))


@pytest.fixture(scope="module")
def graph():
    return build_definition_graph(definition(), MemorySaver())


def outcome(result):
    return result["slots"].get("outcome")


def test_options_are_reported_without_invalid_entries():
    nodes = {n["id"]: n for n in definition()["nodes"]}
    assert _awaiting_options(definition(), nodes["hub"]) == [
        {"id": "yes", "meaning": "They need the service"},
        {"id": "no", "meaning": "They do not need the service"},
        {"id": "later", "meaning": "They want it later"},
    ]
    plain = definition(options=False)
    assert _awaiting_options(plain, {n["id"]: n for n in plain["nodes"]}["hub"]) is None
    assert _awaiting_options(definition(), nodes["end"]) is None


@pytest.mark.parametrize(("option", "expected"), [("yes", "yes"), ("no", "no"), ("later", "later")])
def test_the_chosen_option_decides_whatever_the_words_or_captures_say(graph, option, expected):
    result = turn(graph, "नहीं मैम, ये सही करके नहीं दे रहे, कॉल मत करो ऐसा नहीं बोला", option=option, judged=True)
    assert outcome(result) == expected
    assert result["answered_option"] == option
    assert "clarify" not in result["trace"] and "route_stop" not in result["trace"]


@pytest.mark.parametrize("text", ["जी।", "हाँ", "ठीक है।", "हाँ जी मैम", "Yes ma'am.", "जी मैडम।", "ओके", "बिल्कुल", "ok sir", "ठीक है, थैंक यू"])
def test_a_plain_yes_takes_the_yes_option_past_the_catch_all_capture(graph, text):
    result = turn(graph, text, signal="affirm")
    assert outcome(result) == "yes" and result["answered_option"] == "yes"


@pytest.mark.parametrize("text", ["नहीं", "जी नहीं", "नहीं मैम", "No.", "बिल्कुल नहीं", "नहीं, धन्यवाद"])
def test_a_plain_no_takes_the_no_option(graph, text):
    result = turn(graph, text, signal="refusal")
    assert outcome(result) == "no" and result["answered_option"] == "no"


@pytest.mark.parametrize("text", ["हम्म।", "हेलो।", "हेलो हेलो", "हाँ बोलिए।", "जी बताइए", "थैंक यू।", "धन्यवाद", "bye", "हाँ?", "हाँ, हेलो।"])
def test_a_reaction_is_not_an_answer_and_the_question_is_asked_again(graph, text):
    result = turn(graph, text, signal="affirm")
    assert outcome(result) is None and result["awaiting"] == "hub"
    assert result["reply"] == QUESTION
    assert result["answered_option"] is None
    assert not result["off_script"]


def test_reactions_are_bounded_by_the_else_edge(graph):
    thread = "reaction-bound"
    first = turn(graph, "हेलो।", thread=thread)
    second = turn(graph, "हेलो।", thread=thread, retries=first["node_retries"])
    third = turn(graph, "हेलो।", thread=thread, retries=second["node_retries"])
    assert first["awaiting"] == second["awaiting"] == "hub"
    assert third["awaiting"] == "clarify" and third["reply"] == CLARIFY


@pytest.mark.parametrize("signal", [None, "complaint", "question", "clarify", "hold"])
def test_a_judged_reply_with_no_option_is_left_to_the_brain_not_to_the_word_lists(graph, signal):
    result = turn(graph, "ये सही करके नहीं दे रहे आपके टेक्नीशियन", signal=signal, judged=True)
    assert outcome(result) is None and result["awaiting"] == "hub"
    assert result["off_script"] and "clarify" not in result["trace"]


def test_judged_misses_take_the_else_edge_after_one_contextual_reply(graph):
    thread = "miss-bound"
    first = turn(graph, "पता नहीं क्या बोल रहे हो", judged=True, thread=thread)
    second = turn(graph, "अरे वो वाला", judged=True, thread=thread, retries=first["node_retries"])
    assert first["awaiting"] == "hub" and first["off_script"]
    assert second["awaiting"] == "clarify" and second["reply"] == CLARIFY


def test_questions_and_repeat_requests_never_count_as_misses(graph):
    retries = {}
    for _ in range(4):
        result = turn(graph, "कब तक आएगा?", signal="question", judged=True, retries=retries, thread="questions")
        retries = result["node_retries"]
        assert result["awaiting"] == "hub" and result["off_script"]


def test_without_a_judgement_the_authored_captures_still_route(graph):
    assert outcome(turn(graph, "अरे कॉल मत करो यार")) == "stop"
    unclear = turn(graph, "कुछ और बात है")
    assert unclear["awaiting"] == "clarify"


def test_an_unknown_option_id_is_ignored(graph):
    result = turn(graph, "कुछ और बात है", option="broken")
    assert result["awaiting"] == "clarify"
    result = turn(graph, "कुछ और बात है", option="invented")
    assert result["awaiting"] == "clarify"


def test_a_bare_yes_no_word_inside_a_sentence_never_decides_an_option_hub():
    plain = build_definition_graph(definition(captures=False), MemorySaver())
    result = turn(plain, "ये सही करके नहीं दे रहे आपके टेक्नीशियन")
    assert outcome(result) is None
    assert any(e.get("action") == "generic_token_ignored" for e in result["audit"])
    assert outcome(turn(plain, "अरे वो तो ठीक है लेकिन")) is None
    # A content token of an edge still decides when nothing was judged.
    assert outcome(turn(plain, "service चाहिए मुझे")) == "yes"
    assert turn(plain, "service चाहिए मुझे")["answered_option"] == "yes"


def test_a_hub_without_options_keeps_its_authored_behaviour():
    legacy = build_definition_graph(definition(options=False, captures=False), MemorySaver())
    assert outcome(turn(legacy, "ये सही करके नहीं दे रहे आपके टेक्नीशियन")) == "no"
    assert outcome(turn(legacy, "हेलो हाँ")) == "yes"
    captured = build_definition_graph(definition(options=False), MemorySaver())
    assert turn(captured, "जी।", signal="affirm")["awaiting"] == "clarify"
    assert turn(captured, "जी।", signal="affirm")["answered_option"] is None


def test_the_clarification_hub_gets_the_same_contract(graph):
    assert outcome(turn(graph, "जी।", hub="clarify", signal="affirm")) == "yes"
    hello = turn(graph, "हलो।", hub="clarify")
    assert hello["awaiting"] == "clarify" and hello["reply"] == CLARIFY
    assert outcome(hello) is None


@pytest.mark.parametrize("text", ["ठीक है।", "सही है", "ओके", "ok sir", "fine", "ठीक है, थैंक यू", "हाँ ठीक है"])
def test_agreement_words_are_a_yes_only_where_the_hub_proposes_something(text):
    status = build_definition_graph(definition(proposal=False), MemorySaver())
    result = turn(status, text, signal="affirm")
    assert outcome(result) != "yes" and result["answered_option"] != "yes"
    proposal = build_definition_graph(definition(), MemorySaver())
    assert outcome(turn(proposal, text, signal="affirm")) == "yes"


@pytest.mark.parametrize("text", ["हाँ", "जी।", "हाँ जी मैम", "Yes ma'am.", "बिल्कुल", "जी हाँ"])
def test_explicit_yes_words_are_a_yes_at_every_option_hub(text):
    status = build_definition_graph(definition(proposal=False), MemorySaver())
    assert outcome(turn(status, text, signal="affirm")) == "yes"


def test_an_authored_phrase_containing_a_yes_word_is_not_a_generic_token():
    from shared.orchestration.workflow_engine import _bare_yes_no_token
    for token in ("नहीं", "ठीक है", "जी हाँ", "haan ji", "no", "जी", "ok"):
        assert _bare_yes_no_token(token), token
    for token in ("अब ठीक है", "कोई विजिट नहीं", "पैसे नहीं", "ho gaya", "theek ho gaya", "service chahiye"):
        assert not _bare_yes_no_token(token), token
