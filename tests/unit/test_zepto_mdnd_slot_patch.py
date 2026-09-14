"""Scope/idempotence checks for the narrow MDND deployment patch."""

from copy import deepcopy
import runpy

import pytest


@pytest.fixture()
def patcher():
    return runpy.run_path("zepto/setup/10_mdnd_persistent_slots.py")


def test_workflow_patch_preserves_unrelated_nodes_and_current_settings(patcher):
    nodes, edges = patcher["stage06"].build_mdnd_workflow()
    current = {"name": "Custom MDND name", "status": "approved", "nodes": nodes, "edges": edges}
    lookup = {node["id"]: node for node in nodes}
    lookup["n_start"]["config"] = {"customSetting": "keep"}
    lookup["n_api"]["config"]["connection"] = "Live approved MDND connection"
    lookup["n_msg_close"]["config"]["text"] = "Custom approved closing"
    lookup["n_ask_handover"]["x"] = 1234
    lookup["n_hub_verify"]["config"]["responseDirectiveVariants"] = [{"heard": ["readout"], "directive": "Keep this"}]
    for edge in edges:
        if edge["from"] == "n_ask_handover":
            edge["to"] = "n_cond_guard"
    edges[:] = [edge for edge in edges
                if not (edge["from"] == "n_hub_verify" and edge.get("label") == "correction")]
    snapshot = deepcopy(current)

    updated = patcher["patch_workflow"](current)
    assert current == snapshot  # preview must not mutate the supplied snapshot
    after = {node["id"]: node for node in updated["nodes"]}
    assert after["n_start"]["config"] == {"customSetting": "keep", "semanticSlots": "mdnd_v1"}
    assert after["n_api"] == lookup["n_api"]
    assert after["n_msg_close"] == lookup["n_msg_close"]
    assert after["n_hub_verify"]["config"]["responseDirectiveVariants"] == lookup["n_hub_verify"]["config"]["responseDirectiveVariants"]
    assert after["n_ask_handover"]["x"] == 1234
    assert updated["name"] == current["name"] and updated["status"] == current["status"]
    assert [edge["to"] for edge in updated["edges"] if edge["from"] == "n_ask_handover"] == ["n_ask_cx"]
    assert patcher["patch_workflow"](updated) == updated


def test_prompt_patch_preserves_identity_and_unrelated_rules(patcher):
    current = patcher["stage06"].MDND_SYSTEM.replace(
        "# Identity\n", "# Identity\nCustom approved persona goes here.\n")
    current += "\n# Custom approved rule\nPreserve this unrelated setting.\n"
    current = current.replace(
        "## Unclear speech and retries", "## Before the workflow starts\n"
        'Say "जी, आपके ticket की details देख रहा हूँ, एक मिनट दीजिए।"\n\n'
        "## Unclear speech and retries")
    updated = patcher["patch_system"](current)
    assert "Custom approved persona goes here." in updated
    assert "Preserve this unrelated setting." in updated
    assert "ticket की details देख रहा" not in updated
    assert "Never individually reconfirm" in updated
    assert patcher["patch_system"](updated) == updated


def test_patch_refuses_an_unrecognized_workflow_or_system_shape(patcher):
    with pytest.raises(ValueError, match="missing expected nodes"):
        patcher["patch_workflow"]({"name": "Different workflow", "status": "approved", "nodes": [], "edges": []})
    with pytest.raises(ValueError, match="missing expected section"):
        patcher["patch_system"]("Entirely unrelated prompt")


def test_mdnd_only_has_contextual_bilingual_collection_retries(patcher):
    stage06 = patcher["stage06"]
    nodes, edges = stage06.build_mdnd_workflow()
    lookup = {node["id"]: node for node in nodes}
    reached = {"n_start"}
    while targets := {edge["to"] for edge in edges if edge["from"] in reached} - reached:
        reached |= targets
    assert not {"n_ask_guard_name_known", "n_ask_guard_name"} & reached
    for node_id, term in {
        "n_ask_issue_desc": "delivery", "n_ask_called": "call",
        "n_ask_reached": "location", "n_ask_handover": "order", "n_ask_cx": "CX support",
    }.items():
        config = lookup[node_id]["config"]
        assert "माफ़ कीजिए" in config["unmatchedReply"]
        assert term in config["unmatchedReply"]
        assert term in config["unmatchedReplyByLanguage"]["en"]
    assert lookup["n_msg_empathy"]["config"]["text"] == "मैं आपकी बात समझ सकता हूँ।"
    assert "ticket की details देख रहा" not in stage06.MDND_SYSTEM
    assert "ask it as a short confirmation" not in stage06.MDND_SYSTEM
