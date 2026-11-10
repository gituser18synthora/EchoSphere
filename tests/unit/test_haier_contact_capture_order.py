"""Contact routing must survive JSON object ordering after persistence.

MySQL JSON reorders object keys, while JSON arrays retain their order. These
regressions exercise the real capture helper and workflow interpreter, rather
than assuming Python mapping equality proves equivalent routing behavior.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from shared.orchestration.ask_resolution import _apply_also_capture
from shared.orchestration.router import classify_user_signal
from tests.golden.harness import patched
from tests.unit.test_haier_contact_repair import fixture, repair


ORDERS = ("original", "reversed", "mysql")
PRIORITY = ("unknown", "no_visit", "no_call", "no_contact", "contact_confirmed")


def persisted(value, order):
    """Round-trip objects in observed storage orders, preserving array order."""
    def reorder(item):
        if isinstance(item, list):
            return [reorder(child) for child in item]
        if not isinstance(item, dict):
            return item
        keys = list(item)
        if order == "reversed":
            keys.reverse()
        elif order == "mysql":
            # The observed DEMO object order is byte length, then byte value.
            keys.sort(key=lambda key: (len(key.encode("utf-8")), key.encode("utf-8")))
        return {key: reorder(item[key]) for key in keys}

    return json.loads(json.dumps(reorder(value), ensure_ascii=False))


def intro(definition, node_id="n_intro_unit_ok"):
    return next(node for node in definition["nodes"] if node["id"] == node_id)


def captures(node):
    return [spec for spec in node["config"].get("alsoCapture", [])
            if spec.get("variable") == repair.CONTACT_VARIABLE]


def legacy_entity(definition, node_id="n_intro_unit_ok"):
    return repair.contact_entity([
        edge for edge in definition["edges"]
        if edge["from"] == node_id and edge["id"].startswith("e_v2_special_")
    ])


async def workflow_reply(definition, text):
    with patched(definition, []) as (engine, _):
        arguments = {
            "session_id": "haier-contact-order-test", "tenant_id": "haier-test",
            "bot_id": "haier-test", "workflow_name": definition["name"],
            "language": "hi-IN",
        }
        await engine.handle_turn_detailed(**arguments, user_text="", reset_state=True)
        return await engine.handle_turn_detailed(
            **arguments, user_text=text, signal=classify_user_signal(text))


@pytest.mark.parametrize("text", [
    "हुई तो थी बट मैं अभी सेटिस्फाई नहीं हूँ।",
    "कॉल आया था लेकिन समस्या ठीक नहीं हुई।",
])
def test_legacy_first_match_changes_when_mysql_reorders_canonical_object(text):
    """Reproduce the failure that mapping-equality comparisons concealed."""
    source = fixture()
    node = intro(source)
    node["config"]["alsoCapture"] = [{
        "variable": repair.CONTACT_VARIABLE, "entity": legacy_entity(source),
        "overwrite": True,
    }]
    before, after = {}, {}
    _apply_also_capture(node, text, before, [], node["id"])
    stored = persisted(node, "mysql")
    _apply_also_capture(stored, text, after, [], node["id"])
    assert node == stored  # Python equality ignores the relevant key order.
    assert before[repair.CONTACT_VARIABLE] == "contact_confirmed"
    assert after[repair.CONTACT_VARIABLE] == "unknown"


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize(("text", "canonical", "destination"), [
    ("हुई तो थी बट मैं अभी सेटिस्फाई नहीं हूँ।", "contact_confirmed", "n_reason_unit_ok_q"),
    ("कॉल आया था लेकिन समस्या ठीक नहीं हुई।", "contact_confirmed", "n_reason_unit_ok_q"),
    ("ना call आया ना visit हुई।", "no_contact", "n_apologize_no_visit"),
    ("कॉल नहीं आया, visit नहीं हुई।", "no_contact", "n_apologize_no_visit"),
    ("कॉल आया था, visit नहीं हुई।", "contact_confirmed", "n_reason_unit_ok_q"),
    ("नहीं, कॉल आया था।", "contact_confirmed", "n_reason_unit_ok_q"),
    ("कॉल नहीं आया था।", "no_call", "n_apology_call"),
    ("No technician visited.", "no_visit", "n_apology_visit"),
    ("हाँ जी।", "contact_confirmed", "n_reason_unit_ok_q"),
    ("नहीं।", "no_contact", "n_apologize_no_visit"),
    ("पता नहीं।", "unknown", "n_intro_unit_ok_clarify"),
    ("शायद technician आया था।", "unknown", "n_intro_unit_ok_clarify"),
    ("अभी प्रॉब्लम ठीक नहीं है।", "unknown", "n_intro_unit_ok_clarify"),
    ("मुझे installation की तारीख याद नहीं, technician आया था।", "contact_confirmed", "n_reason_unit_ok_q"),
])
def test_capture_and_actual_route_are_stable_after_json_object_reordering(
        order, text, canonical, destination):
    definition = persisted(repair.apply_contact_repair(fixture()), order)
    node = intro(definition)
    # Start with an old value so overwrite priority is also exercised.
    slots = {repair.CONTACT_VARIABLE: "previous_answer"}
    audit = []
    _apply_also_capture(node, text, slots, audit, node["id"])
    assert slots[repair.CONTACT_VARIABLE] == canonical

    result = asyncio.run(workflow_reply(definition, text))
    assert result["slots"][repair.CONTACT_VARIABLE] == canonical
    assert result["trace"][-1] == destination
    assert not result["done"]
    assert "outcome" not in result["slots"]


@pytest.mark.parametrize("order", ORDERS)
def test_priority_is_an_array_of_single_canonical_overwriting_captures(order):
    definition = persisted(repair.apply_contact_repair(fixture()), order)
    for node_id in repair.INTRO_IDS:
        specs = captures(intro(definition, node_id))
        assert len(specs) == len(PRIORITY)
        assert [next(iter(spec["entity"]["synonymPatterns"])) for spec in specs] == list(PRIORITY)
        for spec in specs:
            assert spec["overwrite"] is True
            assert spec["entity"]["matchCanonicalValues"] is False
            assert len(spec["entity"]["synonymPatterns"]) == 1


@pytest.mark.parametrize("order", ORDERS)
def test_already_marked_single_capture_upgrades_purely_without_changing_graph(order):
    repaired = repair.apply_contact_repair(fixture())
    unrelated = [
        {"variable": "unrelated_before", "entity": {
            "dataType": "text", "synonymPatterns": {"yes": [r"before"]}}},
        {"variable": "unrelated_after", "entity": {
            "dataType": "text", "synonymPatterns": {"yes": [r"after"]}}, "overwrite": True},
    ]
    for node_id in repair.INTRO_IDS:
        intro(repaired, node_id)["config"]["alsoCapture"] = [
            deepcopy(unrelated[0]),
            {"variable": repair.CONTACT_VARIABLE,
             "entity": legacy_entity(repaired, node_id), "overwrite": True},
            deepcopy(unrelated[1]),
        ]
    source = persisted(repaired, order)
    original = deepcopy(source)
    result = repair.apply_contact_repair(source)
    assert source == original
    assert json.dumps(source, ensure_ascii=False) == json.dumps(original, ensure_ascii=False)
    assert result["edges"] == original["edges"]
    assert [node["id"] for node in result["nodes"]] == [node["id"] for node in original["nodes"]]
    assert repair.apply_contact_repair(result) == result
    assert json.dumps(repair.apply_contact_repair(result), ensure_ascii=False) == json.dumps(result, ensure_ascii=False)
    for node_id in repair.INTRO_IDS:
        node = intro(result, node_id)
        assert [spec for spec in node["config"]["alsoCapture"]
                if spec.get("variable") != repair.CONTACT_VARIABLE] == unrelated
        assert len(captures(node)) == len(PRIORITY)
        slots = {}
        _apply_also_capture(node, "हुई तो थी बट मैं अभी सेटिस्फाई नहीं हूँ।", slots, [], node_id)
        assert slots[repair.CONTACT_VARIABLE] == "contact_confirmed"
