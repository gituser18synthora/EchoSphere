"""Opt the Zepto MDND workflow into engine behaviour v2 through the authoring API.

Behaviour v2 (shared/orchestration/behavior.py): a 'question'-labelled
statement of three or more words at a free-text ask is stored as the answer
instead of parking the flow off-script (live call cv_7786bc42deca: the
partner's incident narrative was re-asked twice). The declaration lives on
the definition's start node — ``config.behavior = {"version": 2}`` — so no
other tenant or workflow changes.

Preview by default (prints the exact start-node diff and the version bump);
``--apply`` PUTs the workflow (nodes/edges otherwise byte-identical), which
bumps the workflow version and, once migration a1b2c3d4e5f6 is applied,
stores a revision snapshot. Follow with a Redis bot-config invalidation
(``redis-cli --scan --pattern "botcfg:*<bot>*" | xargs redis-cli del``); the
engine's own definition cache expires within 30 s.

Run: env/bin/python zepto/setup/12_mdnd_behavior_v2.py [--apply]
"""
import argparse
import copy
import importlib.util
import json
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

_spec = importlib.util.spec_from_file_location(
    "mdnd_stage06", Path(__file__).with_name("06_single_bots.py"))
stage06 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stage06)

BEHAVIOR_VERSION = 2


def patched_nodes(nodes: list) -> list:
    out = copy.deepcopy(nodes)
    starts = [n for n in out if n.get("kind") == "start"]
    if len(starts) != 1:
        raise ValueError("Expected exactly one start node")
    config = starts[0].setdefault("config", {})
    declared = config.get("behavior") if isinstance(config.get("behavior"), dict) else {}
    config["behavior"] = {**declared, "version": BEHAVIOR_VERSION}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if urlparse(stage06.BASE).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("This patch requires the local authoring API")
    state = stage06.load_state()
    bot_id, wf_id = state["BOT_MDND"], state["BOT_MDND_WF"]
    with closing(stage06.client()) as client:
        wf = stage06.check(client.get(f"/bots/{bot_id}/workflow"), "read workflow")
        if wf["id"] != wf_id:
            raise ValueError(f"Workflow id drift: state says {wf_id}, API returned {wf['id']}")
        before = next(n for n in wf["nodes"] if n.get("kind") == "start")
        nodes = patched_nodes(wf["nodes"])
        after = next(n for n in nodes if n.get("kind") == "start")
        print(f"bot {bot_id} · workflow {wf_id} v{wf['version']} ({wf['status']}) · "
              f"behaviorVersion now {wf.get('behaviorVersion', 1)}")
        print("start node BEFORE:", json.dumps(before.get("config"), ensure_ascii=False))
        print("start node AFTER: ", json.dumps(after.get("config"), ensure_ascii=False))
        changed = [n["id"] for n, m in zip(wf["nodes"], nodes) if n != m]
        print("nodes changed:", changed, "| edges changed: 0")
        if not args.apply:
            print(f"preview only — would save as v{wf['version'] + 1}; rerun with --apply")
            return
        saved = stage06.check(client.put(f"/bots/{bot_id}/workflow", json={
            "name": wf["name"], "nodes": nodes, "edges": wf["edges"], "status": wf["status"],
        }), "save workflow")
        print(f"saved v{saved['version']} behaviorVersion={saved.get('behaviorVersion')}")


if __name__ == "__main__":
    main()
