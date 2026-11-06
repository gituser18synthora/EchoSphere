"""Pure transform: wire the merchant lookup API into the Wordline workflow.

``upgrade_workflow(definition)`` takes the saved "Paper roll and battery
support" definition (``nodes``/``edges`` as the Studio stores them) and
returns a new definition in which the merchant details are fetched from the
Wordline merchant API instead of being read from the runtime-context test
payload.  ``setup/02_workflow.py`` applies it through the platform API; the
tests run it offline.  It is idempotent: an already-upgraded definition is
returned unchanged.

What changes (and why):

1. The four silent prefill asks that copied ``outlet_name`` / ``bank_name`` /
   ``asset_type`` / ``contact_number`` from the call context into ``ctx_*``
   slots are removed.  The ``api`` node sets mapped slots with ``setdefault``,
   so a value prefilled from the fixture could never be replaced by the
   looked-up one.  ``customer_name`` and ``docket_number`` stay context-fed
   (they are not in the CSV).
2. The read-back nodes speak ``outlet_name`` / ``bank_name`` / ``asset_type``
   / ``contact_number`` — the names the API connection maps into — instead
   of the ``ctx_*`` copies.
3. After each Terminal ID question an ``api`` node calls the connection, a
   condition node checks ``merchant_found``, and three outcomes are handled:
   found → the existing read-back; not found → one re-ask into a *different*
   variable (a same-name re-ask would be skipped as an already-filled slot)
   and a second lookup; still not found → a short apology and the journey
   continues without the read-back; API error / timeout (failure edge) → a
   different apology and the same continuation.  The paper-roll continuation
   is a copy of the options hub without its "I have verified your details"
   sentence; the battery continuation is the battery check itself.

No node outside this list is touched; node ids, edge ids and labels of the
existing graph are preserved so Studio history and replays stay comparable.
"""

from __future__ import annotations

import copy
import json

CONNECTION_NAME = "Wordline Merchant Lookup"
RETRY_VARIABLE = "spoken_terminal_id_retry"
FOUND_VARIABLE = "merchant_found"

PREFILL_NODES = ("n_ctx_outlet_name", "n_ctx_bank_name", "n_ctx_asset_type",
                 "n_ctx_contact_number")
SLOT_RENAMES = {
    "ctx_outlet_name": "outlet_name",
    "ctx_bank_name": "bank_name",
    "ctx_asset_type": "asset_type",
    "ctx_contact_number": "contact_number",
}
MARKER_NODE = "n_api_lookup"

TERMINAL_ENTITY = {"dataType": "text",
                   "regexPattern": r"(?<!\d)(\d(?:[\s-]?\d){3,})(?!\d)"}

# Spoken lines (en + hi) for the new nodes.  Hindi uses the male forms the
# system prompt prescribes and keeps product words in English.
TEXT = {
    "retry_ask": {
        "en": ("Sorry Sir, I could not find a terminal with that Terminal ID. "
               "Please repeat your Terminal ID slowly, one digit at a time."),
        "hi": ("माफ़ कीजिए Sir, इस Terminal ID से कोई terminal नहीं मिला। कृपया अपनी "
               "Terminal ID धीरे-धीरे, एक-एक digit करके दोबारा बताइए।"),
    },
    "retry_reject": {
        "en": ("Sorry Sir, the line may not have been clear. Let me ask again. "
               "Please repeat your Terminal ID slowly, one digit at a time."),
        "hi": ("माफ़ कीजिए Sir, शायद line साफ़ नहीं थी। मैं फिर से पूछता हूँ। कृपया अपनी "
               "Terminal ID धीरे-धीरे, एक-एक digit करके बताइए।"),
    },
    "pr_unverified": {
        "en": ("Sorry Sir, I am unable to locate the terminal details with that "
               "Terminal ID. I can still help you with your paper roll request."),
        "hi": ("माफ़ कीजिए Sir, इस Terminal ID से terminal details नहीं मिल पाईं। फिर भी "
               "मैं आपकी paper roll request में मदद कर सकता हूँ।"),
    },
    "pr_unavailable": {
        "en": ("Sorry Sir, I am unable to verify the terminal details right now. "
               "I can still help you with your paper roll request."),
        "hi": ("माफ़ कीजिए Sir, अभी terminal details verify नहीं हो पा रही हैं। फिर भी "
               "मैं आपकी paper roll request में मदद कर सकता हूँ।"),
    },
    "pr_options": {
        "en": ("Sir, you can request paper rolls either through SMS or our IVR "
               "service. Which option would you like to know more about?"),
        "hi": ("Sir, आप paper roll की request SMS या हमारी IVR service से कर सकते हैं। "
               "आप किस option के बारे में जानना चाहेंगे?"),
    },
    "bat_unverified": {
        "en": ("Sorry Sir, I am unable to locate the terminal details with that "
               "Terminal ID. I can still help you troubleshoot the battery issue."),
        "hi": ("माफ़ कीजिए Sir, इस Terminal ID से terminal details नहीं मिल पाईं। फिर भी "
               "मैं battery की problem troubleshoot करने में आपकी मदद कर सकता हूँ।"),
    },
    "bat_unavailable": {
        "en": ("Sorry Sir, I am unable to verify the terminal details right now. "
               "I can still help you troubleshoot the battery issue."),
        "hi": ("माफ़ कीजिए Sir, अभी terminal details verify नहीं हो पा रही हैं। फिर भी "
               "मैं battery की problem troubleshoot करने में आपकी मदद कर सकता हूँ।"),
    },
}


class UpgradeError(ValueError):
    """The definition is not the Wordline workflow this transform expects."""


# ── node / edge factories ────────────────────────────────────────────────────


def _node(nid: str, kind: str, label: str, config: dict, x: int, y: int) -> dict:
    return {"id": nid, "kind": kind, "label": label, "config": config, "x": x, "y": y}


def _edge(frm: str, to: str, label: str | None = None) -> dict:
    edge = {"id": f"e_{frm}__{to}" + (f"__{label}" if label else ""), "from": frm, "to": to}
    if label:
        edge["label"] = label
    return edge


def _message(nid: str, label: str, text_key: str, x: int, y: int) -> dict:
    text = TEXT[text_key]
    return _node(nid, "message", label, {
        "text": text["en"], "responseMode": "fixed", "textByLanguage": dict(text)}, x, y)


def _api(nid: str, label: str, x: int, y: int) -> dict:
    # ``connection`` is what the engine reads; ``name`` is what the Studio
    # editor shows ("Action name") and the engine's fallback — keep both equal.
    return _node(nid, "api", label, {
        "connection": CONNECTION_NAME, "name": CONNECTION_NAME,
        "onFailure": "skip"}, x, y)


def _found(nid: str, label: str, x: int, y: int) -> dict:
    return _node(nid, "condition", label, {
        "variable": FOUND_VARIABLE, "operator": "equals", "value": "true"}, x, y)


def _retry_ask(nid: str, label: str, x: int, y: int) -> dict:
    ask, reject = TEXT["retry_ask"], TEXT["retry_reject"]
    return _node(nid, "ask", label, {
        "question": ask["en"], "variable": RETRY_VARIABLE,
        "entity": dict(TERMINAL_ENTITY), "responseMode": "fixed",
        "textByLanguage": dict(ask),
        "rejectedAnswerReply": reject["en"],
        "rejectedAnswerReplyByLanguage": dict(reject),
    }, x, y)


# ── transform ────────────────────────────────────────────────────────────────


def is_upgraded(definition: dict) -> bool:
    return any(n.get("id") == MARKER_NODE for n in definition.get("nodes") or [])


def _lookup_block(prefix: str, *, after: str, found_to: str, continue_to: str,
                  unverified_key: str, unavailable_key: str, x: int, y: int,
                  label: str) -> tuple[list[dict], list[dict]]:
    """ask(after) → api → found? → [read-back | retry ask → api → found? →
    [read-back | unverified msg → continue]]; failures → unavailable msg → continue."""
    p = prefix
    nodes = [
        _api(f"{p}api_lookup", f"{label}: merchant lookup API", x, y),
        _found(f"{p}lookup_found", f"{label}: merchant found?", x + 160, y),
        _retry_ask(f"{p}tid_retry", f"{label}: Terminal ID not found — ask again", x + 160, y + 120),
        _api(f"{p}api_lookup_retry", f"{label}: merchant lookup API (retry)", x + 320, y + 120),
        _found(f"{p}lookup_found_retry", f"{label}: merchant found (retry)?", x + 480, y + 120),
        _message(f"{p}lookup_unverified", f"{label}: still not found — continue unverified",
                 unverified_key, x + 480, y + 240),
        _message(f"{p}lookup_unavailable", f"{label}: lookup unavailable — continue unverified",
                 unavailable_key, x + 320, y + 240),
    ]
    edges = [
        _edge(after, f"{p}api_lookup"),
        _edge(f"{p}api_lookup", f"{p}lookup_found", "success"),
        _edge(f"{p}api_lookup", f"{p}lookup_unavailable", "failure"),
        _edge(f"{p}lookup_found", found_to, "true"),
        _edge(f"{p}lookup_found", f"{p}tid_retry", "false"),
        _edge(f"{p}tid_retry", f"{p}api_lookup_retry"),
        _edge(f"{p}api_lookup_retry", f"{p}lookup_found_retry", "success"),
        _edge(f"{p}api_lookup_retry", f"{p}lookup_unavailable", "failure"),
        _edge(f"{p}lookup_found_retry", found_to, "true"),
        _edge(f"{p}lookup_found_retry", f"{p}lookup_unverified", "false"),
        _edge(f"{p}lookup_unverified", continue_to),
        _edge(f"{p}lookup_unavailable", continue_to),
    ]
    return nodes, edges


def upgrade_workflow(definition: dict) -> tuple[dict, list[str]]:
    """Return (upgraded definition, human-readable change list)."""
    if is_upgraded(definition):
        return definition, ["already upgraded (n_api_lookup present) — no change"]
    nodes = copy.deepcopy(list(definition.get("nodes") or []))
    edges = copy.deepcopy(list(definition.get("edges") or []))
    by_id = {n.get("id"): n for n in nodes}
    required = ("n_start", "n_ask_tid", "n_ask_merchant", "n_options", "n_bat_tid",
                "n_bat_details", "n_bat_check", *PREFILL_NODES)
    missing = [nid for nid in required if nid not in by_id]
    if missing:
        raise UpgradeError(f"not the expected Wordline workflow; missing nodes {missing}")
    changes: list[str] = []

    # 1. Drop the context prefill chain for the API-sourced fields and
    #    reconnect what came before it to what came after it.
    prefill = set(PREFILL_NODES)
    into = [e for e in edges if e.get("to") in prefill and e.get("from") not in prefill]
    out = [e for e in edges if e.get("from") in prefill and e.get("to") not in prefill]
    if len(into) != 1 or len(out) != 1:
        raise UpgradeError("prefill chain is not linear (expected one entry and one exit edge)")
    nodes = [n for n in nodes if n.get("id") not in prefill]
    edges = [e for e in edges if e.get("from") not in prefill and e.get("to") not in prefill]
    edges.insert(0, _edge(str(into[0]["from"]), str(out[0]["to"])))
    changes.append(f"removed prefill asks {list(PREFILL_NODES)}; "
                   f"linked {into[0]['from']} → {out[0]['to']}")

    # 2. Read-backs speak the API-mapped slot names.
    renamed = 0
    for node in nodes:
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        raw = json.dumps(config, ensure_ascii=False)
        new = raw
        for old, fresh in SLOT_RENAMES.items():
            new = new.replace(f'"{old}"', f'"{fresh}"')
        if new != raw:
            node["config"] = json.loads(new)
            renamed += 1
    changes.append(f"renamed ctx_* read-back variables in {renamed} node(s)")

    # 3. Lookup blocks after both Terminal ID questions.
    def drop_edge(frm: str, to: str) -> None:
        before = len(edges)
        edges[:] = [e for e in edges if not (e.get("from") == frm and e.get("to") == to)]
        if len(edges) != before - 1:
            raise UpgradeError(f"expected exactly one edge {frm} → {to}")

    drop_edge("n_ask_tid", "n_ask_merchant")
    drop_edge("n_bat_tid", "n_bat_details")

    options = by_id["n_options"]
    unverified_options = copy.deepcopy(options)
    unverified_options.update({
        "id": "n_options_unverified",
        "label": "Unverified — SMS, IVR, or we raise it?",
        "x": int(options.get("x", 0)), "y": int(options.get("y", 0)) + 360,
    })
    cfg = unverified_options.setdefault("config", {})
    cfg["prompt"] = TEXT["pr_options"]["en"]
    if isinstance(cfg.get("textByLanguage"), dict):
        cfg["textByLanguage"] = {**cfg["textByLanguage"], **TEXT["pr_options"]}
    option_edges = []
    for edge in edges:
        if edge.get("from") == "n_options":
            clone = copy.deepcopy(edge)
            clone["id"] = f"{edge['id']}__unverified"
            clone["from"] = "n_options_unverified"
            option_edges.append(clone)

    ask_tid, bat_tid = by_id["n_ask_tid"], by_id["n_bat_tid"]
    pr_nodes, pr_edges = _lookup_block(
        "n_", after="n_ask_tid", found_to="n_ask_merchant", continue_to="n_options_unverified",
        unverified_key="pr_unverified", unavailable_key="pr_unavailable",
        x=int(ask_tid.get("x", 0)) + 100, y=int(ask_tid.get("y", 0)) + 360, label="Paper roll")
    bat_nodes, bat_edges = _lookup_block(
        "n_bat_", after="n_bat_tid", found_to="n_bat_details", continue_to="n_bat_check",
        unverified_key="bat_unverified", unavailable_key="bat_unavailable",
        x=int(bat_tid.get("x", 0)) + 100, y=int(bat_tid.get("y", 0)) + 400, label="Battery")

    nodes.extend([*pr_nodes, unverified_options, *bat_nodes])
    edges.extend([*pr_edges, *option_edges, *bat_edges])
    changes.append(f"paper roll: n_ask_tid → {CONNECTION_NAME!r} lookup → found?/retry/"
                   f"unverified/unavailable → n_ask_merchant | n_options_unverified "
                   f"({len(option_edges)} option edges cloned)")
    changes.append(f"battery: n_bat_tid → {CONNECTION_NAME!r} lookup → found?/retry/"
                   f"unverified/unavailable → n_bat_details | n_bat_check")

    upgraded = {**definition, "nodes": nodes, "edges": edges}
    _validate(upgraded)
    return upgraded, changes


def _validate(definition: dict) -> None:
    ids = [n.get("id") for n in definition["nodes"]]
    if len(ids) != len(set(ids)):
        raise UpgradeError("duplicate node ids after upgrade")
    known = set(ids)
    edge_ids = [e.get("id") for e in definition["edges"]]
    if len(edge_ids) != len(set(edge_ids)):
        raise UpgradeError("duplicate edge ids after upgrade")
    for edge in definition["edges"]:
        if edge.get("from") not in known or edge.get("to") not in known:
            raise UpgradeError(f"dangling edge {edge.get('id')}")
    for node in definition["nodes"]:
        if node.get("kind") == "api" and node["config"].get("connection") != CONNECTION_NAME:
            raise UpgradeError(f"api node {node['id']} is not wired to {CONNECTION_NAME!r}")
