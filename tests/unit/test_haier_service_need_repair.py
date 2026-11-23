"""Service-refusal regressions through the real, in-memory workflow engine."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import pytest

from haier.setup.haier_service_need_repair import (
    HUBS, NO_SERVICE, VARIABLE, WORKFLOW_ID, apply_service_need_repair,
)
from tests.golden.harness import patched


def fixture(hub="n_service_need"):
    nodes = [
        {"id": "start", "kind": "start", "config": {}},
        {"id": "n_redispatch", "kind": "intent", "config": {"prompt": "May I forward this for re-dispatch?"}},
        {"id": NO_SERVICE, "kind": "message", "config": {"silent": True, "setSlots": {"outcome": "cancel_no_service"}}},
        {"id": "end", "kind": "end", "config": {"text": "No service needed; forwarding for cancellation. Thank you."}},
    ]
    for node_id in (*HUBS, "future", "dated", "uncertain", "pending"):
        nodes.append({"id": node_id, "kind": "intent", "config": {"prompt": "Please confirm.", "elseIsAnswer": True}})
    edges = [{"id": "start", "from": "start", "to": hub},
             {"id": "close", "from": NO_SERVICE, "to": "end"}]
    for source in HUBS:
        suffix = "__clarify" if source.endswith("_clarify") else ""
        specs = [
            ("e_service_yes" + suffix, "n_redispatch", "हाँ/yes/haan/service चाहिए"),
            ("e_service_no" + suffix, NO_SERVICE, "नहीं/no/nahi/nahin/service nahi chahiye"),
            (f"e_{source}__service_required", "n_redispatch", "रिक्वायरमेंट/रिक्वायरमेंट है/requirement hai/kaam nahi karta/काम नहीं करता/not working"),
            (f"e_{source}__unsure", "uncertain", "पता नहीं/pata nahi/not sure"),
            (f"e_v2_special_{source}_future", "future", "अभी नहीं/abhi nahi/later/बाद में/baad mein"),
            (f"e_v2_special_{source}_dated", "dated", "next week/अगले हफ्ते/agle hafte"),
            (f"e_v2_special_{source}_pending", "pending", "repair pending/part pending/पार्ट pending"),
            (f"else_{source}", "uncertain", "else"),
        ]
        edges.extend({"id": edge_id, "from": source, "to": target, "label": label}
                     for edge_id, target, label in specs)
    return {"id": WORKFLOW_ID, "name": "Haier service regression", "version": 85,
            "nodes": nodes, "edges": edges}


async def replay(definition, text, *, signal="refusal", slots=None):
    with patched(definition, []) as (engine, executor):
        common = {"session_id": "haier-service-test", "tenant_id": "haier-test", "bot_id": "haier-test",
                  "workflow_name": definition["name"], "language": "hi-IN"}
        await engine.handle_turn_detailed(**common, user_text="", reset_state=True, initial_slots=slots)
        result = await engine.handle_turn_detailed(**common, user_text=text, signal=signal)
        assert not executor.calls
        return result


def test_recorded_answer_reproduces_wrong_branch_before_repair():
    result = asyncio.run(replay(fixture(), "नहीं, अभी रिक्वायरमेंट नहीं है।"))
    assert result["trace"][-1] == "n_redispatch"


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize("text", [
    "नहीं, अभी रिक्वायरमेंट नहीं है।",
    "Nahin, abhi requirement nahin hai.",
    "Nahi, requirement nahi hai.",
    "अभी सर्विस नहीं चाहिए।",
    "मुझे service की ज़रूरत नहीं है।",
    "मुझे service nahi chahiye.",
    "AC kaam nahi karta lekin service nahi chahiye.",
    "I do not need service.",
    "Service is not required.",
    "No service needed.",
])
def test_explicit_no_service_never_proposes_redispatch(hub, text):
    definition = apply_service_need_repair(fixture(hub))
    result = asyncio.run(replay(definition, text))
    assert "n_redispatch" not in result["trace"]
    assert result["slots"]["outcome"] == "cancel_no_service"
    assert result["done"]


@pytest.mark.parametrize("hub", HUBS)
@pytest.mark.parametrize(("text", "signal"), [
    ("Haan, requirement hai.", "affirm"),
    ("हाँ, रिक्वायरमेंट है।", "affirm"),
    ("Service chahiye, technician bhejiye.", "affirm"),
    ("AC kaam nahi karta.", "refusal"),
    ("काम नहीं करता।", "refusal"),
    ("अभी नहीं, अगले हफ्ते चाहिए।", "refusal"),
    ("Abhi nahi, baad mein chahiye.", "refusal"),
    ("Service nahi chahiye, next week chahiye.", "refusal"),
    ("Pata nahi, requirement hai ya nahi.", None),
    ("पार्ट pending है।", None),
    ("Service is not required, why?", "question"),
])
def test_other_existing_answers_keep_their_routes(hub, text, signal):
    before = fixture(hub)
    original = asyncio.run(replay(before, text, signal=signal))
    repaired = asyncio.run(replay(apply_service_need_repair(before), text, signal=signal))
    assert repaired["trace"] == original["trace"]
    assert repaired["reply"] == original["reply"]
    assert repaired["slots"] == original["slots"]


def test_old_decline_slot_cannot_redirect_a_later_affirmative_answer():
    result = asyncio.run(replay(apply_service_need_repair(fixture()), "हाँ, service चाहिए।",
                                signal="affirm", slots={VARIABLE: "yes"}))
    assert result["trace"][-1] == "n_redispatch"


def test_transform_is_pure_idempotent_and_survives_json_key_reordering():
    source = fixture()
    original = deepcopy(source)
    repaired = apply_service_need_repair(source)
    assert source == original
    assert apply_service_need_repair(repaired) == repaired
    stored = json.loads(json.dumps(repaired, sort_keys=True))
    assert apply_service_need_repair(stored) == repaired


@pytest.mark.parametrize("changed", ["workflow", "destination", "correction", "capture"])
def test_unreviewed_source_fails_without_partial_changes(changed):
    source = fixture()
    if changed == "workflow":
        source["id"] = "another-workflow"
    elif changed == "destination":
        next(e for e in source["edges"] if e["id"] == "e_service_no")["to"] = "n_redispatch"
    elif changed == "correction":
        source["edges"].append({"id": "other", "from": HUBS[0], "to": "pending", "label": "correction"})
    else:
        next(n for n in source["nodes"] if n["id"] == HUBS[0])["config"]["alsoCapture"] = [{"variable": "other"}]
    original = deepcopy(source)
    with pytest.raises(ValueError):
        apply_service_need_repair(source)
    assert source == original
