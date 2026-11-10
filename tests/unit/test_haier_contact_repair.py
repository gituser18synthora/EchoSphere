"""Customer-answer regressions through the real workflow interpreter.

No external model is mocked as if it had understood these cases: these tests
exercise the scoped authored answer schema and real deterministic interpreter.
The separate call replay evaluates the live decision model as well.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import re

import pytest

from shared.orchestration.router import classify_user_signal
from shared.orchestration.entity_extractor import extract_entity
from tests.golden.harness import patched


repair = importlib.import_module("haier.setup.07_demo_workflow_repair")


def fixture():
    def intent(node_id, prompt):
        return {"id": node_id, "kind": "intent", "config": {
            "prompt": prompt, "elseIsAnswer": True, "responseMode": "fixed"}}

    def edge(edge_id, source, target, label=""):
        return {"id": edge_id, "from": source, "to": target, "label": label}

    return {"id": repair.WORKFLOW_ID, "name": "Haier contact regression", "version": 77,
            "nodes": [
                {"id": "start", "kind": "start", "config": {}},
                intent("n_intro_unit_ok", "Did a Haier technician call or visit?"),
                intent("n_intro_unit_ok_clarify", "Did a technician call or visit?"),
                intent("n_reason_unit_ok_q", "Does the problem still persist?"),
                intent("n_apologize_no_visit", "Sorry you had no call or visit. Is service needed?"),
                intent("n_apology_call", "Sorry that no technician call came. Is service needed?"),
                intent("n_apology_visit", "Sorry that no technician visit happened. Is service needed?"),
                intent("n_v2_completed_check", "Is the service complete, with nothing pending?"),
                intent("n_v2_pending_case_check", "Does an existing repair remain pending?"),
                {"id": "m_n_v2_end_unverified", "kind": "end",
                 "config": {"text": "Unconfirmed status; no action."}},
            ],
            "edges": [
                edge("start", "start", "n_intro_unit_ok"),
                edge("e_n_intro_unit_ok_yes", "n_intro_unit_ok", "n_reason_unit_ok_q", "हाँ/yes/call आया था"),
                edge("e_n_intro_unit_ok_no", "n_intro_unit_ok", "n_apologize_no_visit", "नहीं/no"),
                edge("e_v2_special_pending", "n_intro_unit_ok", "n_v2_pending_case_check", "repair pending/part pending/पार्ट pending"),
                edge("e_v2_special_completed", "n_intro_unit_ok", "n_v2_completed_check", "installation हो गया/installation completed/work done"),
                edge("else", "n_intro_unit_ok", "n_intro_unit_ok_clarify", "else"),
                edge("clarify_yes", "n_intro_unit_ok_clarify", "n_reason_unit_ok_q", "हाँ/yes"),
                edge("clarify_no", "n_intro_unit_ok_clarify", "n_apologize_no_visit", "नहीं/no"),
                edge("clarify_else", "n_intro_unit_ok_clarify", "m_n_v2_end_unverified", "else"),
            ]}


async def replay(text, *, signal=None):
    definition = repair.apply_contact_repair(fixture())
    with patched(definition, []) as (engine, _):
        await engine.handle_turn_detailed(
            session_id="haier-contact-test", tenant_id="haier-test", bot_id="haier-test",
            workflow_name=definition["name"], user_text="", language="hi-IN", reset_state=True)
        return await engine.handle_turn_detailed(
            session_id="haier-contact-test", tenant_id="haier-test", bot_id="haier-test",
            workflow_name=definition["name"], user_text=text, language="hi-IN",
            signal=signal if signal is not None else classify_user_signal(text))


@pytest.mark.parametrize("text", [
    "हुई तो थी बट मैं अभी सेटिस्फाई नहीं हूँ।",
    "हुई थी लेकिन अभी काम नहीं हुआ।",
    "नहीं, कॉल आया था।",
    "कॉल आया था लेकिन समस्या ठीक नहीं हुई।",
    "फोन आया था मगर मैं संतुष्ट नहीं हूँ।",
    "No, I received a call.",
    "They visited, but the problem is not fixed.",
    "हाँ जी।",
    "yes",
    "मुझे installation की तारीख याद नहीं, technician आया था।",
])
def test_contact_confirmation_does_not_become_no_contact_from_other_negation(text):
    result = asyncio.run(replay(text))
    assert result["trace"][-1] == "n_reason_unit_ok_q"
    assert "n_apologize_no_visit" not in result["trace"]
    assert result["slots"][repair.CONTACT_VARIABLE] == "contact_confirmed"


@pytest.mark.parametrize("text", [
    "नहीं।", "नहीं जी।", "ना call आया ना visit हुई।",
])
def test_explicit_absence_of_contact_still_takes_no_contact_branch(text):
    result = asyncio.run(replay(text))
    assert result["trace"][-1] == "n_apologize_no_visit"
    assert result["slots"][repair.CONTACT_VARIABLE] == "no_contact"


@pytest.mark.parametrize(("text", "canonical", "expected"), [
    ("कॉल नहीं आया था।", "no_call", "n_apology_call"),
    ("No one called.", "no_call", "n_apology_call"),
    ("I did not receive any call.", "no_call", "n_apology_call"),
    ("I have never received a call.", "no_call", "n_apology_call"),
    ("No technician visited.", "no_visit", "n_apology_visit"),
    ("Technician did not visit.", "no_visit", "n_apology_visit"),
    ("कोई नहीं आया।", "no_visit", "n_apology_visit"),
    ("technician आया था नहीं।", "no_visit", "n_apology_visit"),
])
def test_negative_contact_does_not_invent_absence_of_the_other_contact_method(text, canonical, expected):
    result = asyncio.run(replay(text))
    assert result["trace"][-1] == expected
    assert result["slots"][repair.CONTACT_VARIABLE] == canonical
    assert "n_apologize_no_visit" not in result["trace"]


@pytest.mark.parametrize("text", [
    "पता नहीं।", "मुझे याद नहीं।", "शायद आया था।", "I'm not sure.",
    "technician आने वाला है", "कॉल आएगा", "अभी प्रॉब्लम ठीक नहीं है।",
    "installation नहीं हुआ है।",
    "काम नहीं हुआ है।",
])
def test_uncertain_future_or_other_negative_statement_does_not_invent_contact(text):
    result = asyncio.run(replay(text))
    assert "n_apologize_no_visit" not in result["trace"]
    assert "n_reason_unit_ok_q" not in result["trace"]
    assert result["slots"].get(repair.CONTACT_VARIABLE) in (None, "unknown")
    assert "outcome" not in result["slots"]
    assert not result["done"]


@pytest.mark.parametrize(("text", "expected"), [
    ("installation हो गया है", "n_v2_completed_check"),
    ("Work done", "n_v2_completed_check"),
    ("repair pending", "n_v2_pending_case_check"),
    ("हाँ, पार्ट pending है", "n_v2_pending_case_check"),
])
def test_existing_completed_and_pending_overrides_keep_original_destination(text, expected):
    assert asyncio.run(replay(text))["trace"][-1] == expected


@pytest.mark.parametrize(("special", "expected"), [
    ("repair pending", "n_v2_pending_case_check"),
    ("installation completed", "n_v2_completed_check"),
])
@pytest.mark.parametrize("contact", [
    "technician आया था।", "कॉल नहीं आया था।", "पता नहीं।",
])
def test_special_case_exclusion_still_wins_over_contact_facts(special, expected, contact):
    assert asyncio.run(replay(f"{contact} {special}"))["trace"][-1] == expected


def test_each_contact_canonical_checks_the_special_case_guard_only_once():
    """Large authored alias lists must not be repeated for every phrasing."""
    special_edges = [edge for edge in fixture()["edges"]
                     if edge["id"].startswith("e_v2_special_")]
    entity = repair.contact_entity(special_edges)
    patterns = entity["synonymPatterns"]
    assert list(patterns) == ["contact_confirmed", "no_contact", "no_call", "no_visit", "unknown"]
    for alternatives in patterns.values():
        assert len(alternatives) == 1
        assert alternatives[0].count(repair._literal_pattern([
            token for edge in special_edges for token in repair._tokens(edge["label"])
        ])) == 1
    for special in ("repair pending", "installation completed"):
        assert not extract_entity(f"technician आया था। {special}", entity)["matched"]


def test_factored_literal_guard_retains_unicode_boundaries_prefixes_and_escaping():
    aliases = ["ab", "abc", "a.bc", "Haier", "विज़िट", "विज़िट हुई", "work done", "C++", "x|y"]
    original = re.compile(repair._BOUNDARY + "(?:" + "|".join(re.escape(t) for t in aliases) + ")", re.I)
    factored = re.compile(repair._literal_pattern(aliases), re.I)
    for token in aliases + ["aXbc", "विजिट", "x", "y", "unrelated", "HAIER"]:
        for prefix in ("", " ", "z", "_", "ह", "\n"):
            for suffix in ("", "?", "def", " हुई"):
                text = prefix + token + suffix
                assert bool(factored.search(text)) == bool(original.search(text)), text


def test_workflow_patch_is_pure_and_idempotent():
    source = fixture()
    original = deepcopy(source)
    result = repair.apply_contact_repair(source)
    assert source == original
    assert repair.apply_contact_repair(result) == result


def test_unreviewed_contact_destination_fails_closed():
    source = fixture()
    next(e for e in source["edges"] if e["id"] == "e_n_intro_unit_ok_no")["to"] = "other"
    with pytest.raises(ValueError, match="Unexpected Unit-OK"):
        repair.apply_contact_repair(source)
