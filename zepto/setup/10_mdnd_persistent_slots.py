"""Apply the MDND four-slot collection fix without reconfiguring the bot.

Default is a read-only diff; pass --apply to update the current MDND workflow
and publish the targeted system-prompt changes. Preserves greeting, intents,
context, voice/goal settings, API connections, other bots and unrelated nodes.

Run: env/bin/python zepto/setup/10_mdnd_persistent_slots.py [--apply]
"""

import argparse
from copy import deepcopy
import difflib
import importlib.util
import json
from pathlib import Path
import re


_spec = importlib.util.spec_from_file_location(
    "mdnd_stage06", Path(__file__).with_name("06_single_bots.py"))
stage06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stage06)


def patch_workflow(current: dict) -> dict:
    """Patch selected config keys and edges on the current stored definition."""
    result = {key: deepcopy(current[key]) for key in ("name", "nodes", "edges", "status")}
    authored, _edges = stage06.build_mdnd_workflow()
    wanted = {node["id"]: node for node in authored}
    nodes = {node["id"]: node for node in result["nodes"]}
    required = {"n_start", "n_msg_empathy", "n_ask_handover", "n_ask_cx",
                "n_hub_verify", "n_ask_correction"}
    required.update(node["id"] for node in authored
                    if "unmatchedReplyByLanguage" in node.get("config", {}))
    if missing := required - nodes.keys():
        raise ValueError(f"MDND definition is missing expected nodes: {sorted(missing)}")
    nodes["n_start"].setdefault("config", {})["semanticSlots"] = "mdnd_v1"
    nodes["n_msg_empathy"].setdefault("config", {})["text"] = (
        wanted["n_msg_empathy"]["config"]["text"])
    for node_id in required:
        config = wanted[node_id].get("config", {})
        for key in ("unmatchedReply", "unmatchedReplyByLanguage"):
            if key in config:
                nodes[node_id].setdefault("config", {})[key] = deepcopy(config[key])
    handover_edges = [edge for edge in result["edges"] if edge["from"] == "n_ask_handover"]
    if len(handover_edges) != 1:
        raise ValueError("Expected one outgoing MDND handover edge; refusing to replace custom branching")
    handover_edges[0]["to"] = "n_ask_cx"
    if not any(edge["from"] == "n_hub_verify" and edge.get("label") == "correction"
               for edge in result["edges"]):
        result["edges"].append({
            "id": "e_mdnd_verify_correction", "from": "n_hub_verify",
            "to": "n_ask_correction", "label": "correction",
        })
    return result


def _section(text: str, heading: str) -> str:
    match = re.search(r"(?m)^" + re.escape(heading) + r"\n.*?(?=^# |\Z)", text, re.S)
    if match is None:
        raise ValueError(f"System prompt is missing expected section {heading!r}")
    return match.group(0)


def patch_system(text: str) -> str:
    """Replace only collection instructions, retaining persona and other rules."""
    for heading in ("# Division of Work — CRITICAL", "# Instruction vs Actual Handover",
                    "# Verification Node"):
        text = text.replace(_section(text, heading), _section(stage06.MDND_SYSTEM, heading), 1)
    text = text.replace("मैं आपकी परेशानी पूरी तरह समझ सकता हूँ।", "मैं आपकी बात समझ सकता हूँ।")
    text = text.replace("मैं आपकी परेशानी पूरी तरह समझ सकती हूँ।", "मैं आपकी बात समझ सकती हूँ।")
    old_empathy = "* Acknowledge the problem at most once in the whole call, and only if the workflow has not already played its empathy line. Never stack sympathy phrases."
    new_empathy = next(line for line in stage06.MDND_SYSTEM.splitlines()
                       if line.startswith("* Acknowledge the problem at most once"))
    return text.replace(old_empathy, new_empathy)


def _diff(before: str, after: str, label: str) -> None:
    print("".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"current/{label}", tofile=f"updated/{label}")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the displayed changes")
    args = parser.parse_args()
    state = stage06.load_state()
    bot_id = state["BOT_MDND"]
    with stage06.client() as client:
        workflow = stage06.check(client.get(f"/bots/{bot_id}/workflow"), "read MDND workflow")
        current = {key: workflow[key] for key in ("name", "nodes", "edges", "status")}
        updated = patch_workflow(current)
        prompts = stage06.check(client.get(f"/bots/{bot_id}/prompts"), "read MDND prompts")
        system = next(prompt for prompt in prompts if prompt["type"] == "system")
        version_no = system.get("publishedVersion") or system.get("activeVersion")
        version = next(item for item in system["versions"] if item["version"] == version_no)
        system_before = version.get("fullPrompt") or ""
        system_after = patch_system(system_before)
        _diff(json.dumps(current, ensure_ascii=False, indent=2) + "\n",
              json.dumps(updated, ensure_ascii=False, indent=2) + "\n", "workflow.json")
        _diff(system_before, system_after, "system_prompt.md")
        if not args.apply:
            print("Read-only preview complete; use --apply to apply these MDND-only changes.")
            return
        if updated != current:
            stage06.check(client.put(f"/bots/{bot_id}/workflow", json=updated), "update MDND collection")
        if system_after != system_before:
            stage06.check(client.post(f"/prompts/{system['id']}/versions", json={
                "promptMode": "full", "fullPrompt": system_after,
                "note": "MDND persistent four-slot collection, contextual retries and empathy wording",
            }), "create MDND system version")
            stage06.check(client.patch(f"/prompts/{system['id']}", json={"state": "approved"}),
                          "approve MDND system")
            stage06.check(client.patch(f"/prompts/{system['id']}", json={"state": "published"}),
                          "publish MDND system")
        print(f"MDND-only collection fix applied to {bot_id}.")


if __name__ == "__main__":
    main()
