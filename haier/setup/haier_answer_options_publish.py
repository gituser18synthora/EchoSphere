"""Save the answer-options workflow revision through an environment's own API
and release it. Shared by ``09_answer_options_local.py`` (LOCAL) and
``10_answer_options_demo.py`` (DEMO); each of those fixes its destination.

Two writes, both through the API (which snapshots the revision and audits the
change): the next workflow revision, and one release that pins it. Prompts,
the greeting and the summary policy are not touched. No server file is copied
and no service is restarted.
"""
from __future__ import annotations

import hashlib
import json

from haier.setup.haier_answer_options_repair import (
    MARKER, VERSION, apply_answer_options_repair, hubs_with_options,
)

BOT = "bot_450a4f7b4832"
TENANT = "tn_fb39b67f5361"
WORKFLOW = "wf_1c3e59362c51"
# The flow the repair was written for and tested on, by what it DOES (node
# kinds and config, edges) and not by where its nodes sit on the canvas:
# LOCAL v96 and DEMO v85 on 2026-11-26. Any other flow needs a new review.
REVIEWED_LOGIC = "22a45801c7ef72c9e273"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def logic_digest(nodes: list[dict], edges: list[dict]) -> str:
    core = sorted(
        ({"id": n.get("id"), "kind": n.get("kind"), "config": n.get("config") or {}} for n in nodes),
        key=lambda node: str(node["id"]),
    )
    links = sorted(
        ({"from": e.get("from"), "to": e.get("to"), "label": e.get("label")} for e in edges),
        key=lambda edge: json.dumps(edge, sort_keys=True, ensure_ascii=False),
    )
    return hashlib.sha256(
        json.dumps([core, links], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


def next_release(version: str) -> str:
    head, _, patch = (version or "v0.1.0").rpartition(".")
    return f"{head}.{int(patch) + 1}"


def publish(common, client, *, apply: bool, environment: str, validation: str) -> dict | None:
    """Plan (and with ``apply`` perform) the two writes. Returns what was
    published, or None for a dry-run / nothing to do."""
    bot = common.check(client.get(f"/bots/{BOT}"), "read bot")
    if bot.get("tenantId") not in (None, TENANT):
        raise SystemExit(f"Unexpected tenant {bot.get('tenantId')}")
    current = common.check(client.get(f"/bots/{BOT}/workflow"), "read workflow")
    if current["id"] != WORKFLOW:
        raise SystemExit(f"Unexpected workflow {current['id']}")
    candidate = apply_answer_options_repair(
        {"id": current["id"], "name": current["name"], "nodes": current["nodes"], "edges": current["edges"]})
    hubs = hubs_with_options(candidate)
    already = digest(candidate["nodes"]) == digest(current["nodes"]) and all(
        (node.get("config") or {}).get(MARKER) == VERSION
        for node in current["nodes"] if node.get("kind") == "intent" and node["id"] in hubs)
    start = next(n for n in candidate["nodes"] if n.get("kind") == "start")["config"]
    print(f"{environment}: workflow {current['id']} v{current['version']} ({digest(current['nodes'])}); "
          f"live release {bot.get('liveVersion')}; {len(hubs)} questions get answer options "
          f"({sum(len(o) for o in hubs.values())} options); edges unchanged: "
          f"{digest(candidate['edges']) == digest(current['edges'])}")
    print(f"start node: {json.dumps({k: start[k] for k in ('behavior', 'closingFollowups', 'doNotCallNode') if k in start})}")
    # A release created by this script names the revision in its notes. The
    # revision can be saved without one: the API stores it and then fails to
    # answer when its process predates the runtime code (it reported HTTP 500
    # after the commit on LOCAL, 2026-11-26). The release is then all that is
    # left to do.
    releases = common.check(client.get(f"/bots/{BOT}/releases"), "read releases") or []
    released = any(
        item.get("stage") == "published" and f"Workflow v{current['version']}:" in (item.get("notes") or "")
        for item in releases)
    if already and released:
        print("Nothing to do: the saved workflow already carries this revision and a release pins it.")
        return None
    reviewed = logic_digest(current["nodes"], current["edges"])
    if not already and reviewed != REVIEWED_LOGIC:
        # A flow that already carries an earlier state of this repair differs
        # from the reviewed source only in what the repair itself writes.
        raise SystemExit(
            f"The saved workflow ({reviewed}) is not the reviewed one ({REVIEWED_LOGIC}): it changed since the "
            "repair was tested. Export it, re-run the QA suites against it and update REVIEWED_LOGIC.")
    release = next_release(bot.get("liveVersion") or "v0.1.0")
    target = current["version"] if already else current["version"] + 1
    before = current["version"] - 1 if already else current["version"]
    if already:
        print(f"\nThe revision v{target} is already saved but no release pins it: only the release is created.")
    else:
        print(f"\nWRITE 1  PUT /bots/{BOT}/workflow  -> revision v{target} "
              f"(nodes {digest(candidate['nodes'])}; node config only: answerOptions, answerAgreementWords, "
              f"{MARKER} on the questions; closingFollowups, doNotCallNode and behavior.max_node_steps on "
              "the start node; no spoken text, no edge)")
    print(f"WRITE {'1' if already else '2'}  POST /bots/{BOT}/releases  -> {release}, then approve + publish "
          f"(pins {current['id']} v{target}; prompts and summary policy unchanged)")
    if not apply:
        print(f"\nDry-run: nothing was sent. Re-run with --apply to write to the {environment} database.")
        return None
    if already:
        saved = current
    else:
        saved = common.check(client.put(f"/bots/{BOT}/workflow", json={
            "name": current["name"], "nodes": candidate["nodes"], "edges": candidate["edges"],
            "status": "approved",
        }), "save workflow revision")
        if saved["version"] != target or digest(saved["nodes"]) != digest(candidate["nodes"]):
            raise SystemExit("The saved workflow differs from the candidate; nothing was released")
    created = common.check(client.post(f"/bots/{BOT}/releases", json={
        "version": release,
        "notes": (f"Workflow v{saved['version']}: answer options on {len(hubs)} questions, bounded closing, "
                  "own stop-calling closing, node budget 120 (2026-11-25 call review, phase 1). Prompts and "
                  "summary policy unchanged. Needs the matching runtime (workflow engine answerOptions)."),
        "diff": [{"component": "workflow", "before": before, "after": saved["version"]}],
    }), "create release")
    approved = common.check(client.patch(f"/releases/{created['id']}", json={
        "stage": "approved", "note": validation,
    }), "approve release")
    body = {"stage": "published", "note": f"{environment}."}
    if [item for item in approved.get("checklist") or [] if not item.get("ok")]:
        body["overrideReason"] = ("Fixed-workflow bot: no knowledge base and no Studio scenarios; "
                                  "validated by haier/qa step cases, call replays, the text suite and voice cases.")
    published = common.check(client.patch(f"/releases/{created['id']}", json=body), "publish release")
    result = {"environment": environment, "workflow_version": saved["version"],
              "previous_workflow_version": before, "release": published.get("version"),
              "previous_release": bot.get("liveVersion"), "stage": published.get("stage")}
    print(json.dumps(result))
    return result
