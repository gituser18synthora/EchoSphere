"""Bill corrections and call/visit distinctions through the real interpreter."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from haier.setup.haier_bill_state_repair import (
    BILL_HUBS, CONTACT_HUBS, MARKER, PREFIX, VERSION, apply_bill_state_repair,
)
from haier.setup.haier_followup_consent_repair import apply_followup_consent_repair
from tests.unit.test_haier_followup_audit_repair import replay
from tests.unit.test_haier_followup_consent_repair import fixture as consent_fixture


def fixture():
    definition = apply_followup_consent_repair(consent_fixture())
    nodes, edges = definition["nodes"], definition["edges"]
    for hub in BILL_HUBS:
        node = next((n for n in nodes if n["id"] == hub), None)
        value = {"id": hub, "kind": "intent", "config": {
            "prompt": "Bill question: " + hub, "elseIsAnswer": True,
            "textByLanguage": {"hi": "Bill question: " + hub, "en": "Bill question: " + hub},
            "responseMode": "fixed",
        }}
        if node:
            node.update(value)
        else:
            nodes.append(value)
        edges[:] = [edge for edge in edges if edge["from"] != hub]
        positive = "n_service_need" if "reason" in hub else "n_redispatch"
        negative = "n_bill_dealer_q" if "reason" in hub else "n_bill_charge_q" if "dealer" in hub else "m_n_v2_end_payment_declined"
        clarify = hub + "_clarify" if hub in ("n_reason_bill_q", "n_bill_dealer_q", "n_bill_charge_q") else "m_n_v2_end_unverified"
        for label, target in [("yes/haan/हाँ/हां", positive), ("no/nahi/नहीं", negative), ("else", clarify)]:
            edges.append({"id": f"bill_{hub}_{len(edges)}", "from": hub, "to": target, "label": label})
        for label, target, stem in [
            ("stop calling", "m_n_v2_end_repeat_calls", "e_v2_special_"),
            ("already paid", "n_v2_paid_status_check", "e_v2_special_"),
            ("next week/अगले हफ्ते", "n_future_confirm", "e_v2_special_"),
            ("later/baad mein/abhi bahar hoon/abhi nahi/बाद में/अभी बाहर हूँ", "n_v2_future_when", "e_v2_special_"),
            ("warranty mein hai/warranty में है/warranty में/under warranty/free service/extra charge kyun/पैसे क्यों/charge kyun lag raha", "m_warranty_claimed", "e_warranty_"),
        ]:
            edges.append({"id": stem + hub + "_" + str(len(edges)), "from": hub, "to": target, "label": label})
    if not any(node["id"] == "m_n_v2_end_payment_declined" for node in nodes):
        nodes.append({"id": "m_n_v2_end_payment_declined", "kind": "message", "config": {
            "silent": True, "setSlots": {"outcome": "cancel_payment_declined"}}})
        edges.append({"id": "payment_declined_end", "from": "m_n_v2_end_payment_declined", "to": "end"})
    return definition


def run(hub, text, signal=None, slots=None, *, definition=None, context=None):
    return asyncio.run(replay(definition or apply_bill_state_repair(fixture()), hub, text, signal, slots, context))


@pytest.mark.parametrize("hub", BILL_HUBS)
@pytest.mark.parametrize("signal", [None, "affirm", "refusal"])
@pytest.mark.parametrize("text", [
    "हाँ, उस समय तो नहीं था, अभी है मेरे पास।",
    "नहीं, मिल गया। अभी है मेरे पास।",
    "अरे पर मेरे पास बिल है।", "mere paas bill hai", "I have the bill now",
])
def test_current_bill_correction_wins_over_a_generic_polarity(hub, signal, text):
    result = run(hub, text, signal)
    assert result["awaiting"] == PREFIX + "service_need"
    assert result["slots"]["bill_available_now"] == "yes"
    assert "outcome" not in result["slots"]
    assert "warranty_claimed" not in result["slots"]
    assert "n_bill_dealer_q" not in result["trace"][1:]
    assert "n_bill_charge_q" not in result["trace"][1:]


@pytest.mark.parametrize("hub", ["n_reason_bill_q", "n_reason_bill_q_clarify"])
@pytest.mark.parametrize("text", ["हाँ, उस समय तो नहीं था, अभी है मेरे पास।", "us samay nahi tha abhi hai mere paas"])
def test_historical_missing_and_current_available_are_both_saved(hub, text):
    result = run(hub, text)
    assert result["slots"]["bill_available_at_visit"] == "no"
    assert result["slots"]["bill_available_now"] == "yes"


@pytest.mark.parametrize("text", ["उस समय bill था अब नहीं है", "तब बिल था, अब मेरे पास नहीं है", "bill was available at the visit, now nahi hai"])
def test_new_missing_bill_does_not_erase_the_historical_available_fact(text):
    result = run("n_reason_bill_q", text, slots={"bill_available_now": "yes"})
    assert result["slots"]["bill_available_at_visit"] == "yes"
    assert result["slots"]["bill_available_now"] == "no"
    assert result["awaiting"] == "n_bill_charge_q"


def test_historical_no_checks_current_status_once_and_uses_pdf_charge_branch_if_missing():
    past = run("n_reason_bill_q", "नहीं।", "refusal")
    assert past["awaiting"] == PREFIX + "now_q"
    assert past["slots"]["bill_available_at_visit"] == "no"
    missing = run(past["awaiting"], "नहीं।", "refusal", past["slots"])
    assert missing["awaiting"] == "n_bill_charge_q"
    assert missing["slots"]["bill_available_now"] == "no"
    assert "n_bill_dealer_q" not in missing["trace"]


@pytest.mark.parametrize("text", ["जब बिल है तो सर्विस चार्जेज क्यों दूँगा?", "bill hai to charges kyun doon?", "mere paas bill hai, why should I pay?"])
@pytest.mark.parametrize("signal", [None, "affirm", "refusal"])
def test_bill_based_charges_question_is_answered_without_payment_or_warranty_inference(text, signal):
    result = run("n_bill_charge_q_clarify", text, signal)
    assert result["awaiting"] == PREFIX + "service_need"
    assert result["slots"]["bill_available_now"] == "yes"
    assert result["slots"].get("warranty_claimed") != "yes"
    assert result["slots"].get("outcome") is None
    assert "charges या warranty confirm नहीं कर सकती" in result["reply"]


@pytest.mark.parametrize("text", ["charges kyun lag rahe hain", "पैसे क्यों दूँ?", "extra charge kyun", "how much amount should I pay?"])
def test_charge_question_without_known_bill_asks_availability_once(text):
    result = run("n_charges_info_q", text)
    assert result["awaiting"] == PREFIX + "now_q"
    assert "bill_available_now" not in result["slots"]
    assert "warranty_claimed" not in result["slots"]
    assert result["slots"].get("outcome") is None


@pytest.mark.parametrize("text", [
    "क्या मेरे पास bill है?", "bill hai kya?", "agar bill mil gaya to?", "if I have the bill?",
    "शायद bill है", "payment mil gaya", "product mil gaya", "refund mil gaya", "technician mil gaya",
])
def test_questions_hypotheticals_and_other_subjects_do_not_become_current_possession(text):
    result = run("n_bill_charge_q", text)
    assert "bill_available_now" not in result["slots"]
    assert result["awaiting"] != PREFIX + "service_need"


@pytest.mark.parametrize("text", ["मेरे पास bill है नहीं", "bill available hai nahi", "ab bill nahi hai", "मेरे पास बिल नहीं है।"])
def test_current_negation_overwrites_stale_yes_without_becoming_payment_refusal(text):
    result = run("n_bill_charge_q_clarify", text, "refusal", {"bill_available_now": "yes", "bill_available_at_visit": "yes"})
    assert result["slots"]["bill_available_now"] == "no"
    assert result["slots"]["bill_available_at_visit"] == "yes"
    assert result["awaiting"] == "n_bill_charge_q"
    assert result["slots"].get("outcome") is None


@pytest.mark.parametrize("hub", CONTACT_HUBS)
@pytest.mark.parametrize("text", ["कॉल आया था।", "call aaya tha", "I received a call", "technician called"])
@pytest.mark.parametrize("signal", [None, "affirm"])
def test_phone_call_does_not_confirm_a_technician_visit(hub, text, signal):
    result = run(hub, text, signal)
    assert result["awaiting"] == PREFIX + "visit_q"
    assert result["slots"]["technician_contact_type"] == "phone_call"
    assert "n_reason_bill_q" not in result["trace"]


@pytest.mark.parametrize("text", ["call aaya tha aur visit bhi hui thi", "call आया था, visit भी हुई थी", "technician came"])
def test_explicit_visit_may_progress_to_historical_bill_question(text):
    result = run("n_intro_bill", text, "affirm")
    assert result["awaiting"] == "n_reason_bill_q"
    assert result["slots"]["technician_contact_type"] == "visit"


@pytest.mark.parametrize("text", ["call aaya tha visit nahi hui", "visit हुई नहीं थी", "technician आया नहीं था"])
def test_explicit_no_visit_keeps_phone_contact_separate(text):
    result = run("n_intro_bill", text, "affirm")
    assert result["awaiting"] == "n_apology_visit"
    assert result["slots"]["technician_contact_type"] == "no_visit"


@pytest.mark.parametrize("text", ["कॉल आया नहीं था", "call nahi aaya", "no call"])
def test_denied_phone_call_does_not_claim_contact_or_a_visit(text):
    result = run("n_intro_bill", text, "affirm")
    assert result["awaiting"] == "n_intro_bill_clarify"
    assert "technician_contact_type" not in result["slots"]


def test_repeated_phone_only_has_distinct_once_then_safe_unknown():
    first = run("n_intro_bill", "कॉल आया था।")
    second = run(first["awaiting"], "कॉल आया था।", slots=first["slots"])
    assert second["awaiting"] == PREFIX + "visit_clarify"
    assert first["reply"] != second["reply"]
    final = run(second["awaiting"], "कॉल आया था।", slots=second["slots"])
    assert final["slots"]["outcome"] == "unverified_no_action"
    assert "n_reason_bill_q" not in final["trace"]


@pytest.mark.parametrize("text", [
    "क्या ये warranty में है?", "ये warranty में नहीं है।", "warranty mein hai kya?",
    "अगर warranty में है तो charge क्यों?", "शायद warranty में है।",
    "मुझे नहीं पता warranty है या नहीं।", "warranty नहीं, बस bill है।",
])
def test_warranty_question_negation_and_uncertainty_do_not_record_warranty_or_cancel(text):
    result = run("n_bill_charge_q", text, "refusal")
    assert result["slots"].get("warranty_claimed") != "yes"
    assert result["slots"].get("outcome") is None
    assert result["awaiting"] in (PREFIX + "now_q", PREFIX + "service_need")


@pytest.mark.parametrize("text", ["warranty mein hai", "under warranty", "free service"])
def test_explicit_warranty_claim_keeps_the_existing_reviewed_branch(text):
    result = run("n_bill_charge_q", text)
    assert result["awaiting"] == "n_warranty_claim_q"
    assert result["slots"]["warranty_claimed"] == "yes"


@pytest.mark.parametrize("text", ["bill hai, next week chahiye", "bill hai baad mein chahiye", "bill hai, abhi bahar hoon", "bill hai, abhi nahi", "bill hai stop calling", "bill hai already paid"])
def test_prior_timing_optout_and_paid_routes_are_preserved(text):
    original = fixture()
    before = run("n_bill_charge_q", text, definition=original)
    after = run("n_bill_charge_q", text, definition=apply_bill_state_repair(original))
    assert after["awaiting"] == before["awaiting"]
    assert after["slots"].get("outcome") == before["slots"].get("outcome")


def test_explicit_service_refusal_wins_over_bill_and_warranty_context():
    result = run("n_bill_charge_q", "bill hai, warranty mein hai, service nahi chahiye")
    assert result["slots"]["outcome"] == "cancel_no_service"


def test_stale_routing_marker_cannot_turn_an_unclear_turn_into_a_bill_correction():
    result = run("n_bill_charge_q", "हम्म।", slots={"haier_bill_state_n_bill_charge_q_route": "current_yes"})
    assert "bill_available_now" not in result["slots"]
    assert result["awaiting"] != PREFIX + "service_need"


@pytest.mark.parametrize(("hub", "text", "signal", "target", "fact", "value"), [
    (PREFIX+"now_q", "हाँ।", "affirm", PREFIX+"service_need", "bill_available_now", "yes"),
    (PREFIX+"now_q", "नहीं।", "refusal", "n_bill_charge_q", "bill_available_now", "no"),
    (PREFIX+"visit_q", "हाँ।", "affirm", "n_reason_bill_q", "technician_contact_type", "visit"),
    (PREFIX+"visit_q", "नहीं।", "refusal", "n_apology_visit", "technician_contact_type", "no_visit"),
])
def test_new_authored_questions_accept_a_contextual_bare_answer(hub, text, signal, target, fact, value):
    result = run(hub, text, signal)
    assert result["awaiting"] == target
    assert result["slots"][fact] == value


@pytest.mark.parametrize("text", ["हाँ?", "नहीं?", "yes?", "no?", "हाँ？"])
def test_bare_polarity_question_does_not_establish_current_bill_possession(text):
    result = run(PREFIX+"now_q", text)
    assert "bill_available_now" not in result["slots"]
    assert result["awaiting"] == PREFIX+"now_clarify"


def test_charge_question_can_correct_a_stale_current_bill_fact_to_missing():
    result = run("n_bill_charge_q", "ab bill nahi hai to charges kyun doon?", slots={"bill_available_now": "yes"})
    assert result["slots"]["bill_available_now"] == "no"
    assert result["awaiting"] == PREFIX+"now_q"
    assert "आपके पास bill है" not in result["reply"]


def test_bill_correction_still_requires_service_and_explicit_forward_permission():
    definition = apply_bill_state_repair(fixture())
    corrected = run("n_bill_charge_q", "अरे पर मेरे पास बिल है।", definition=definition)
    service = run(corrected["awaiting"], "हाँ।", "affirm", corrected["slots"], definition=definition)
    assert service["awaiting"] == "n_redispatch"
    assert "outcome" not in service["slots"]
    consent = run(service["awaiting"], "हाँ कर दीजिए।", "affirm", service["slots"], definition=definition)
    assert consent["slots"]["outcome"] == "redispatch"
    assert consent["slots"]["bill_available_now"] == "yes"


def test_bill_clarification_retains_ownership_when_a_missed_visit_is_reported():
    first = run(PREFIX+"service_need", "visit nahi hui hai")
    assert first["awaiting"] == PREFIX+"service_need_clarify"
    correction = run(first["awaiting"], "मेरे पास बिल है।", slots=first["slots"])
    assert correction["awaiting"] == PREFIX+"service_need"
    assert correction["slots"]["bill_available_now"] == "yes"


@pytest.mark.parametrize("fault", ["bot", "version", "node", "correction"])
def test_transform_rejects_an_unreviewed_workflow_contract(fault):
    definition = fixture()
    if fault == "bot":
        definition["bot_id"] = "another_bot"
    elif fault == "version":
        next(n for n in definition["nodes"] if n["id"] == "n_identity_gate")["config"]["haierFollowupConsentRepairVersion"] = 3
    elif fault == "node":
        definition["nodes"] = [n for n in definition["nodes"] if n["id"] != "n_bill_dealer_q"]
    else:
        next(n for n in definition["nodes"] if n["id"] == "n_intro_bill")["config"]["alsoCapture"] = []
    with pytest.raises(ValueError):
        apply_bill_state_repair(definition)


def test_transform_is_pure_idempotent_json_safe_and_does_not_edit_general_service_nodes():
    original = fixture()
    snapshot = deepcopy(original)
    repaired = apply_bill_state_repair(original)
    assert original == snapshot
    assert apply_bill_state_repair(repaired) == repaired
    assert apply_bill_state_repair(json.loads(json.dumps(repaired, sort_keys=True))) == repaired
    assert next(n for n in repaired["nodes"] if n["id"] == "n_identity_gate")["config"][MARKER] == VERSION
    for hub in ("n_service_need", "n_service_need_clarify", "n_redispatch", "n_intro_not_haier"):
        assert next(n for n in repaired["nodes"] if n["id"] == hub) == next(n for n in original["nodes"] if n["id"] == hub)
        assert [e for e in repaired["edges"] if e["from"] == hub] == [e for e in original["edges"] if e["from"] == hub]
