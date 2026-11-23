"""Later call-audit business regressions through the actual graph interpreter."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

from langgraph.checkpoint.memory import MemorySaver
import pytest

from haier.setup.haier_call_audit_repair import apply_call_audit_repair
from haier.setup.haier_followup_audit_repair import PREFIX, apply_followup_audit_repair
from shared.orchestration.workflow_engine import build_definition_graph
from tests.unit.test_haier_call_audit_repair import fixture as earlier_fixture


def fixture():
    """Compact authored topology preserving the reviewed questions and answers."""
    definition = apply_call_audit_repair(earlier_fixture("n_service_need"))
    nodes, edges = definition["nodes"], definition["edges"]

    def node(node_id, kind="intent", **config):
        old = next((n for n in nodes if n["id"] == node_id), None)
        value = {"id": node_id, "kind": kind, "config": {"prompt": f"Question: {node_id}", **config}}
        if old:
            old.update(value)
        else:
            nodes.append(value)

    def edge(edge_id, source, target, label="next"):
        edges.append({"id": edge_id, "from": source, "to": target, "label": label})

    for base in ("n_apology_call", "n_apology_visit", "n_apologize_no_visit"):
        for suffix in ("", "_clarify"):
            hub = base + suffix
            node(hub, elseIsAnswer=True)
            edge(hub + "_yes", hub, "n_redispatch", "yes/haan/हाँ/chahiye/चाहिए")
            edge(hub + "_no", hub, "m_n_end_no_service", "no/nahi/नहीं")
            edge(hub + "_else", hub, base + "_clarify" if not suffix else "m_n_v2_end_unverified", "else")
            edge("e_v2_special_" + hub + "_stop", hub, "m_n_v2_end_repeat_calls", "stop calling")
            edge("e_v2_special_" + hub + "_future", hub, "n_future_confirm", "next week/अगले हफ्ते")
            edge("e_v2_special_" + hub + "_paid", hub, "n_v2_paid_status_check", "already paid")
    node("n_future_confirm")
    node("n_unit_ok_pending_apology", "message", text="Sorry for the inconvenience.")
    edge("pending_apology_next", "n_unit_ok_pending_apology", "n_redispatch")
    for base in ("n_v2_paid_status_check", "n_reason_unit_ok_q", "n_reason_refused_pay_q"):
        for suffix in ("", "_clarify"):
            hub = base + suffix
            node(hub, elseIsAnswer=True)
            positive = {"n_v2_paid_status_check": "n_v2_guard_completed", "n_reason_unit_ok_q": "n_resolved_how_q",
                        "n_reason_refused_pay_q": "n_rtp_warn_q"}[base]
            negative = "n_unit_ok_pending_apology" if base == "n_reason_unit_ok_q" else "n_service_need"
            edge(hub + "_yes", hub, positive, "yes/haan/हाँ/ho gaya/हो गया")
            edge(hub + "_no", hub, negative, "no/nahi/नहीं")
            edge(hub + "_else", hub, base + "_clarify" if not suffix else "m_n_v2_end_unverified", "else")
            edge("e_v2_special_" + hub + "_stop", hub, "m_n_v2_end_repeat_calls", "stop calling")
            edge("e_warranty_" + hub, hub, "m_warranty_claimed", "warranty dispute")
    node("n_rtp_warn_q")
    for terminal, outcome in (("m_n_end_resolved_tech", "redispatch_completion"),
                              ("m_n_end_resolved_auto", "cancel_resolved_auto"),
                              ("m_n_v2_end_completed_haier", "redispatch_completion"),
                              ("m_n_v2_end_completed_other", "close_completed_other"),
                              ("m_n_v2_end_completed", "close_completed")):
        node(terminal, "message", silent=True, setSlots={"outcome": outcome})
        edge(terminal + "_next", terminal, "end")
    node("n_resolved_how_q", elseIsAnswer=True)
    edge("e_how_tech", "n_resolved_how_q", "m_n_end_resolved_tech", "Haier wale ne/Haier वाले ने/technician fixed")
    edge("e_how_auto", "n_resolved_how_q", "m_n_end_resolved_auto", "apne aap/अपने आप/I fixed it myself")
    edge("e_how_not_resolved", "n_resolved_how_q", "n_service_need", "not resolved")
    edge("e_how_else", "n_resolved_how_q", "m_n_end_resolved_auto", "else")
    node("n_v2_completed_by_q", elseIsAnswer=True)
    edge("e_completed_by_haier", "n_v2_completed_by_q", "m_n_v2_end_completed_haier", "Haier/yes/हाँ")
    edge("e_completed_by_other", "n_v2_completed_by_q", "m_n_v2_end_completed_other", "khud/खुद/local mechanic")
    edge("e_completed_by_unknown", "n_v2_completed_by_q", "m_n_v2_end_completed", "not sure/pata nahi/पता नहीं")
    edge("e_completed_by_else", "n_v2_completed_by_q", "m_n_v2_end_completed", "else")
    # Real product-containing speech demonstrates authored readback precedence.
    intro = next(n for n in nodes if n["id"] == "n_intro_unit_ok")
    intro["config"].update(prompt="Any technician visit for your [product_name]?",
                            textByLanguage={"en": "Any technician visit for your [product_name]?",
                                            "hi": "क्या आपके [product_name] के लिए technician की visit हुई?"})
    next(e for e in edges if e["id"] == "identity_yes")["to"] = "n_intro_unit_ok"
    next(e for e in edges if e["id"] == "start")["to"] = "n_identity_gate"
    return definition


async def replay(definition, hub, text, signal=None, slots=None, context=None):
    graph = build_definition_graph(definition, MemorySaver())
    return await graph.ainvoke({
        "current_node": hub, "awaiting": hub, "user_text": text,
        "signal_override": signal, "signal_authoritative": True,
        "slots": slots or {}, "context_values": context or {}, "language": "hi-IN",
        "status": "collecting", "node_retries": {}, "audit": [],
    }, {"configurable": {"thread_id": "haier-followup-test"}})


_LONG_REFUSAL = "नहीं, अब तो मुझे करानी ही नहीं है क्योंकि। 2 महीने हो चुके हैं, आने को कोई तैयार ही नहीं होता।"


@pytest.mark.parametrize("hub", [base + suffix for base in
    ("n_service_need", "n_apology_call", "n_apology_visit", "n_apologize_no_visit") for suffix in ("", "_clarify")])
def test_long_refusal_overrides_overlapping_affirmation(hub):
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), hub, _LONG_REFUSAL, "affirm"))
    assert result["slots"]["outcome"] == "cancel_no_service"
    assert result["slots"][f"haier_followup_{hub}_route"] == "no_service"
    assert "n_redispatch" not in result["trace"]


@pytest.mark.parametrize(("hub", "text", "signal", "target"), [
    ("n_v2_paid_status_check", "पैसे पेमेंट उसका हो गया।", "already_paid", "n_v2_paid_status_check_clarify"),
    ("n_v2_paid_status_check", "नहीं मैडम, पैसे ले गया वो 590 रुपये।", "already_paid", "n_v2_paid_status_check_clarify"),
    ("n_v2_paid_status_check_clarify", "I already paid", "already_paid", "n_v2_paid_status_check_clarify"),
    ("n_reason_refused_pay_q", "पेमेंट करने से हम मना क्यों करेंगे? हमारा काम करेंगे हम, पेमेंट देंगे।", "payment_intent", "n_service_need"),
    ("n_reason_refused_pay_q_clarify", "We will pay once the work is done", "affirm", "n_service_need"),
    ("n_apologize_no_visit", "नहीं हुई है।", None, "n_service_need_clarify"),
    ("n_apology_visit_clarify", "visit nahi hui hai", "refusal", "n_service_need_clarify"),
    ("n_intro_unit_ok", "मैम, 10 दिन पहले हुई थी, उसके बाद नहीं हुई है। हमने दोबारा एप्लीकेशन डाली थी।", None, "n_reason_unit_ok_q"),
    ("n_intro_unit_ok_clarify", "10 din pehle hui thi uske baad nahi hui hai", "refusal", "n_reason_unit_ok_q"),
    ("n_reason_unit_ok_q", "अभी नहीं हुई है ठीक।", None, "n_redispatch"),
    ("n_reason_unit_ok_q_clarify", "It is still not fixed", "affirm", "n_redispatch"),
    ("n_reason_unit_ok_q", "हाँ", "affirm", "n_redispatch"),
    ("n_reason_unit_ok_q", "नहीं", "refusal", "n_resolved_how_q"),
    ("n_reason_unit_ok_q", "There was no problem. It was installation required.", None, "n_v2_no_response_status"),
    ("n_reason_unit_ok_q_clarify", "प्रॉब्लम नहीं थी, सिर्फ installation चाहिए थी", "affirm", "n_v2_no_response_status"),
    ("n_v2_paid_status_check", "Payment ho gaya but issue not resolved", "already_paid", "n_service_need"),
    ("n_resolved_how_q", "नहीं, अब ठीक हो गए।", None, PREFIX + "n_resolved_how_q_clarify"),
    ("n_v2_completed_by_q", "not sure", "affirm", PREFIX + "n_v2_completed_by_q_clarify"),
    (PREFIX + "n_resolved_how_q_clarify", "नहीं, अब ठीक हो गए।", None, "m_n_v2_end_unverified"),
    ("n_resolved_how_q", "Haier technician ne nahi kiya", "affirm", PREFIX + "n_resolved_how_q_clarify"),
    ("n_v2_completed_by_q", "Haier technician ne nahi kiya", "affirm", PREFIX + "n_v2_completed_by_q_clarify"),
    ("n_v2_completed_by_q", "Maine khud nahi kiya.", "affirm", PREFIX + "n_v2_completed_by_q_clarify"),
    ("n_v2_completed_by_q", "पैसे पेमेंट उसका हो गया।", "already_paid", PREFIX + "n_v2_completed_by_q_clarify"),
    ("n_resolved_how_q", "Local technician ne nahi kiya", "affirm", PREFIX + "n_resolved_how_q_clarify"),
    ("n_v2_paid_status_check", "Payment ho gaya, warranty dispute hai", "already_paid", "n_warranty_claim_q"),
])
def test_recorded_business_meanings_reach_safe_next_question(hub, text, signal, target):
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), hub, text, signal))
    assert result["trace"][-1] == target
    assert result["slots"].get("outcome") not in {"cancel_no_service", "cancel_resolved_auto", "redispatch_completion"}


@pytest.mark.parametrize("hub", [base + suffix for base in
    ("n_service_need", "n_apology_call", "n_apology_visit", "n_apologize_no_visit") for suffix in ("", "_clarify")])
def test_weekday_preference_is_kept_without_claiming_booking(hub):
    text = "यस, गीज़र की सर्विस करवा दीजिए वेडनेसडे को।"
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), hub, text))
    assert result["awaiting"] == "n_redispatch"
    assert result["slots"]["preferred_service_day"] == "वेडनेसडे"
    assert result["slots"]["preferred_service_timing"] == text
    assert result["slots"].get("outcome") is None
    assert "वेडनेसडे के लिए आपकी preference नोट कर ली है" in result["reply"]
    assert result["reply"].endswith("क्या technician की visit के लिए मैं आपकी request आगे बढ़ा दूँ?")


@pytest.mark.parametrize(("hub", "text", "signal"), [
    ("n_apology_visit", "stop calling", None),
    ("n_apology_call", "next week", None),
    ("n_apologize_no_visit", "already paid", "already_paid"),
    ("n_v2_paid_status_check", "warranty dispute", None),
    ("n_resolved_how_q", "Haier वाले ने ठीक किया", None),
    ("n_resolved_how_q", "अपने आप ठीक हो गया", None),
    ("n_v2_completed_by_q", "Haier", None),
    ("n_v2_completed_by_q", "local mechanic", None),
    ("n_service_need", "next week", None),
    ("n_redispatch", "Hello", None),
    ("n_redispatch", "हाँ", "affirm"),
])
def test_unrelated_authored_routes_remain_unchanged(hub, text, signal):
    original = fixture()
    before = asyncio.run(replay(original, hub, text, signal))
    after = asyncio.run(replay(apply_followup_audit_repair(original), hub, text, signal))
    assert [n for n in after["trace"] if not n.startswith(PREFIX)] == before["trace"]
    assert after["slots"].get("outcome") == before["slots"].get("outcome")


def test_identity_opt_out_does_not_imply_named_person_verification():
    text = "आप कहाँ से बात कर रहे हैं? नहीं चाहिए हायर कस्टमर वगैरह कुछ भी भैया, कितनी बार बोलूँ नहीं चाहिए, नहीं चाहिए, मेरा प्रोडक्ट ठीक हो गया है।"
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), "n_identity_gate", text, "refusal"))
    assert result["slots"]["identity_answer"] == "service_declined"
    assert result["slots"]["outcome"] == "cancel_no_service"
    assert "n_intro_unit_ok" not in result["trace"]


@pytest.mark.parametrize("text", ["Haan bol raha hoon, mujhe service nahi chahiye", "हाँ, service नहीं चाहिए।",
                                 "नहीं, मैं service नहीं चाहता, service नहीं चाहिए।"])
def test_identity_opt_out_has_priority_independent_of_json_key_order(text):
    candidate = apply_followup_audit_repair(fixture())
    reordered = json.loads(json.dumps(candidate, sort_keys=True))
    for definition in (candidate, reordered):
        result = asyncio.run(replay(definition, "n_identity_gate", text, "affirm"))
        assert result["slots"]["identity_answer"] == "service_declined"
        assert result["slots"]["outcome"] == "cancel_no_service"
        assert "n_intro_unit_ok" not in result["trace"]


@pytest.mark.parametrize("product", ["0", 0, "0.0", 0.0, "", None, "unknown", "null", "n/a", "[product_name]", "washing machine"])
def test_product_context_uses_safe_authored_display_value(product):
    definition = apply_followup_audit_repair(fixture())
    graph = build_definition_graph(definition, MemorySaver())

    async def run():
        config = {"configurable": {"thread_id": "product-placeholder"}}
        first = await graph.ainvoke({"current_node": None, "awaiting": None, "user_text": "",
            "slots": {}, "context_values": {"product_name": product}, "language": "hi-IN"}, config)
        assert first["awaiting"] == "n_identity_gate"
        return await graph.ainvoke({"user_text": "haan", "signal_override": "affirm", "signal_authoritative": True}, config)

    result = asyncio.run(run())
    expected = product if product == "washing machine" else "Haier product"
    assert result["slots"]["product_name"] == expected
    assert expected in result["reply"]
    assert "[product_name]" not in result["reply"]
    assert " 0 " not in result["reply"]


def test_transform_pure_idempotent_and_stale_routes_do_not_reapply():
    original = fixture()
    backup = deepcopy(original)
    repaired = apply_followup_audit_repair(original)
    assert original == backup
    assert apply_followup_audit_repair(repaired) == repaired
    assert apply_followup_audit_repair(json.loads(json.dumps(repaired, sort_keys=True))) == repaired
    result = asyncio.run(replay(repaired, "n_service_need", "yes", "affirm",
                               {"haier_followup_n_service_need_route": "no_service"}))
    assert result["awaiting"] == "n_redispatch"
    for node in repaired["nodes"]:
        config = node.get("config", {})
        if node["kind"] == "message" and "outcome" in config.get("setSlots", {}):
            assert config["terminalOutcome"] is True
        if node["id"] == PREFIX + "product_fallback":
            assert not config.get("terminalOutcome")


@pytest.mark.parametrize("text", [
    "Wednesday", "Wednesday ko mat aana", "Wednesday ko service nahi chahiye",
    "aap Wednesday ko kyun phone kar rahe hain?", "Wednesday nahi, 3 mahine baad",
    "Wednesday ko service nahi chahiye, Friday ko chahiye", "Wednesday ko technician nahi bhejna",
])
def test_day_mention_alone_never_proves_service_consent(text):
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), "n_apology_call", text, None))
    assert result.get("awaiting") != "n_redispatch"
    assert result["slots"].get("haier_followup_n_apology_call_route") != "weekday"
    assert result["slots"].get("preferred_service_day") is None


@pytest.mark.parametrize("text", ["Aap kahan se bol rahe hain?", "आप कहाँ से बोल रहे हैं?", "जी, मैं उनकी वाइफ बोल रही हूँ, बताइए।"])
def test_identity_questions_and_family_contacts_do_not_verify_named_customer(text):
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), "n_identity_gate", text, "wrong_person"))
    assert result["slots"].get("identity_answer") != "yes"
    assert "n_intro_unit_ok" not in result["trace"]
    assert "n_read_reason" not in result["trace"]


def test_negated_family_relationship_does_not_create_wrong_person_fact():
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), "n_identity_gate",
        "मैं उनकी वाइफ नहीं हूँ, मैं राकेश बोल रहा हूँ।", "affirm"))
    assert result["slots"].get("identity_answer") != "no"


@pytest.mark.parametrize("text", ["Wrong number. Service nahi chahiye.", "Main customer nahi hoon, service nahi chahiye",
                                 "मैं उनकी वाइफ बोल रही हूँ, service नहीं चाहिए।"])
def test_explicit_wrong_party_cannot_cancel_named_customers_request(text):
    repaired = apply_followup_audit_repair(fixture())
    for definition in (repaired, json.loads(json.dumps(repaired, sort_keys=True))):
        result = asyncio.run(replay(definition, "n_identity_gate", text, "wrong_person"))
        assert result["slots"].get("identity_answer") == "no"
        assert result["slots"].get("outcome") != "cancel_no_service"
        assert "n_intro_unit_ok" not in result["trace"]


def test_paid_fact_remains_separate_from_pending_work_fact():
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), "n_v2_no_response_status",
        "installation pending, already paid", None))
    assert result["slots"]["payment_status"] == "paid"
    assert result["slots"]["installation_status"] == "pending"


@pytest.mark.parametrize("hub", ["n_redispatch", "n_haier_audit_redispatch_confirm"])
@pytest.mark.parametrize("text", ["हाँ, technician भेज दीजिए।", "हाँ, request आगे बढ़ा दीजिए।",
                                 "haan technician bhej dijiye", "haan request aage badha do", "जी भेज दीजिए।"])
def test_natural_commands_give_explicit_permission(hub, text):
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), hub, text, "affirm"))
    assert result["slots"]["outcome"] == "redispatch"


@pytest.mark.parametrize("text", ["technician मत भेजिए", "technician nahi bhej dijiye", "क्या technician भेज दीजिए?"])
def test_negated_or_questioned_commands_do_not_give_permission(text):
    result = asyncio.run(replay(apply_followup_audit_repair(fixture()), "n_redispatch", text, "affirm"))
    assert result["slots"].get("outcome") != "redispatch"


def test_repeated_payment_then_unfixed_status_recovers_to_actual_visit_consent():
    definition = apply_followup_audit_repair(fixture())

    async def run():
        graph = build_definition_graph(definition, MemorySaver())
        config = {"configurable": {"thread_id": "paid-but-unfixed-recorded-sequence"}}
        results = []
        seed = {"current_node": "n_v2_paid_status_check", "awaiting": "n_v2_paid_status_check",
                "slots": {}, "context_values": {}, "language": "hi-IN", "audit": [], "node_retries": {}}
        for index, (text, signal) in enumerate([
            ("नहीं मैडम, पैसे ले गया वो 590 रुपये।", "already_paid"),
            ("पैसे पेमेंट उसका हो गया।", "already_paid"),
            ("नहीं, TV की problem अभी ठीक नहीं हुई है।", None),
            ("हाँ, service चाहिए।", "affirm"),
            ("हाँ, technician भेज दीजिए।", "affirm"),
        ]):
            state = seed if index == 0 else {}
            state.update(user_text=text, signal_override=signal, signal_authoritative=True)
            results.append(await graph.ainvoke(state, config))
        return results

    results = asyncio.run(run())
    assert [r.get("awaiting") for r in results[:-1]] == ["n_v2_paid_status_check_clarify", "n_v2_paid_status_check_clarify",
                                                          "n_service_need", "n_redispatch"]
    assert all(not r["slots"].get("outcome") for r in results[:-1])
    assert results[-1]["slots"]["outcome"] == "redispatch"
    assert results[-1]["slots"]["payment_status"] == "paid"
