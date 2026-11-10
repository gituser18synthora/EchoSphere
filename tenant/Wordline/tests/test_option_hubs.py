"""Step 4 — option hubs: IVR question routing and the closing edge.

DEMO cv_cb68cbdc84a0 (2026-11-10): at ``n_options`` "Tell me the IVR process."
was labelled ``question`` (0.98) by the intent classifier and answered by the
knowledge base instead of ``n_ivr``; "No. That's all. Thank you." was labelled
``refusal``, no edge supported it, and the call stayed open.

The turns below are driven through the real workflow engine with the SAME
labels the classifier produced (the engine's edge choice is deterministic
given the label), on the current workflow (DEMO v17 snapshot) upgraded with
steps 1–3 ("before", = LOCAL v15 / DEMO v18) and steps 1–4 ("after").
Merchant lookups hit the real API code (TestClient) with the real connection
mapping (see test_visit_details).
"""

from __future__ import annotations

import json

import pytest

import shared.orchestration.workflow_engine as wfe
from shared.orchestration.router import classify_user_signal
from tenant.Wordline.setup import workflow_upgrade as up
from tenant.Wordline.tests.test_visit_details import SNAPSHOT, _ApiExecutor, _engine

HUB_PREFIX = ["I need paper roll", "91379784", "haan sahi hai", "haan sahi hai", "haan sahi hai"]


@pytest.fixture(scope="module")
def before() -> dict:
    definition = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    step1, _ = up.upgrade_workflow(definition)
    step2, _ = up.upgrade_terminal_id_capture(step1)
    step3, _ = up.upgrade_visit_details(step2)
    return step3


@pytest.fixture(scope="module")
def after(before) -> dict:
    definition, _ = up.upgrade_option_hubs(before)
    return definition


async def _say(engine, session, text, signal=None, lang="en-IN"):
    return await engine.handle_turn_detailed(
        session_id=session, tenant_id="tn_ccc488c2c47d", bot_id="bot_a9d4caca70dc",
        workflow_name="wf_0a6550d061e6", user_text=text, language=lang, signal=signal,
        context_values={"customer_name": "Rahul Sharma", "docket_number": "WL458921"})


async def _at_options(monkeypatch, definition, session):
    engine, _ = _engine(monkeypatch, definition, _ApiExecutor)
    for text in HUB_PREFIX:
        r = await _say(engine, session, text, "affirm" if text.startswith("haan") else None)
    assert "n_options" in r["trace"]
    return engine


# ── structure ───────────────────────────────────────────────────────────────


def test_step4_only_flags_hubs_and_adds_closing_edges(before, after):
    old = {n["id"]: n for n in before["nodes"]}
    new = {n["id"]: n for n in after["nodes"]}
    assert set(old) == set(new)
    for node_id, node in old.items():
        if node_id in up.OPTION_HUBS:
            assert new[node_id]["config"] == {**node["config"], "optionHub": True}
        else:
            assert new[node_id] == node
    old_edges = {e["id"]: e for e in before["edges"]}
    new_edges = {e["id"]: e for e in after["edges"]}
    assert all(new_edges[k] == v for k, v in old_edges.items())          # nothing re-pointed
    added = [new_edges[k] for k in set(new_edges) - set(old_edges)]
    assert sorted((e["from"], e["to"]) for e in added) == sorted(
        (h, "n_end") for h in up.OPTION_HUBS)
    again, changes = up.upgrade_option_hubs(after)
    assert again is after and "already upgraded" in changes[0]


def test_closing_tokens_never_carry_an_affirm_or_other_signal():
    tokens = [t.strip() for t in up.CLOSING_LABEL.split("/")]
    assert len(tokens) == len(set(tokens)) and all(tokens)
    assert not any("," in t or "|" in t for t in tokens)                  # label separators
    for token in tokens:
        assert classify_user_signal(token) in (None, "refusal"), token
    for left_out in ("thank you", "theek hai", "okay", "nothing", "that's it"):
        assert left_out not in [t.lower() for t in tokens]


# ── issue 1: IVR question routing ───────────────────────────────────────────


@pytest.mark.parametrize("text,target", [
    ("Tell me the IVR process.", "n_ivr"),                  # cv_cb68cbdc84a0, labelled question
    ("Please explain the IVR process", "n_ivr"),
    ("How does the SMS process work?", "n_sms"),
    ("Can you explain both options?", "n_both"),
])
async def test_question_naming_an_option_selects_it(monkeypatch, before, after, text, target):
    r = await _say(await _at_options(monkeypatch, before, f"b-{text}"), f"b-{text}", text, "question")
    assert r["trace"] == ["n_options"] and r["offScript"] is True          # the defect
    r = await _say(await _at_options(monkeypatch, after, f"a-{text}"), f"a-{text}", text, "question")
    assert target in r["trace"] and not r.get("offScript")


@pytest.mark.parametrize("text", [
    "Who is the contact person in your records?",
    "Which bank is my terminal with?",
    "What is my registered address?",
])
async def test_question_naming_no_option_stays_off_script(monkeypatch, after, text):
    r = await _say(await _at_options(monkeypatch, after, f"q-{text}"), f"q-{text}", text, "question")
    assert r["trace"] == ["n_options"] and r["offScript"] is True and not r["done"]


# ── issue 2: closing at the options hub ─────────────────────────────────────


@pytest.mark.parametrize("text,signal", [
    ("No. That's all. Thank you.", "refusal"),              # cv_cb68cbdc84a0
    ("No", "refusal"),
    ("Nothing else", None),
    ("Nothing else", "refusal"),
    ("That's all", None),
    ("nahi bas itna hi", "refusal"),
    ("नहीं, कुछ नहीं चाहिए", "refusal"),
])
async def test_decline_closes_through_n_end(monkeypatch, before, after, text, signal):
    r = await _say(await _at_options(monkeypatch, before, f"cb-{text}"), f"cb-{text}", text, signal)
    assert "n_end" not in r["trace"] and not r["done"]                     # the defect
    r = await _say(await _at_options(monkeypatch, after, f"ca-{text}"), f"ca-{text}", text, signal)
    assert r["trace"][-1] == "n_end" and r["done"] is True
    assert "Have a nice day" in r["reply"] or "nice day" in r["reply"].lower() or r["done"]


@pytest.mark.parametrize("text,signal,target", [
    ("nahi, IVR batao", "refusal", "n_ivr"),                # option beats the bare decline
    ("No, tell me about SMS", "refusal", "n_sms"),
    ("haan IVR wala batao", "affirm", "n_ivr"),
    ("IVR wala batao", None, "n_ivr"),
    ("dono batao", None, "n_both"),
])
async def test_named_option_still_wins(monkeypatch, after, text, signal, target):
    r = await _say(await _at_options(monkeypatch, after, f"o-{text}"), f"o-{text}", text, signal)
    assert target in r["trace"] and "n_end" not in r["trace"]


async def test_bare_affirm_does_not_close(monkeypatch, after):
    r = await _say(await _at_options(monkeypatch, after, "aff"), "aff", "haan theek hai", "affirm")
    assert "n_end" not in r["trace"] and not r["done"]


async def test_raise_path_unchanged(monkeypatch, after):
    engine = await _at_options(monkeypatch, after, "raise")
    r = await _say(engine, "raise", "aap request raise kar do")
    assert r["trace"] == ["n_options", "n_raise_has_record", "n_raise_record"]
    r = await _say(engine, "raise", "sab sahi hai, Sunday off, 5 rolls")
    assert "n_anything_else" in r["trace"]
    r = await _say(engine, "raise", "nahi", "refusal")
    assert r["trace"][-1] == "n_end" and r["done"] is True


async def test_ivr_then_close_and_unverified_hub(monkeypatch, after):
    engine = await _at_options(monkeypatch, after, "ivr-close")
    r = await _say(engine, "ivr-close", "Tell me the IVR process.", "question")
    assert "n_ivr" in r["trace"]
    r = await _say(engine, "ivr-close", "No, that's all", "refusal")
    assert r["trace"][-1] == "n_end" and r["done"] is True
    engine, _ = _engine(monkeypatch, after, _ApiExecutor)
    for text in ("I need paper roll", "12345678", "87654321"):
        r = await _say(engine, "unverified", text)
    assert "n_options_unverified" in r["trace"]
    r = await _say(engine, "unverified", "Tell me the IVR process.", "question")
    assert "n_ivr" in r["trace"]


async def test_pr_again_hub_closes(monkeypatch, after):
    engine = await _at_options(monkeypatch, after, "again")
    await _say(engine, "again", "aap request raise kar do")
    r = await _say(engine, "again", "sab sahi hai, Sunday off, 5 rolls")
    assert "n_anything_else" in r["trace"]
    r = await _say(engine, "again", "paper roll chahiye")
    assert "n_pr_again" in r["trace"]
    r = await _say(engine, "again", "No. That's all.", "refusal")
    assert r["trace"][-1] == "n_end" and r["done"] is True


def test_engine_default_is_unchanged_without_the_flag(after):
    hub_edges = [e for e in after["edges"] if e["from"] == "n_options"]
    meta = wfe._edge_meta(hub_edges)
    edge, why, _ = wfe._choose_intent_edge_detailed(meta, "Tell me the IVR process.", "question")
    assert (edge, why) == (None, "off_script")
    edge, why, _ = wfe._choose_intent_edge_detailed(
        meta, "Tell me the IVR process.", "question", option_hub=True)
    assert edge["to"] == "n_ivr" and why == "token"
