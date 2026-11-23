"""Business regressions from the 23 November audit, using the real graph."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from haier.setup.haier_call_audit_repair import PREFIX, apply_call_audit_repair
from tests.golden.harness import patched
from tests.unit.test_haier_service_need_repair import fixture as service_fixture


def fixture(hub="n_intro_refused_visit"):
    definition = service_fixture()
    nodes, edges = definition["nodes"], definition["edges"]
    nodes[0]["id"] = "start"
    next(e for e in edges if e["id"] == "start")["to"] = hub

    def node(node_id, kind="intent", **config):
        old = next((n for n in nodes if n["id"] == node_id), None)
        value = {"id": node_id, "kind": kind,
                 "config": {"prompt": f"Question: {node_id}", **config}}
        if old:
            old.update(value)
        else:
            nodes.append(value)

    def edge(edge_id, source, target, label):
        edges.append({"id": edge_id, "from": source, "to": target, "label": label})

    for target in ["m_n_v2_end_unverified", "n_apology_call", "n_apology_visit",
                   "n_apologize_no_visit", "n_v2_completed_by_q", "n_v2_pending_case_check",
                   "n_warranty_claim_q", "n_wp_available_q", "n_read_reason", "n_v2_future_when",
                   "n_reason_refused_q", "n_reason_refused_q_clarify", "n_v2_paid_status_check",
                   "m_n_v2_end_repeat_calls"]:
        node(target)
    node("m_n_end_redispatch_thanks", "message", silent=True, setSlots={"outcome": "redispatch"})
    edge("redispatch_close", "m_n_end_redispatch_thanks", "end", "next")
    node("m_warranty_claimed", "message", silent=True, setSlots={"warranty_claimed": "yes"})
    edge("warranty_next", "m_warranty_claimed", "n_warranty_claim_q", "next")
    node("n_v2_guard_completed", "condition", variable="installation_status", operator="equals", value="pending")
    edge("guard_yes", "n_v2_guard_completed", "n_service_need", "true")
    edge("guard_no", "n_v2_guard_completed", "n_v2_completed_by_q", "false")
    for stem, mode in [("bill", "visit"), ("refused_pay", "visit"), ("refused_visit", "call"),
                       ("not_delivered", "call"), ("ecom_damaged", "call"),
                       ("unit_ok", "both"), ("not_haier", "both")]:
        target = {"refused_visit": "n_reason_refused_q", "not_delivered": "n_reason_delivered_q",
                  "ecom_damaged": "n_reason_ecom_q"}.get(stem, f"n_reason_{stem}_q")
        node(target)
        for suffix in ("", "_clarify"):
            source = f"n_intro_{stem}{suffix}"
            extra = {}
            if stem == "unit_ok":
                extra["alsoCapture"] = [{"variable": "contact_report", "overwrite": True,
                                          "entity": {"dataType": "text", "matchCanonicalValues": False,
                                                     "synonymPatterns": {"yes": [r"technician called"]}}}]
                node(source + "_contact_confirmed")
                edge(source + "_correction", source, source + "_contact_confirmed", "correction")
            node(source, elseIsAnswer=True, **extra)
            edge(f"e_{source}_yes", source, target, "हाँ/haan/yes/hmm/हम्म/कॉल आया था/call aaya tha")
            negative = "n_apology_call" if mode == "call" else (
                "n_apology_visit" if mode == "visit" else "n_apologize_no_visit")
            edge(f"e_{source}_no", source, negative, "नहीं/nahi/no")
            clarify = f"n_intro_{stem}_clarify" if not suffix else "m_n_v2_end_unverified"
            edge(source + "_else", source, clarify, "else")
            edge("e_v2_special_" + source + "_future", source, "future", "later/बाद में/baad mein")
            edge("e_warranty_" + source, source, "m_warranty_claimed", "warranty/वारंटी/warranty dispute/under warranty/free service")
            edge("e_v2_special_" + source + "_paid", source, "n_v2_paid_status_check", "already paid")
            edge("e_v2_special_" + source + "_stop", source, "m_n_v2_end_repeat_calls", "stop calling")
    for base, completed in [("n_v2_no_response_status", False), ("n_v2_completed_check", True)]:
        for suffix in ("", "_clarify"):
            source = base + suffix
            captures = [{"variable": "installation_status", "overwrite": True,
                         "entity": {"dataType": "text", "matchCanonicalValues": False,
                                    "synonymPatterns": {"done": [r"installation ho gaya|installation done|लग गया"],
                                                        "pending": [r"installation pending|installation nahi hui"]}}}]
            node(source, alsoCapture=captures, elseIsAnswer=True)
            if completed:
                edge("e_completed_yes" + suffix, source, "n_v2_guard_completed", "हाँ/haan/yes")
                edge("e_completed_no" + suffix, source, "n_service_need", "नहीं/no/nahi")
            else:
                edge("e_noresp_done" + suffix, source, "n_v2_completed_check", "done/ho gaya")
                edge("e_noresp_pending" + suffix, source, "n_service_need", "pending/nahi hua")
                edge("e_noresp_bare" + suffix, source, base + "_clarify", "नहीं/no/nahi/हाँ/yes")
            edge(source + "_else", source, base + "_clarify" if not suffix else "m_n_v2_end_unverified", "else")
            edge("e_warranty_" + source, source, "m_warranty_claimed", "warranty/वारंटी/warranty dispute/under warranty/free service")
            edge("e_v2_special_" + source + "_paid", source, "n_v2_paid_status_check", "already paid")
            edge("e_v2_special_" + source + "_stop", source, "m_n_v2_end_repeat_calls", "stop calling")
    node("n_redispatch", elseIsAnswer=True)
    edge("e_redispatch_ok", "n_redispatch", "m_n_end_redispatch_thanks", "yes/हाँ/haan/ok/okay/hmm/thanks")
    edge("e_redispatch_no", "n_redispatch", "n_service_need_clarify", "no/नहीं/nahi")
    edge("e_redispatch_else", "n_redispatch", "m_n_end_redispatch_thanks", "else")
    edge("e_v2_special_n_redispatch_future", "n_redispatch", "future", "later/बाद में/baad mein")
    node("n_identity_gate", "ask", variable="identity_answer", entity={
        "dataType": "text", "matchCanonicalValues": False,
        "synonymPatterns": {"no": [r"^wrong person$|^no$"], "yes": [r"^yes$|^haan$|^जी$"]}})
    node("n_is_identity_denied", "condition", variable="identity_answer", operator="equals", value="no")
    edge("e_gate_next", "n_identity_gate", "n_is_identity_denied", "fallback")
    edge("identity_no", "n_is_identity_denied", "n_wp_available_q", "true")
    edge("identity_yes", "n_is_identity_denied", "n_read_reason", "false")
    next(n for n in nodes if n["id"] == "start")["config"] = {"behavior": {"max_ask_retries": 1}}
    return definition


async def replay(definition, text, signal=None, slots=None):
    with patched(definition, []) as (engine, executor):
        common = {"session_id": "haier-audit-test", "tenant_id": "test", "bot_id": "test",
                  "workflow_name": definition["name"], "language": "hi-IN"}
        await engine.handle_turn_detailed(**common, user_text="", reset_state=True, initial_slots=slots)
        result = await engine.handle_turn_detailed(**common, user_text=text, signal=signal)
        assert not executor.calls
        return result


@pytest.mark.parametrize(("hub", "text", "signal", "target"), [
    ("n_intro_refused_visit", "टेक्नीशियन आ गया, डेमो दिखा कर चला गया, पानी से wash नहीं किया।", "refusal", "n_v2_no_response_status"),
    ("n_intro_refused_visit", "technician aa gaya, demo dikha gaya, wash nahi kiya", "refusal", "n_v2_no_response_status"),
    ("n_intro_refused_visit", "technician ki taraf se nahi, seller ka call aaya tha", "affirm", "n_apology_call"),
    ("n_intro_refused_visit", "टेक्नीशियन की तरफ से नहीं, जहां से खरीदा उसका कॉल आया था", None, "n_apology_call"),
    ("n_intro_refused_visit", "technician called, service nahi hui", "refusal", "n_reason_refused_q"),
    ("n_intro_not_delivered", "हम्म। हम्म हम्म हम्म।", "affirm", "n_intro_not_delivered_clarify"),
    ("n_intro_not_delivered", "हाँ जी बोलिए।", "affirm", "n_intro_not_delivered_clarify"),
    ("n_intro_refused_visit", "Yeah?", "affirm", "n_intro_refused_visit_clarify"),
    ("n_v2_no_response_status", "अभी पेंडिंग है, सर्विस नहीं हुई है।", None, "n_service_need"),
    ("n_v2_no_response_status_clarify", "Service abhi tak nahi hui hai.", "refusal", "n_service_need"),
    ("n_v2_completed_check", "बिल भी दे दिया है, वारंटी चालू कर दिया, दोनों लग गया।", None, "n_v2_completed_by_q"),
    ("n_v2_completed_check", "नहीं नहीं।", "refusal", "n_v2_completed_by_q"),
    ("n_v2_completed_check", "हाँ", "affirm", "n_v2_pending_case_check"),
    ("n_v2_completed_check", "warranty", "affirm", "n_v2_completed_check_clarify"),
    ("n_intro_bill", "technician called", "affirm", "n_intro_bill_clarify"),
    ("n_intro_refused_visit", "technician aaya nahi", "affirm", "n_intro_refused_visit_clarify"),
    ("n_intro_refused_visit", "technician ne call kiya nahi", "affirm", "n_apology_call"),
    ("n_service_need", "अभी तो प्रोडक्ट मिल गया है। No.", None, "end"),
    ("n_service_need", "Abhi product mil gaya hai. No.", "affirm", "end"),
    ("n_service_need", "नहीं, अभी रिक्वायरमेंट नहीं है।", "refusal", "end"),
    ("n_intro_refused_visit", "Installation humne khud kar liya; koi zaroorat nahi; cancel karaiye", "affirm", "end"),
    ("n_reason_refused_q", "रिनोवेशन चल रहा है। जिस वजह से हमने मना किया फिलहाल के लिए। रिन्यूएशन के होने के बाद हम इंस्टॉल करने के लिए बोल सकते हैं।", None, "n_v2_future_when"),
    ("n_redispatch", "Hello Hello", None, PREFIX + "redispatch_confirm"),
    ("n_redispatch", "hmm", "affirm", PREFIX + "redispatch_confirm"),
    ("n_redispatch", "thank you", "affirm", PREFIX + "redispatch_confirm"),
    ("n_redispatch", "Yes?", "affirm", PREFIX + "redispatch_confirm"),
    ("n_redispatch", "maybe tomorrow?", "affirm", PREFIX + "redispatch_confirm"),
    ("n_redispatch", "Okay thank you so much", "affirm", "end"),
    ("n_redispatch", "हाँ जी", "affirm", "end"),
    ("n_redispatch", "Please send a technician", None, "end"),
    ("n_redispatch", "नहीं", "refusal", "n_service_need_clarify"),
    ("n_redispatch", "No thanks", "refusal", "n_service_need_clarify"),
    ("n_redispatch", "yes but baad mein", "affirm", "future"),
    ("n_redispatch", "service nahi chahiye", "affirm", "end"),
    ("n_identity_gate", "Hello haan ji", "affirm", "n_read_reason"),
    ("n_identity_gate", "Hello ji maam", "affirm", "n_read_reason"),
])
def test_audit_answers_reach_the_correct_question(hub, text, signal, target):
    source = fixture(hub)
    # Model values are deliberately adversarial in several cases.
    result = asyncio.run(replay(apply_call_audit_repair(source), text, signal, {"installation_status": "done"}))
    assert result["trace"][-1] == target
    if hub == "n_redispatch" and target != "end":
        assert result["slots"].get("outcome") != "redispatch"
    if hub == "n_service_need" and target == "end":
        assert result["slots"]["outcome"] == "cancel_no_service"
    if hub == "n_v2_completed_check" and text == "हाँ":
        assert result["slots"]["installation_status"] == "pending"
    if hub == "n_v2_completed_check" and text == "नहीं नहीं।":
        assert result["slots"]["installation_status"] == "done"


@pytest.mark.parametrize(("hub", "text", "signal"), [
    ("n_intro_refused_visit", "haan", "affirm"),
    ("n_intro_bill", "yes", "affirm"),
    ("n_intro_unit_ok", "technician called", None),
    ("n_intro_not_haier", "later", None),
    ("n_service_need", "service chahiye", "affirm"),
    ("n_service_need", "next week", None),
    ("n_service_need", "part pending", None),
    ("n_redispatch", "baad mein", None),
    ("n_v2_completed_check", "warranty dispute", None),
    ("n_v2_no_response_status", "under warranty", None),
    ("n_v2_no_response_status", "installation pending, already paid", None),
    ("n_v2_no_response_status", "installation pending, stop calling", None),
    ("n_v2_completed_check", "installation done, warranty dispute", None),
    ("n_v2_completed_check", "installation done, stop calling", None),
    ("n_intro_refused_visit", "service nahi chahiye kya?", "question"),
    ("n_intro_refused_visit", "Are you saying I do not need service?", "question"),
    ("n_reason_refused_q", "service nahi chahiye kya?", "question"),
])
def test_unrelated_authored_routes_are_preserved(hub, text, signal):
    source = fixture(hub)
    before = asyncio.run(replay(source, text, signal))
    after = asyncio.run(replay(apply_call_audit_repair(source), text, signal))
    before_trace = [n for n in before["trace"] if not n.startswith(PREFIX)]
    after_trace = [n for n in after["trace"] if not n.startswith(PREFIX)]
    assert after_trace == before_trace
    assert after["slots"].get("outcome") == before["slots"].get("outcome")


def test_transform_is_pure_idempotent_and_json_key_order_independent():
    source = fixture()
    backup = deepcopy(source)
    repaired = apply_call_audit_repair(source)
    assert source == backup
    assert apply_call_audit_repair(repaired) == repaired
    reordered = json.loads(json.dumps(repaired, sort_keys=True))
    assert apply_call_audit_repair(reordered) == repaired
    for text, expected in [("No", "end"), ("Hello", PREFIX + "redispatch_confirm"), ("Yes", "end")]:
        next(e for e in reordered["edges"] if e["id"] == "start")["to"] = "n_redispatch"
        result = asyncio.run(replay(reordered, text))
        target = "n_service_need_clarify" if text == "No" else expected
        assert result["trace"][-1] == target


def test_stale_decline_route_does_not_change_new_consent():
    result = asyncio.run(replay(apply_call_audit_repair(fixture("n_redispatch")), "Yes", "affirm",
                                {"haier_audit_n_redispatch_route": "declined"}))
    assert result["slots"]["outcome"] == "redispatch"


def test_old_contact_route_cannot_override_this_turns_existing_capture():
    result = asyncio.run(replay(apply_call_audit_repair(fixture("n_intro_unit_ok")),
                                "technician called", None,
                                {"haier_audit_n_intro_unit_ok_route": "attention"}))
    assert result["trace"][-1] == "n_intro_unit_ok_contact_confirmed"


@pytest.mark.parametrize("hub", ["n_v2_no_response_status", "n_v2_completed_check"])
def test_protected_status_answer_cannot_trigger_a_stale_route(hub):
    source = apply_call_audit_repair(fixture(hub))
    result = asyncio.run(replay(source, "installation pending, already paid", None,
                                {f"haier_audit_{hub}_route": "done"}))
    assert result["trace"][-1] == "n_v2_paid_status_check"


def test_repeated_hello_never_bypasses_identity():
    async def run():
        with patched(apply_call_audit_repair(fixture("n_identity_gate")), []) as (engine, _):
            common = {"session_id": "identity-hello", "tenant_id": "test", "bot_id": "test",
                      "workflow_name": "Haier service regression", "language": "hi-IN"}
            await engine.handle_turn_detailed(**common, user_text="", reset_state=True)
            for _ in range(3):
                result = await engine.handle_turn_detailed(**common, user_text="Hello")
                assert "n_read_reason" not in result["trace"]
            return result
    result = asyncio.run(run())
    assert "identity_answer" not in result["slots"]
    assert result["trace"][-1] == "m_n_v2_end_unverified"


@pytest.mark.parametrize("change", ["bot", "identity", "contact", "correction", "status"])
def test_changed_contract_fails_without_modifying_input(change):
    source = fixture()
    if change == "bot":
        source["id"] = "other"
    elif change == "identity":
        next(e for e in source["edges"] if e["id"] == "e_gate_next")["to"] = "n_service_need"
    elif change == "contact":
        source["edges"] = [e for e in source["edges"] if e["id"] != "e_n_intro_bill_yes"]
    elif change == "status":
        next(n for n in source["nodes"] if n["id"] == "n_v2_completed_check")["config"]["alsoCapture"] = []
    else:
        source["edges"].append({"id": "unknown", "from": "n_intro_bill", "to": "n_service_need", "label": "correction"})
    original = deepcopy(source)
    with pytest.raises(ValueError):
        apply_call_audit_repair(source)
    assert source == original
