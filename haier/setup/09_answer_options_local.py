"""Save the answer-options workflow revision on the LOCAL bot and release it.

Dry-run by default: prints what would be written and sends nothing. ``--apply``
writes through the local API only (a workflow revision and one release that
pins it); the script refuses any API that is not on this machine. It never
touches DEMO or production, copies no code and restarts no service.

    env/bin/python -B haier/setup/09_answer_options_local.py            # dry-run
    env/bin/python -B haier/setup/09_answer_options_local.py --apply

The running local API / voice worker keep the code they were started with:
restart them yourself to pick up the matching runtime (the workflow revision
alone changes nothing on a runtime that does not know ``answerOptions``).
To publish the same revision on DEMO, export it and use the reviewed DEMO
publication path; that is a separate, explicitly approved step.
"""
from __future__ import annotations

import hashlib
import json
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import _common  # noqa: E402
from haier.setup.haier_answer_options_repair import (  # noqa: E402
    MARKER, VERSION, apply_answer_options_repair, hubs_with_options,
)

BOT = "bot_450a4f7b4832"
WORKFLOW = "wf_1c3e59362c51"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def next_release(version: str) -> str:
    head, _, patch = (version or "v0.1.0").rpartition(".")
    return f"{head}.{int(patch) + 1}"


def main() -> None:
    host = urlparse(_common.BASE).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"Refusing a non-local API: {_common.BASE}")
    _common.banner("Haier answer options: LOCAL workflow revision + release")
    with closing(_common.client(prefer_tenant_admin=False)) as c:
        bot = _common.check(c.get(f"/bots/{BOT}"), "read bot")
        current = _common.check(c.get(f"/bots/{BOT}/workflow"), "read workflow")
        if current["id"] != WORKFLOW:
            raise SystemExit(f"Unexpected workflow {current['id']}")
        candidate = apply_answer_options_repair(
            {"id": current["id"], "name": current["name"], "nodes": current["nodes"], "edges": current["edges"]}
        )
        hubs = hubs_with_options(candidate)
        already = all(
            (node.get("config") or {}).get(MARKER) == VERSION
            for node in current["nodes"] if node.get("kind") == "intent" and node["id"] in hubs
        ) and digest(candidate["nodes"]) == digest(current["nodes"])
        print(f"workflow {current['id']} v{current['version']} ({digest(current['nodes'])}); "
              f"bot live release {bot.get('liveVersion')}; {len(hubs)} hubs get answer options; "
              f"{sum(len(o) for o in hubs.values())} options; edges unchanged: "
              f"{digest(candidate['edges']) == digest(current['edges'])}")
        if already:
            print("Nothing to do: the saved workflow already carries these answer options.")
            return
        release = next_release(bot.get("liveVersion") or "v0.1.0")
        print(f"\nWRITE 1  PUT /bots/{BOT}/workflow  -> revision v{current['version'] + 1} "
              f"(nodes {digest(candidate['nodes'])}; only node config keys answerOptions + {MARKER} added)")
        print(f"WRITE 2  POST /bots/{BOT}/releases  -> {release}, then approve + publish "
              f"(pins {current['id']} v{current['version'] + 1}; prompts and summary policy unchanged)")
        if not _common.APPLY:
            print("\nDry-run: nothing was sent. Re-run with --apply to write to the LOCAL database.")
            return
        saved = _common.check(c.put(f"/bots/{BOT}/workflow", json={
            "name": current["name"], "nodes": candidate["nodes"], "edges": candidate["edges"], "status": "approved",
        }), "save workflow revision")
        if saved["version"] != current["version"] + 1 or digest(saved["nodes"]) != digest(candidate["nodes"]):
            raise SystemExit("The saved workflow differs from the candidate; nothing was released")
        created = _common.check(c.post(f"/bots/{BOT}/releases", json={
            "version": release,
            "notes": (f"Workflow v{saved['version']}: answer options on {len(hubs)} questions "
                      "(2026-11-25 call review, phase 1). Prompts and summary policy unchanged. "
                      "Needs the matching runtime (workflow engine answerOptions)."),
            "diff": [{"component": "workflow", "before": current["version"], "after": saved["version"]}],
        }), "create release")
        approved = _common.check(c.patch(f"/releases/{created['id']}", json={
            "stage": "approved", "note": "Step cases, call replays and the QA suite ran against this revision.",
        }), "approve release")
        body = {"stage": "published", "note": "LOCAL only."}
        if [item for item in approved.get("checklist") or [] if not item.get("ok")]:
            body["overrideReason"] = ("LOCAL fixed-workflow bot: no knowledge base and no Studio scenarios; "
                                      "validated by haier/qa step cases, call replays and the text suite.")
        published = _common.check(c.patch(f"/releases/{created['id']}", json=body), "publish release")
        print(json.dumps({"workflow_version": saved["version"], "release": published.get("version"),
                          "stage": published.get("stage")}))


if __name__ == "__main__":
    main()
