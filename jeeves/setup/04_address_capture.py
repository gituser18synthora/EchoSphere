"""Jeeves (bot_44a50e8c4146): clean address capture on the imported v22 flow.

The flow saved by the 2026-10-28 import asks for the customer's correct
address at two ``elseIsAnswer`` hubs (``n_ask_address``, ``n_ask_address_2``,
``captureVariable: new_address``). The engine stored the whole utterance
there, and the final readback repeated the caller's filler as the address
(cv_21df0c56facf). This stage opts the flow into the ``jeeves_address_v1``
semantic-slots provider (shared/orchestration/extensions/jeeves_address),
which copies only the verbatim address components out of the answer, asks
again when the answer holds no place, and falls back to the address-mismatch
branch after the retries.

Patch (applied to the saved nodes/edges, nothing else changes):

  - start node:            ``semanticSlots: "jeeves_address_v1"``
  - both address hubs:     ``semanticCapture: true``, bilingual
                           ``captureClarificationByLanguage``, ``captureMaxRetries: 2``,
                           ``captureFallbackTo: n_mark_address_mismatch`` (the edge the
                           engine takes after the retries — the hubs' own "don't
                           know" edge; its spoken tokens stay as they are)
  - a retraction edge ``n_ask_address`` → ``n_mark_address_ok`` ("wahi sahi
    hai / same address …"): a customer who takes the change back confirms the
    registered address again (address_verified = yes)

Dry run by default; ``--apply`` saves a new workflow version through the
normal API (``PUT /bots/{id}/workflow``); ``--dump`` writes the patched
definition. Refuses a flow without the expected nodes. Needs the runtime
code that understands ``semanticCapture`` (shared/orchestration/
workflow_engine.py, 2026-10-29); on an older runtime the keys are inert and
the raw capture continues.

    env/bin/python jeeves/setup/04_address_capture.py            # dry run against local
    env/bin/python jeeves/setup/04_address_capture.py --apply
Env: ECHOSPHERE_API_BASE (default http://127.0.0.1:9001/api/v1),
     JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin, or a super admin).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

BOT_ID = "bot_44a50e8c4146"
WORKFLOW_ID = "wf_9685ed07e312"
PROVIDER_KEY = "jeeves_address_v1"
ADDRESS_HUBS = ("n_ask_address", "n_ask_address_2")
MISMATCH_MARKER = "n_mark_address_mismatch"
ADDRESS_OK_MARKER = "n_mark_address_ok"
RETRACTION_EDGE_ID = "e_n_ask_address__n_mark_address_ok__retraction"
RETRACTION_TOKENS = (
    "wahi sahi hai/wahi address sahi hai/wahi address/same address/the same address/"
    "that address is correct/registered address sahi hai/purana address hi sahi hai/"
    "purana hi sahi hai/jo aapne bola wahi/jo aapne bataya wahi/aapne jo bola wahi sahi hai/"
    "वही सही है/वही पता सही है/वही एड्रेस सही है/वही एड्रेस/वही पता/पुराना ही सही है/"
    "पुराना पता ही सही है/जो आपने बोला वही/जो आपने बताया वही/रजिस्टर्ड एड्रेस सही है"
)
CLARIFICATION = {
    "hi": "माफ़ कीजिए, मुझे address ठीक से समझ नहीं आया। आप बस address बताइए — area या society, landmark और pincode।",
    "en": "Sorry, I could not catch the address. Please tell me just the address — the area or society, a landmark and the pincode.",
}
MAX_RETRIES = 2


def patch_definition(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """The patched (nodes, edges) and a description of every change; raises
    ValueError when the flow lacks the nodes this stage expects."""
    nodes, edges = copy.deepcopy(nodes), copy.deepcopy(edges)
    by_id = {n.get("id"): n for n in nodes}
    missing = [i for i in (*ADDRESS_HUBS, MISMATCH_MARKER, ADDRESS_OK_MARKER) if i not in by_id]
    if missing:
        raise ValueError(f"workflow lacks the expected nodes: {', '.join(missing)}")
    start = next((n for n in nodes if n.get("kind") == "start"), None)
    if start is None:
        raise ValueError("workflow has no start node")
    changes: list[str] = []
    cfg = start.setdefault("config", {})
    if cfg.get("semanticSlots") != PROVIDER_KEY:
        cfg["semanticSlots"] = PROVIDER_KEY
        changes.append(f"{start['id']}: semanticSlots = {PROVIDER_KEY}")
    for hub_id in ADDRESS_HUBS:
        hub = by_id[hub_id]
        hcfg = hub.setdefault("config", {})
        if hcfg.get("elseIsAnswer") is not True or str(hcfg.get("captureVariable") or "") != "new_address":
            raise ValueError(f"{hub_id} is not the elseIsAnswer/new_address hub this stage expects")
        if not any(e.get("from") == hub_id and e.get("to") == MISMATCH_MARKER for e in edges):
            raise ValueError(f"{hub_id} has no edge to {MISMATCH_MARKER} to fall back on")
        wanted = {"semanticCapture": True, "captureClarificationByLanguage": CLARIFICATION,
                  "captureMaxRetries": MAX_RETRIES, "captureFallbackTo": MISMATCH_MARKER}
        for key, value in wanted.items():
            if hcfg.get(key) != value:
                hcfg[key] = copy.deepcopy(value)
                changes.append(f"{hub_id}: {key} set")
    if not any(e.get("id") == RETRACTION_EDGE_ID for e in edges):
        edges.append({"id": RETRACTION_EDGE_ID, "from": "n_ask_address", "to": ADDRESS_OK_MARKER,
                      "label": RETRACTION_TOKENS})
        changes.append(f"n_ask_address → {ADDRESS_OK_MARKER}: retraction edge added")
    return nodes, edges, changes


def _client(base: str) -> requests.Session:
    email = os.environ.get("JEEVES_SETUP_EMAIL")
    password = os.environ.get("JEEVES_SETUP_PASSWORD")
    if not email or not password:
        sys.exit("set JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin)")
    s = requests.Session()
    r = s.post(f"{base}/auth/login", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"login failed: {r.status_code} {r.text[:300]}")
    s.headers["Authorization"] = "Bearer " + r.json()["data"]["token"]
    s.get, s.put = (lambda p, **k: requests.Session.get(s, base + p, timeout=60, **k),
                    lambda p, **k: requests.Session.put(s, base + p, timeout=60, **k))
    return s


def _check(r, what: str):
    if r.status_code >= 300:
        sys.exit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="save the patched workflow (default: dry run)")
    parser.add_argument("--base", default=os.environ.get("ECHOSPHERE_API_BASE", "http://127.0.0.1:9001/api/v1"))
    parser.add_argument("--definition", type=Path, help="offline: a saved {nodes, edges} JSON to patch")
    parser.add_argument("--dump", type=Path, help="write the patched definition here")
    args = parser.parse_args(argv)

    if args.definition:
        saved = json.loads(args.definition.read_text(encoding="utf-8"))
        nodes, edges = saved.get("nodes") or [], saved.get("edges") or []
        client = None
    else:
        client = _client(args.base)
        wf = _check(client.get(f"/bots/{BOT_ID}/workflow"), f"read workflow of {BOT_ID}")
        if wf.get("id") != WORKFLOW_ID:
            sys.exit(f"unexpected workflow id {wf.get('id')!r}")
        print(f"     {wf.get('id')} v{wf.get('version')} status={wf.get('status')} "
              f"({len(wf.get('nodes') or [])} nodes / {len(wf.get('edges') or [])} edges)")
        nodes, edges = wf.get("nodes") or [], wf.get("edges") or []
    try:
        new_nodes, new_edges, changes = patch_definition(nodes, edges)
    except ValueError as exc:
        sys.exit(f"refusing: {exc}")
    if args.dump:
        args.dump.write_text(json.dumps({"nodes": new_nodes, "edges": new_edges}, ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
        print(f"wrote {args.dump}")
    if not changes:
        print("     workflow already carries the address-capture patch — nothing to change")
        return
    for line in changes:
        print("    ", line)
    if client is None or not args.apply:
        print("dry run — re-run with --apply to save a new workflow version")
        return
    saved = _check(client.put(f"/bots/{BOT_ID}/workflow", json={
        "nodes": new_nodes, "edges": new_edges, "status": "approved",
    }), "save patched workflow")
    print(f"     now v{saved.get('version')} status={saved.get('status')} issues={saved.get('issues')}")


if __name__ == "__main__":
    main()
