"""Regression checks for the dedicated Zepto MDND reference-call config."""

import runpy

from shared.orchestration.placeholders import sanitize_spoken_text


def _config():
    module = runpy.run_path("zepto/setup/06_single_bots.py")
    spec = next(
        item for item in module["CONCERNS"]
        if item["state_key"] == "BOT_MDND"
    )
    nodes, edges = module["build_mdnd_workflow"]()
    return module, spec, {node["id"]: node for node in nodes}, edges


def test_greeting_uses_partner_name_and_is_safe_when_name_is_missing():
    _module, spec, _nodes, _edges = _config()
    named = sanitize_spoken_text(
        spec["greeting_hi"],
        {"partner_name": "Saurabh", "voice_speaker_name": "Kavya"},
    )
    missing = sanitize_spoken_text(
        spec["greeting_hi"], {"voice_speaker_name": "Kavya"},
    )
    assert "Saurabh" in named
    assert "Rajesh" not in named
    assert "नमस्ते!" in missing
    assert "delivery partner से बात" in missing


def test_known_ticket_facts_are_prefilled_and_incident_answers_are_structured():
    _module, _spec, nodes, _edges = _config()
    assert nodes["n_ask_amount"]["config"]["prefillFromContext"] == \
        "mdnd_deduction_amount"
    assert nodes["n_ask_order"]["config"]["prefillFromContext"] == \
        "mdnd_order_last4"
    assert nodes["n_ask_date"]["config"]["prefillFromContext"] == \
        "mdnd_deduction_date"
    assert nodes["n_ask_reached"]["config"]["variable"] == \
        "m_reached_location"
    assert nodes["n_ask_called"]["config"]["variable"] == \
        "m_called_customer"
    assert nodes["n_ask_handover"]["config"]["variable"] == \
        "m_handover_recipient"


def test_correction_node_updates_every_structured_mdnd_answer():
    _module, _spec, nodes, _edges = _config()
    captures = nodes["n_ask_correction"]["config"]["alsoCapture"]
    variables = {item["variable"] for item in captures}
    assert {
        "m_deduction_amount", "m_order_last4", "m_deduction_date",
        "m_reached_location", "m_called_customer",
        "m_handover_recipient", "m_cx_support_call",
    } <= variables
    assert all(item.get("overwrite") is True for item in captures)
