"""Pure transforms: look up the caller's "MID and outlet name" answer.

The Wordline team's workflow v57 (saved on LOCAL 2026-11-26 20:10 IST by
admin@worldline.com) already asks a caller whose Terminal ID was not found,
or who wants to give the MID instead, for "your MID, that is your Merchant
ID, and your outlet name" (``n_pr_merchant_outlet`` /
``n_bat_merchant_outlet``) — and then only notes the answer and continues
unverified (``n_pr_merchant_noted`` → ``n_options_unverified``,
``n_bat_merchant_noted`` → ``n_bat_switch_off``).  The merchant API can now
resolve that answer (Merchant ID exact, outlet name partial; see
``api/main.py``), so ``upgrade_alternate_lookup`` puts a lookup between the
answer and the "noted" step, per journey:

    "MID and outlet name" answer
      → lookup → found      → the journey's existing record read-back
                              (n_has_ask_merchant / n_has_bat_details: the
                              caller confirms outlet, bank, model as for a TID)
               → several    → "I found N merchants with that name, such as A,
                              B, C. Please tell me the complete registered
                              outlet name, or your MID." → lookup again
                              → found → read-back; otherwise → "noted"
               → not found  → "I could not find a merchant with these details.
                              Please tell me your MID and outlet name once
                              more, slowly." → lookup again → found →
                              read-back; otherwise the existing "noted" step
                              (unverified, as today)
               → service down → the existing "noted" step
    caller has no Terminal ID at the FIRST question ("TID nahi pata")
      → was: n_tid_none_close (end); now: "No problem, Sir. I can also verify
        your details through your MID and outlet name." → the same question.

Why the shape:

* The team's questions, wording, retry ladder and unverified continuation
  stay exactly as they are; only the edge "answer → noted" (one per journey)
  and the first-question "no Terminal ID → close" edge (one per journey) get
  new targets.  The "no Terminal ID" change is the one behaviour change the
  team did not ask for themselves: without it a caller who has no Terminal ID
  could never use the new lookup (v57 offers MID only after a failed TID).
* One repeat on a miss: the team's question is a free-text ask, so a caller
  who pauses inside a 12-15 character MID is cut at the pause and the
  first part alone is looked up.  The second answer goes to the same lookup.
* A found record lands on the node the Terminal ID path uses, so nothing is
  treated as verified before the caller confirms the read-back.
* Several matches never pick one: the API answers ``multiple`` with no
  merchant fields, the connection maps only ``merchant_ambiguous`` /
  ``merchant_match_count`` / ``merchant_candidates``, and the bot reads at
  most three names.  The engine maps with ``setdefault`` (a slot keeps its
  first value for the call), so the ambiguity condition sits ONLY after the
  first "MID and outlet" lookup — the only earlier lookups are Terminal ID
  lookups, which never answer ``multiple`` — and the second answer is stored
  in a new variable (``<pr|bat>_merchant_outlet_retry``).
* ``ctx_distance``: the connection maps the record's ``distance`` into the
  slot the team's visit-timing conditions read, so it needs no node here.

``upgrade_prompt_alternate_lookup`` makes the matching exact-match edits to
the system prompt.  Both transforms are idempotent and refuse
(``UpgradeError``) a definition whose anchors moved.  Applied by
``setup/05_alternate_lookup.py``.
"""

from __future__ import annotations

import copy

from tenant.Wordline.setup.workflow_upgrade import (
    CONNECTION_NAME, FOUND_VARIABLE, UpgradeError, _edge, _node, _validate)

MARKER_NODE = "n_pr_mo_api"
AMBIGUOUS_VARIABLE = "merchant_ambiguous"

# Per journey: the team's nodes this step hooks into.
JOURNEYS = {
    "paper_roll": {
        "prefix": "n_pr", "ask": "n_pr_merchant_outlet", "noted": "n_pr_merchant_noted",
        "variable": "pr_merchant_outlet", "found": "n_has_ask_merchant",
        "no_tid": ("n_ask_tid_given", "true", "n_tid_none_close"),
    },
    "battery": {
        "prefix": "n_bat", "ask": "n_bat_merchant_outlet", "noted": "n_bat_merchant_noted",
        "variable": "bat_merchant_outlet", "found": "n_has_bat_details",
        "no_tid": ("n_bat_tid_given", "true", "n_tid_none_close"),
    },
}

TEXT = {
    "no_tid_bridge": {
        "en": "No problem, Sir. I can also verify your details through your MID and outlet name.",
        "hi": "कोई बात नहीं Sir। मैं आपकी details आपके MID और outlet के नाम से भी verify कर सकता "
              "हूँ।",
    },
    "pick_found": {
        "en": "Sir, I found {merchant_match_count} merchants with that name, such as "
              "{merchant_candidates}.",
        "hi": "Sir, इस नाम से मुझे {merchant_match_count} merchants मिले, जैसे "
              "{merchant_candidates}।",
    },
    "pick_question": {
        "en": "Please tell me the complete registered outlet name, or your MID.",
        "hi": "कृपया अपने outlet का पूरा registered नाम, या अपना MID बताइए।",
    },
    "retry": {
        "en": "Sorry Sir, I could not find a merchant with these details. Please tell me your MID "
              "and outlet name once more, slowly.",
        "hi": "माफ़ कीजिए Sir, इन details से कोई merchant नहीं मिला। कृपया अपना MID और outlet का "
              "नाम एक बार फिर धीरे-धीरे बताइए।",
    },
    "pick_plain": {
        "en": "Sir, I found more than one merchant with that name. Please tell me the complete "
              "registered outlet name, or your MID.",
        "hi": "Sir, इस नाम से मुझे एक से ज़्यादा merchants मिले। कृपया अपने outlet का पूरा "
              "registered नाम, या अपना MID बताइए।",
    },
}


def _message(nid: str, label: str, text_key: str, x: int, y: int) -> dict:
    text = TEXT[text_key]
    return _node(nid, "message", label, {"text": text["en"], "responseMode": "fixed",
                                         "textByLanguage": dict(text)}, x, y)


def _cond(nid: str, label: str, variable: str, value: str, x: int, y: int) -> dict:
    return _node(nid, "condition", label,
                 {"variable": variable, "operator": "equals", "value": value}, x, y)


def _lookup(nid: str, label: str, x: int, y: int) -> dict:
    return _node(nid, "api", label, {"connection": CONNECTION_NAME, "name": CONNECTION_NAME,
                                     "onFailure": "skip"}, x, y)


def _pick_readback() -> dict:
    """"I found N merchants, such as A, B, C. <question>" through the node's
    deterministic readback (an ask question does not interpolate slots); the
    plain question when the candidate slots are missing."""
    found, question, plain = TEXT["pick_found"], TEXT["pick_question"], TEXT["pick_plain"]
    return {locale: {
        "intro": "", "fields": [],
        "groups": [
            {"requires": ["merchant_match_count", "merchant_candidates"],
             "template": found[locale] + " " + question[locale]},
            {"absent": ["merchant_candidates"], "template": plain[locale]},
        ],
        "question": "",
    } for locale in ("en", "hi")}


def _second_ask(nid: str, label: str, team_ask: dict, variable: str, text_key: str,
                x: int, y: int, *, readback: dict | None = None) -> dict:
    """The second answer: the team's own answer pattern / unmatched reply /
    "can't hear you" guards, a new variable, own wording (or readback)."""
    config = {k: copy.deepcopy(v) for k, v in (team_ask.get("config") or {}).items()
              if k in ("pattern", "entityType", "unmatchedReply", "unmatchedReplyByLanguage",
                       "rejectAnswerPatterns", "rejectedAnswerReply",
                       "rejectedAnswerReplyByLanguage", "entity")}
    text = TEXT[text_key]
    config.update({"question": text["en"], "textByLanguage": dict(text), "variable": variable,
                   "responseMode": "fixed"})
    if readback:
        config["readback"] = readback
    return _node(nid, "ask", label, config, x, y)


def is_alternate_lookup_upgraded(definition: dict) -> bool:
    return any(n.get("id") == MARKER_NODE for n in definition.get("nodes") or [])


def _answer_edges(edges: list[dict], ask: str) -> list[dict]:
    return [e for e in edges if e.get("from") == ask
            and str(e.get("label") or "").lower() != "fallback"]


def _check_anchors(name: str, spec: dict, by_id: dict, edges: list[dict]) -> None:
    for node_id in (spec["ask"], spec["noted"], spec["found"]):
        if node_id not in by_id:
            raise UpgradeError(f"{name}: node {node_id} not found — refusing to guess")
    ask_cfg = by_id[spec["ask"]].get("config") or {}
    if ask_cfg.get("variable") != spec["variable"]:
        raise UpgradeError(f"{name}: {spec['ask']} stores {ask_cfg.get('variable')!r}, expected "
                           f"{spec['variable']!r} — refusing to guess")
    answered = _answer_edges(edges, spec["ask"])
    if len(answered) != 1 or answered[0].get("to") != spec["noted"]:
        raise UpgradeError(f"{name}: expected one answer edge {spec['ask']} → {spec['noted']}, "
                           f"found {[(e.get('id'), e.get('to')) for e in answered]} — refusing "
                           "to guess")
    frm, label, expected = spec["no_tid"]
    no_tid = [e for e in edges if e.get("from") == frm and str(e.get("label")) == label]
    if len(no_tid) != 1 or no_tid[0].get("to") != expected:
        raise UpgradeError(f"{name}: expected one '{label}' edge {frm} → {expected}, found "
                           f"{[(e.get('id'), e.get('to')) for e in no_tid]} — refusing to guess")


def upgrade_alternate_lookup(definition: dict) -> tuple[dict, list[str]]:
    """Look up the "MID and outlet name" answer in both journeys.  Pure and
    idempotent; refuses a definition whose anchors moved."""
    if is_alternate_lookup_upgraded(definition):
        return definition, []
    result = copy.deepcopy(definition)
    by_id = {n["id"]: n for n in result["nodes"]}
    for name, spec in JOURNEYS.items():
        _check_anchors(name, spec, by_id, result["edges"])
    changes: list[str] = []
    for name, spec in JOURNEYS.items():
        p, ask, noted, found = spec["prefix"], spec["ask"], spec["noted"], spec["found"]
        x, y = int(by_id[ask].get("x") or 0), int(by_id[ask].get("y") or 0) + 160
        title = "Paper roll" if name == "paper_roll" else "Battery"
        nodes = [
            _lookup(f"{p}_mo_api", f"{title}: merchant lookup API (MID / outlet name)", x, y),
            _cond(f"{p}_mo_found", f"{title}: merchant found (MID / outlet name)?",
                  FOUND_VARIABLE, "true", x + 200, y),
            _cond(f"{p}_mo_ambiguous", f"{title}: several merchants match the name?",
                  AMBIGUOUS_VARIABLE, "true", x + 400, y),
            _second_ask(f"{p}_mo_pick", f"{title}: several matches — ask full outlet name or MID",
                        by_id[ask], f"{spec['variable']}_retry", "pick_plain", x + 400, y + 140,
                        readback=_pick_readback()),
            _second_ask(f"{p}_mo_retry", f"{title}: not found — ask MID and outlet name again",
                        by_id[ask], f"{spec['variable']}_retry", "retry", x + 400, y + 280),
            _lookup(f"{p}_mo_api_retry", f"{title}: merchant lookup API (second answer)",
                    x + 600, y + 140),
            _cond(f"{p}_mo_found_retry", f"{title}: merchant found (second answer)?",
                  FOUND_VARIABLE, "true", x + 800, y + 140),
            _message(f"{p}_no_tid_bridge", f"{title}: no Terminal ID — MID and outlet name instead",
                     "no_tid_bridge", x - 220, y - 160),
        ]
        edges = [
            _edge(f"{p}_mo_api", f"{p}_mo_found", "success"),
            _edge(f"{p}_mo_api", noted, "failure"),
            _edge(f"{p}_mo_found", found, "true"),
            _edge(f"{p}_mo_found", f"{p}_mo_ambiguous", "false"),
            _edge(f"{p}_mo_ambiguous", f"{p}_mo_pick", "true"),
            _edge(f"{p}_mo_ambiguous", f"{p}_mo_retry", "false"),
            _edge(f"{p}_mo_pick", f"{p}_mo_api_retry"),
            _edge(f"{p}_mo_pick", noted, "fallback"),
            _edge(f"{p}_mo_retry", f"{p}_mo_api_retry"),
            _edge(f"{p}_mo_retry", noted, "fallback"),
            _edge(f"{p}_mo_api_retry", f"{p}_mo_found_retry", "success"),
            _edge(f"{p}_mo_api_retry", noted, "failure"),
            _edge(f"{p}_mo_found_retry", found, "true"),
            _edge(f"{p}_mo_found_retry", noted, "false"),
            _edge(f"{p}_no_tid_bridge", ask),
        ]
        answer = _answer_edges(result["edges"], ask)[0]
        answer["to"] = f"{p}_mo_api"
        frm, label, expected = spec["no_tid"]
        no_tid = next(e for e in result["edges"]
                      if e.get("from") == frm and str(e.get("label")) == label)
        no_tid["to"] = f"{p}_no_tid_bridge"
        result["nodes"].extend(nodes)
        result["edges"].extend(edges)
        changes.append(f"{name}: {ask} answer → {p}_mo_api (was {noted}, unverified)")
        changes.append(f"{name}: {frm} [{label}] → {p}_no_tid_bridge → {ask} (was {expected})")
        changes.append(f"{name}: + {len(nodes)} nodes / {len(edges)} edges")
    _validate(result)
    return result, changes


# ── system prompt (exact-match edits of the published v21 text) ─────────────

PROMPT_ROUTING_OLD = (
    "Every journey requires Terminal ID for lookup/verification. If unknown, unavailable or not "
    "found in records, the request cannot proceed and the FLOW explains/closes. At its question, "
    "explain why if asked and re-ask T-I-D; never substitute name/mobile/other data or offer SMS, "
    "IVR, request-raising or battery steps without it.")
PROMPT_ROUTING_NEW = (
    "Every journey starts with the merchant lookup. The FLOW asks for the Terminal ID first; if "
    "the caller does not have it or it is not found, the FLOW itself asks for the MID (Merchant "
    "ID) and outlet name and looks them up, and when several merchants match it asks for the "
    "complete outlet name or the MID. Never search, guess or confirm a record yourself and never "
    "offer these alternatives before the FLOW does; when no record is found, follow the FLOW "
    "and never call the details verified. At the Terminal ID question, explain why if asked and "
    "re-ask T-I-D; never substitute mobile/other data or offer SMS, IVR, request-raising or "
    "battery steps before the FLOW reaches them.")
PROMPT_DIGITS_OLD = (
    '- Terminal ID (including call data/caller repeat): digit-by-digit groups of four, letters as '
    'single capital letters: 11398421 → "one one three nine, eight four two one"; 1132986A → '
    '"one one three two, nine eight six, A".')
PROMPT_DIGITS_NEW = PROMPT_DIGITS_OLD + (
    '\n- MID / Merchant ID (`merchant_id`, 12 to 15 characters): the same way, digit by digit in '
    'groups of four, a letter as a single capital letter: 0110656A0007856 → "zero one one zero, '
    'six five six, A, zero zero zero seven, eight five six".')
PROMPT_FIELDS_OLD = (
    "Terminal fields (`terminal_id`, `outlet_name`, `bank_name`, `asset_type`, `contact_number`, "
    "`address`, `contact_person`) become available ONLY after caller provides Terminal ID and flow "
    "completes lookup. If ID not found, flow closes; only lookup-service outage allows call to "
    "continue without these fields.")
PROMPT_FIELDS_NEW = (
    "Terminal fields (`terminal_id`, `merchant_id`, `outlet_name`, `bank_name`, `asset_type`, "
    "`contact_number`, `address`, `contact_person`) become available ONLY after caller provides "
    "the Terminal ID, or the MID and outlet name, and flow completes lookup. "
    "`merchant_candidates` / `merchant_match_count` only describe an earlier ambiguous name "
    "search; never read them out as the caller's record. If no record is found, the FLOW may "
    "continue without these fields (unverified); they then stay unknown for the whole call.")
PROMPT_NOTE = "Merchant Helpdesk — MID and outlet name looked up when the Terminal ID is missing"


def upgrade_prompt_alternate_lookup(text: str) -> str | None:
    """None when already current; refuses an edited anchor."""
    if PROMPT_ROUTING_NEW in text:
        return None
    for anchor, what in ((PROMPT_ROUTING_OLD, "Terminal ID routing rule"),
                         (PROMPT_DIGITS_OLD, "Terminal ID digit rule"),
                         (PROMPT_FIELDS_OLD, "Terminal fields sentence")):
        if text.count(anchor) != 1:
            raise UpgradeError(f"prompt: the {what} was edited — refusing to guess")
    return (text.replace(PROMPT_ROUTING_OLD, PROMPT_ROUTING_NEW, 1)
                .replace(PROMPT_DIGITS_OLD, PROMPT_DIGITS_NEW, 1)
                .replace(PROMPT_FIELDS_OLD, PROMPT_FIELDS_NEW, 1))
