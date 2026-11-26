"""Answer options for the Haier rejected-WO workflow (2026-11-25 review)."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest
from langgraph.checkpoint.memory import MemorySaver

from haier.setup.haier_answer_options_repair import (
    MARKER, VERSION, _CHOICE_OPTIONS, _CHOICE_TWINS, _TOPIC_OPTIONS, _YES_NO_MEANINGS,
    _YES_NO_TARGETS, apply_answer_options_repair, hubs_with_options, options_for,
)
from shared.orchestration.workflow_engine import build_definition_graph

RT, UNV, NOSVC, STOP = "m_n_end_redispatch_thanks", "m_n_v2_end_unverified", "m_n_end_no_service", "m_n_v2_end_repeat_calls"


def fixture():
    """The permission, service-need and who-fixed-it steps with the catch-all
    route capture that used to send every unlisted reply to the clarification."""
    nodes = [{"id": "start", "kind": "start"}]
    edges = []

    def hub(node_id, question, *, captured=True):
        config = {"prompt": question, "textByLanguage": {"hi": question}, "elseIsAnswer": False}
        if captured:
            config.update({"correctionCaptureVariable": f"route_{node_id}", "alsoCapture": [{
                "variable": f"route_{node_id}", "overwrite": True, "entity": {
                    "dataType": "text", "matchCanonicalValues": False,
                    "synonymPatterns": {"unclear": [r"[\s\S]+$"]}}}]})
        nodes.append({"id": node_id, "kind": "intent", "config": config})

    def edge(source, target, label="next"):
        edges.append({"id": f"e{len(edges)}", "from": source, "to": target, "label": label})

    def terminal(node_id, outcome):
        nodes.append({"id": node_id, "kind": "message", "config": {
            "silent": True, "terminalOutcome": True, "setSlots": {"outcome": outcome}}})
        edge(node_id, "end")

    for node_id, outcome in ((RT, "redispatch"), (UNV, "unverified_no_action"), (NOSVC, "cancel_no_service"),
                             (STOP, "repeat_calls_noted"), ("m_n_end_gift", "cancel_gift"),
                             ("m_n_end_resale", "cancel_resale"), ("m_n_end_resolved_tech", "redispatch_completion"),
                             ("m_n_end_resolved_auto", "cancel_resolved"), ("m_n_v2_other_issue", "other_issue_noted")):
        terminal(node_id, outcome)
    nodes.append({"id": "end", "kind": "end", "config": {"text": "धन्यवाद।"}})
    hub("n_redispatch", "क्या technician की visit के लिए मैं आपकी request आगे forward कर दूँ?")
    hub("n_haier_audit_redispatch_confirm", "क्या technician की visit वाली request आगे forward कर दूँ?")
    hub("n_service_need", "क्या आपको service की requirement है?")
    hub("n_service_need_clarify", "एक बार confirm कर दूँ, क्या आपको service की requirement है?")
    hub("n_resolved_how_q", "क्या ये problem Haier के technician ने ठीक की थी, या अपने आप ठीक हो गई?")
    hub("n_haier_followup_n_resolved_how_q_clarify", "Haier के technician ने ठीक की थी, या अपने आप?")
    # Later-date questions: only their place in the graph matters here.
    for node_id in ("n_v2_future_when", "n_future_confirm"):
        nodes.append({"id": node_id, "kind": "message", "config": {"text": node_id}})
        edge(node_id, "end")
    for permission, fallback in (("n_redispatch", "n_haier_audit_redispatch_confirm"),
                                 ("n_haier_audit_redispatch_confirm", UNV)):
        edge(permission, RT, "yes/haan/हाँ")
        edge(permission, "n_service_need_clarify", "no/nahi/नहीं")
        edge(permission, STOP, "stop calling")
        edge(permission, "n_v2_future_when", "abhi nahi aana")
        edge(permission, "n_future_confirm", "next month")
        edge(permission, "m_n_end_gift", "gift")
        edge(permission, "m_n_end_resale", "resale")
        edge(permission, fallback, "else")
        nodes.append({"id": f"c_{permission}", "kind": "condition", "config": {
            "variable": f"route_{permission}", "operator": "equals", "value": "never"}})
        edge(permission, f"c_{permission}", "correction")
        edge(f"c_{permission}", RT, "true")
        edge(f"c_{permission}", fallback, "false")
    for need, fallback in (("n_service_need", "n_service_need_clarify"), ("n_service_need_clarify", UNV)):
        edge(need, "n_redispatch", "yes/haan/हाँ/chahiye")
        edge(need, NOSVC, "no/nahi/नहीं")
        edge(need, STOP, "stop calling")
        edge(need, fallback, "else")
    for who, fallback in (("n_resolved_how_q", "n_haier_followup_n_resolved_how_q_clarify"),
                          ("n_haier_followup_n_resolved_how_q_clarify", UNV)):
        edge(who, "m_n_end_resolved_tech", "technician ne/टेक्नीशियन ने")
        edge(who, "m_n_end_resolved_auto", "apne aap/अपने आप")
        edge(who, "n_service_need", "theek nahi hua")
        edge(who, "m_n_v2_other_issue", "doosra issue")
        edge(who, fallback, "else")
    edge("start", "n_service_need")
    return {"id": "wf_1c3e59362c51", "bot_id": "bot_450a4f7b4832", "name": "fixture", "nodes": nodes, "edges": edges}


_SERIAL = iter(range(10_000))


def replay(definition, hub, text, signal=None, option=None, judged=False):
    graph = build_definition_graph(definition, MemorySaver())
    return asyncio.run(graph.ainvoke({
        "current_node": hub, "awaiting": hub, "user_text": text, "language": "hi-IN",
        "signal_override": signal, "signal_authoritative": True,
        "option_override": option, "option_judged": judged,
        "slots": {}, "context_values": {}, "status": "collecting", "node_retries": {}, "audit": [],
    }, {"configurable": {"thread_id": f"haier-options-{next(_SERIAL)}"}}))


@pytest.fixture(scope="module")
def repaired():
    return apply_answer_options_repair(fixture())


def test_every_intent_hub_gets_options_and_the_input_is_untouched(repaired):
    original = fixture()
    before = json.dumps(original, sort_keys=True)
    result = apply_answer_options_repair(original)
    assert json.dumps(original, sort_keys=True) == before
    hubs = hubs_with_options(result)
    assert set(hubs) == {n["id"] for n in original["nodes"] if n["kind"] == "intent"}
    for node in result["nodes"]:
        if node["id"] in hubs:
            assert node["config"][MARKER] == VERSION
    assert apply_answer_options_repair(result) == result


def test_no_option_adds_a_route_the_hub_did_not_have(repaired):
    edges_from: dict = {}
    for edge in repaired["edges"]:
        edges_from.setdefault(edge["from"], []).append(edge["to"])
    nodes = {n["id"]: n for n in repaired["nodes"]}

    def reachable(hub):
        found, stack, seen = set(), list(edges_from.get(hub, [])), set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if nodes[current]["kind"] == "condition":
                stack.extend(edges_from.get(current, []))
            else:
                found.add(current)
        return found

    for hub, options in hubs_with_options(repaired).items():
        ids = [option["id"] for option in options]
        assert len(ids) == len(set(ids))
        assert {option["to"] for option in options} <= reachable(hub)
        assert all(option["meaning"].strip() for option in options)


def test_a_missing_destination_or_an_unknown_hub_fails_loudly():
    broken = fixture()
    broken["edges"] = [e for e in broken["edges"] if not (e["from"] == "n_redispatch" and e["to"] == RT)]
    broken["edges"] = [e for e in broken["edges"] if not (e["from"] == "c_n_redispatch" and e["to"] == RT)]
    with pytest.raises(ValueError, match="new route"):
        apply_answer_options_repair(broken)
    unknown = fixture()
    unknown["nodes"].append({"id": "n_brand_new_question", "kind": "intent", "config": {"prompt": "?"}})
    with pytest.raises(ValueError, match="No answer meanings"):
        apply_answer_options_repair(unknown)


@pytest.mark.parametrize("hub", ["n_redispatch", "n_haier_audit_redispatch_confirm"])
@pytest.mark.parametrize("text", [
    # the user's rule (2026-11-25): a bare ji / haan / theek hai is permission
    "जी।", "हाँ।", "ठीक है।",
    # the replies the DEMO calls gave, and the rest of the category
    "जी, जी, जी।", "जी मैडम।", "हाँ जी मैम", "Yes ma'am.", "Yes, yes.", "ओके मैडम।", "ok sir", "बिल्कुल", "चलेगा।",
    "हाँ जी, थैंक यू।", "ठीक है, थैंक यू",
])
def test_a_plain_yes_is_permission_at_both_permission_questions(repaired, hub, text):
    result = replay(repaired, hub, text, "affirm")
    assert result["slots"]["outcome"] == "redispatch"
    before = replay(fixture(), hub, text, "affirm")
    assert before["slots"].get("outcome") != "redispatch"   # what the DEMO calls got


@pytest.mark.parametrize("hub", ["n_redispatch", "n_haier_audit_redispatch_confirm"])
@pytest.mark.parametrize("text", ["हम्म।", "hmm", "हेलो।", "hello", "थैंक यू।", "thank you", "धन्यवाद", "हाँ बोलिए।", "जी, थैंक यू"])
def test_hmm_hello_and_thanks_are_never_permission(repaired, hub, text):
    result = replay(repaired, hub, text, "affirm")
    assert result["slots"].get("outcome") is None
    assert result["awaiting"] == hub
    assert result["reply"].endswith("forward कर दूँ?")


@pytest.mark.parametrize("hub", ["n_redispatch", "n_haier_audit_redispatch_confirm"])
@pytest.mark.parametrize("text", [
    "हाँ जी मैम, कर दो।", "बिल्कुल करो।", "यस कर दो।", "27वीं के लिए कीजिएगा।", "Yes, but only on Friday.",
    "बिल्कुल कर दीजिए, पर पिछली बार वाला टेक्नीशियन नहीं चाहिए, कोई नया।",
])
def test_the_decisions_yes_is_permission_whatever_else_is_said(repaired, hub, text):
    result = replay(repaired, hub, text, "affirm", option="yes", judged=True)
    assert result["slots"]["outcome"] == "redispatch"


def test_a_complaint_containing_nahi_no_longer_cancels(repaired):
    text = "ये सही करके नहीं दे रहे आपके जो ये टेक्नीशियन है।"
    assert replay(fixture(), "n_service_need", text)["trace"][-1] != "n_redispatch"
    judged = replay(repaired, "n_service_need", text, option="yes", judged=True)
    assert "n_redispatch" in judged["trace"] and judged["slots"].get("outcome") is None
    unsure = replay(repaired, "n_service_need", "नहीं, मना नहीं किए हैं।", judged=True)
    assert unsure["slots"].get("outcome") is None and unsure["awaiting"] == "n_service_need"
    assert replay(repaired, "n_service_need", "नहीं।", "refusal")["slots"]["outcome"] == "cancel_no_service"


@pytest.mark.parametrize("hub", ["n_resolved_how_q", "n_haier_followup_n_resolved_how_q_clarify"])
def test_who_fixed_it_follows_the_meaning(repaired, hub):
    said = "नहीं हायर का टेक्नीशियन सही किया था।"
    assert replay(repaired, hub, said, option="haier_technician", judged=True)["slots"]["outcome"] == "redispatch_completion"
    assert replay(repaired, hub, "लोकल मिस्त्री से करवाया", option="other_or_self", judged=True)["slots"]["outcome"] == "cancel_resolved"
    assert "n_service_need" in replay(repaired, hub, "ठीक ही कहाँ हुआ", option="not_resolved", judged=True)["trace"]
    # a bare yes does not say WHO: neither closing is taken
    assert replay(repaired, hub, "हाँ।", "affirm")["slots"].get("outcome") in (None, "unverified_no_action")
    hello = replay(repaired, hub, "हलो।")
    assert hello["awaiting"] == hub and hello["slots"].get("outcome") is None


def test_stop_calling_is_offered_wherever_the_hub_could_already_end_that_way(repaired):
    hubs = hubs_with_options(repaired)
    for hub in ("n_redispatch", "n_haier_audit_redispatch_confirm", "n_service_need", "n_service_need_clarify"):
        assert "stop_calling" in [option["id"] for option in hubs[hub]]
        result = replay(repaired, hub, "आप कॉल ही मत करो", option="stop_calling", judged=True)
        assert result["slots"]["outcome"] == "repeat_calls_noted"
    assert "stop_calling" not in [option["id"] for option in hubs["n_resolved_how_q"]]
    stop = next(option for option in hubs["n_service_need"] if option["id"] == "stop_calling")
    assert stop["notOnQuestion"] is True
    asked = replay(repaired, "n_service_need", "क्या बार-बार आप लोग कॉल कर रहे हो?", "complaint",
                   option="stop_calling", judged=True)
    assert asked["awaiting"] == "n_service_need" and asked["slots"].get("outcome") is None


def test_the_authored_tables_are_consistent():
    topic_ids = [option_id for option_id, _target, _meaning in _TOPIC_OPTIONS]
    assert len(topic_ids) == len(set(topic_ids))
    assert len({target for _id, target, _meaning in _TOPIC_OPTIONS}) == len(_TOPIC_OPTIONS)
    for hub, twin in _CHOICE_TWINS.items():
        assert twin in _CHOICE_OPTIONS and hub in _YES_NO_TARGETS
    for hub in _YES_NO_MEANINGS:
        assert hub in _YES_NO_TARGETS, hub
    plain = {"yes", "no"}
    for options in _CHOICE_OPTIONS.values():
        assert not plain & {option_id for option_id, _target, _meaning in options}


def test_theek_hai_is_a_yes_except_where_it_may_describe_a_state():
    from haier.setup.haier_answer_options_repair import _STATE_HUBS
    assert "n_reason_unit_ok_q" in _STATE_HUBS and "n_redispatch" not in _STATE_HUBS
    definition = fixture()
    question = "क्या आपके product में अभी भी कोई problem है?"
    definition["nodes"].append({"id": "n_reason_unit_ok_q", "kind": "intent", "config": {
        "prompt": question, "textByLanguage": {"hi": question}, "elseIsAnswer": False}})
    definition["nodes"].append({"id": "n_unit_ok_pending_apology", "kind": "message", "config": {"text": "sorry"}})
    definition["edges"] += [
        {"id": "u1", "from": "n_reason_unit_ok_q", "to": "n_unit_ok_pending_apology", "label": "yes/haan/हाँ/ठीक"},
        {"id": "u2", "from": "n_reason_unit_ok_q", "to": "n_resolved_how_q", "label": "no/nahi/नहीं/अब ठीक है"},
        {"id": "u3", "from": "n_reason_unit_ok_q", "to": "m_n_v2_end_unverified", "label": "else"},
        {"id": "u4", "from": "n_unit_ok_pending_apology", "to": "n_redispatch"},
    ]
    repaired = apply_answer_options_repair(definition)
    nodes = {n["id"]: n for n in repaired["nodes"]}
    assert nodes["n_redispatch"]["config"]["answerAgreementWords"] is True
    assert nodes["n_service_need"]["config"]["answerAgreementWords"] is True
    assert "answerAgreementWords" not in nodes["n_reason_unit_ok_q"]["config"]
    # the decision reads it there: with no option chosen the question stays open
    status = replay(repaired, "n_reason_unit_ok_q", "ठीक है।", None, judged=True)
    assert status["awaiting"] == "n_reason_unit_ok_q"
    assert "n_unit_ok_pending_apology" not in status["trace"]
    assert "n_unit_ok_pending_apology" in replay(repaired, "n_reason_unit_ok_q", "हाँ जी।", "affirm")["trace"]
    # an authored phrase of the "no problem" edge still decides when nothing was judged
    assert "n_resolved_how_q" in replay(repaired, "n_reason_unit_ok_q", "नहीं, अब ठीक है।")["trace"]
    assert replay(repaired, "n_service_need", "सही है।", "affirm")["answered_option"] == "yes"
    start = next(n for n in repaired["nodes"] if n["kind"] == "start")
    assert start["config"]["closingFollowups"] == 2
