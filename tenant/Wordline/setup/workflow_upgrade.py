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

The transform is shape-agnostic within the Wordline journey: it finds the
prefill asks by their ``prefillFromContext`` key (``asset_type`` on the
original v10, ``asset_model`` on the Studio v12 edited on DEMO), and wires
the lookup in front of whatever node followed each Terminal ID question (the
combined read-back on v10, the per-field "as per our records" confirmation
chain on v12).

Step 2 — ``upgrade_terminal_id_capture`` (2026-11-07, after real voice tests):

4. The Terminal ID asks accepted any run of four or more digits.  Callers
   pause while dictating and the STT emits each fragment as a final, so the
   first four digits were looked up before the caller finished.  The pattern
   is pinned to the 7–8 digit lengths the data has: the voice brain's
   identifier capture then holds shorter fragments for the dictation window
   (3 s) and the engine accumulates them ("noted 4 digits so far — please
   continue") until a valid length arrives.  An 8-digit id dictated with a
   pause after exactly the 7th digit still dispatches at 7 (a 7-digit id is
   valid) — that remaining case misses the lookup and goes through the
   existing retry ask.
5. A read-back node whose only outgoing edge is the "yes" confirmation (the
   v10 shape) gets a "no / wrong details" edge to an apology message that
   continues the journey unverified.  v12's confirmations already carry
   their own "wrong → disconnect" edges and are left alone.

Step 3 — ``upgrade_visit_details`` (2026-11-10, CSV gained ``Address`` and
``Contact Person``; the API returns them TTS-ready as ``address`` /
``contact_person`` and the connection maps them into slots):

6. The two free-text steps that collect the visit details after a Terminal
   ID lookup — ``n_raise`` (we raise the paper roll request) and
   ``n_bat_visit`` (engineer visit for a damaged battery) — asked the caller
   to dictate the outlet name, T-I-D, complete address, contact number and
   contact person although a verified record already holds them.  A
   condition ``address exists`` now sits in front of each: with a record the
   flow goes to a new ask (``n_raise_record`` / ``n_bat_visit_record``) that
   reads the registered address and contact person back (deterministic
   ``readback`` groups — one for both values, one for an address without a
   contact person) and asks only for corrections plus what the record cannot
   know (weekly off, preferred working time, the paper roll quantity).
   Without a record — lookup miss, API down, wrong details, the below-80 %
   journey (no lookup), or a merchant service that predates the two fields —
   the original node runs unchanged.  The new asks store the answer in the
   same variable and continue to the same next step, so routing after them
   is identical.  Edges into the original nodes keep their ids and labels;
   only their target becomes the gate.

Step 4 — ``upgrade_option_hubs`` (2026-11-10, DEMO cv_cb68cbdc84a0):

7. "Tell me the IVR process" at the options hub was labelled ``question``
   by the intent classifier; the engine lets a question label take a literal
   edge only when the words have no question shape, and "tell"/"batao" are
   question markers — so the turn went off-script and the knowledge base
   answered instead of ``n_ivr`` (the hub's labels already carry "ivr
   process" / "tell me about ivr": a label change alone cannot fix it).  The
   three option hubs (``n_options``, ``n_options_unverified``,
   ``n_pr_again``) get ``optionHub: true`` (engine opt-in, see
   ``_choose_intent_edge_detailed``): a question naming an option selects
   it; a question naming none ("who is the contact person?") stays
   off-script.
8. "No, that's all" there was labelled ``refusal`` and no edge supported it:
   the LLM said "You're welcome" and the call stayed open.  Each option hub
   gets one closing edge to the existing ``n_end`` with decline / closing
   tokens only — no affirm-classified token ("theek hai" at "which option?"
   means "go on", not "goodbye"), no "thank you" (it rides along with real
   answers).  With ``optionHub`` an option named in the same breath still
   wins ("nahi, IVR batao" → IVR).
"""

from __future__ import annotations

import copy
import json
import re

CONNECTION_NAME = "Wordline Merchant Lookup"
RETRY_VARIABLE = "spoken_terminal_id_retry"
FOUND_VARIABLE = "merchant_found"

# Context keys the API now supplies (→ the slot name the connection maps into).
API_SLOT_BY_CONTEXT_KEY = {
    "outlet_name": "outlet_name",
    "bank_name": "bank_name",
    "asset_type": "asset_type",
    "asset_model": "asset_type",      # DEMO v12 spelling of the same field
    "contact_number": "contact_number",
}
TERMINAL_VARIABLES = ("spoken_terminal_id", RETRY_VARIABLE)
MARKER_NODE = "n_api_lookup"

# Step 1 keeps the asks' original open-ended pattern; step 2 pins it.
TERMINAL_ENTITY = {"dataType": "text",
                   "regexPattern": r"(?<!\d)(\d(?:[\s-]?\d){3,})(?!\d)"}
# Terminal ids are 7 or 8 characters (digits, optionally a trailing letter);
# the converter guarantees unique digit keys.  Spaces/dashes between dictated
# digits are allowed; a longer run never matches partially (lookarounds).
TERMINAL_ENTITY_PINNED = {"dataType": "text",
                          "regexPattern": r"(?<!\d)(\d(?:[\s-]?\d){6,7})(?!\d)"}
WRONG_DETAILS_LABEL = ("Nahi galat hai/Nahi, ye sahi nahi hai/Galat hai/Sahi nahi hai/Sahi nahi/"
                       "Nahi, aisa nahi hai/Ye galat hai/Bilkul galat hai/Wrong/Incorrect/"
                       "Not correct/No, that is wrong/No, not correct/No sir/Nahi/Nahi ji/No/"
                       "नहीं/नहीं जी/गलत है/सही नहीं है/नहीं, यह सही नहीं है/यह गलत है")
_NEGATIVE_TOKENS = ("no", "nahi", "galat", "wrong", "incorrect", "not correct", "नहीं", "गलत")

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
    "pr_wrong": {
        "en": ("Sorry Sir, I was unable to verify the terminal details. "
               "I can still help you with your paper roll request."),
        "hi": ("माफ़ कीजिए Sir, terminal details verify नहीं हो पाईं। फिर भी "
               "मैं आपकी paper roll request में मदद कर सकता हूँ।"),
    },
    "bat_wrong": {
        "en": ("Sorry Sir, I was unable to verify the terminal details. "
               "I can still help you troubleshoot the battery issue."),
        "hi": ("माफ़ कीजिए Sir, terminal details verify नहीं हो पाईं। फिर भी "
               "मैं battery की problem troubleshoot करने में आपकी मदद कर सकता हूँ।"),
    },
}

# Step 3 — record-aware visit-detail asks.  ``{address}`` / ``{contact_person}``
# are rendered by the node's ``readback`` groups from the looked-up slots; the
# plain ``question`` / ``textByLanguage`` carry no placeholder (they are what a
# re-ask, the LLM's "pending question" and the Studio editor show).
VISIT_TEXT = {
    "n_raise": {
        "both": {
            "en": ("I understand, Sir or Ma'am. If you're unable to place the request through SMS "
                   "or I-V-R, I can raise the paper roll request on your behalf. I already have "
                   "your Outlet Name, T-I-D and registered contact number. As per our records, "
                   "your address is {address}, and the contact person is {contact_person}. If any "
                   "of these details have changed, please tell me the correct ones. Please also "
                   "share your Work Order details, including your weekly off, your preferred "
                   "working time when our team can visit, and the required paper roll quantity, "
                   "which should be between one and nine."),
            "hi": ("जी सर या मैम, मैं आपकी बात समझ सकता हूँ। अगर आप SMS या I-V-R के जरिए request "
                   "नहीं कर पा रहे हैं, तो मैं आपकी तरफ से paper roll की request raise कर सकता "
                   "हूँ। आपका Outlet Name, T-I-D और registered contact number मेरे पास पहले से है। "
                   "हमारे records के अनुसार आपका address {address} है, और contact person "
                   "{contact_person} हैं। अगर इनमें से कुछ बदल गया है, तो कृपया सही details "
                   "बताइए। साथ ही अपने Work Order की details बताइए, जिसमें आपका weekly off भी "
                   "शामिल हो, अपना preferred working time बताइए, जिस समय हमारी team visit कर सके, "
                   "और आपको कितने paper rolls चाहिए, उसकी quantity बताइए। ये quantity one से "
                   "nine के बीच होनी चाहिए।"),
        },
        "address_only": {
            "en": ("I understand, Sir or Ma'am. If you're unable to place the request through SMS "
                   "or I-V-R, I can raise the paper roll request on your behalf. I already have "
                   "your Outlet Name, T-I-D and registered contact number. As per our records, "
                   "your address is {address}. If it has changed, please tell me the correct "
                   "address. Please also share the contact person's name, your Work Order "
                   "details, including your weekly off, your preferred working time when our team "
                   "can visit, and the required paper roll quantity, which should be between one "
                   "and nine."),
            "hi": ("जी सर या मैम, मैं आपकी बात समझ सकता हूँ। अगर आप SMS या I-V-R के जरिए request "
                   "नहीं कर पा रहे हैं, तो मैं आपकी तरफ से paper roll की request raise कर सकता "
                   "हूँ। आपका Outlet Name, T-I-D और registered contact number मेरे पास पहले से है। "
                   "हमारे records के अनुसार आपका address {address} है। अगर address बदल गया है, "
                   "तो कृपया सही address बताइए। साथ ही contact person का नाम, अपने Work Order की "
                   "details, जिसमें आपका weekly off भी शामिल हो, अपना preferred working time, और "
                   "paper rolls की quantity बताइए। ये quantity one से nine के बीच होनी चाहिए।"),
        },
        "question": {
            "en": ("Please confirm whether the address and contact person on our records are "
                   "correct, and share your Work Order details including your weekly off, your "
                   "preferred working time for our team's visit, and the paper roll quantity, "
                   "between one and nine."),
            "hi": ("कृपया बताइए कि हमारे records वाला address और contact person सही हैं या नहीं, "
                   "और अपने Work Order की details, weekly off, preferred working time और paper "
                   "rolls की quantity बताइए, जो one से nine के बीच हो।"),
        },
    },
    "n_bat_visit": {
        "both": {
            "en": ("To arrange the field engineer's visit, I already have your Outlet Name, T-I-D "
                   "and registered contact number. As per our records, your address is {address}, "
                   "and the contact person is {contact_person}. If any of these details have "
                   "changed, please tell me the correct ones. Please also confirm your weekly off "
                   "and your preferred working time, so we can proceed with your service request."),
            "hi": ("Field engineer की visit arrange करने के लिए, आपका Outlet Name, T-I-D और "
                   "registered contact number मेरे पास पहले से है। हमारे records के अनुसार आपका "
                   "address {address} है, और contact person {contact_person} हैं। अगर इनमें से "
                   "कुछ बदल गया है, तो कृपया सही details बताइए। साथ ही कृपया अपना weekly off और "
                   "preferred working time बताइए, ताकि हम आपकी service request आगे बढ़ा सकें।"),
        },
        "address_only": {
            "en": ("To arrange the field engineer's visit, I already have your Outlet Name, T-I-D "
                   "and registered contact number. As per our records, your address is {address}. "
                   "If it has changed, please tell me the correct address. Please also share the "
                   "contact person's name, your weekly off and your preferred working time, so we "
                   "can proceed with your service request."),
            "hi": ("Field engineer की visit arrange करने के लिए, आपका Outlet Name, T-I-D और "
                   "registered contact number मेरे पास पहले से है। हमारे records के अनुसार आपका "
                   "address {address} है। अगर address बदल गया है, तो कृपया सही address बताइए। "
                   "साथ ही contact person का नाम, अपना weekly off और preferred working time "
                   "बताइए, ताकि हम आपकी service request आगे बढ़ा सकें।"),
        },
        "question": {
            "en": ("Please confirm whether the address and contact person on our records are "
                   "correct, and share your weekly off and your preferred working time for the "
                   "engineer's visit."),
            "hi": ("कृपया बताइए कि हमारे records वाला address और contact person सही हैं या नहीं, "
                   "और अपना weekly off और engineer की visit के लिए preferred working time बताइए।"),
        },
    },
}
VISIT_REJECT_PREFIX = {
    "en": "Sorry Sir, the line may not have been clear. Let me ask again. ",
    "hi": "माफ़ कीजिए Sir, शायद line पर आवाज़ साफ़ नहीं आई। मैं दोबारा पूछता हूँ। ",
}
# Step 4 — option hubs.
OPTION_HUBS = ("n_options", "n_options_unverified", "n_pr_again")
CLOSE_NODE = "n_end"
# Decline / closing tokens only.  Every token must classify as ``refusal`` or
# as no signal (tests pin it): an affirm token would close the call on "theek
# hai, batao".  Left out on purpose: "thank you", bare "nothing", "that's it",
# bare "itna hi" — they ride along with real answers ("thank you, IVR batao").
CLOSING_LABEL = "/".join((
    "No", "No thanks", "No thank you", "Nope", "Nothing else", "Nothing more",
    "That's all", "That is all", "Thats all", "That will be all", "No need", "Not needed",
    "Not required", "I don't need anything", "I'm done", "Goodbye",
    "Nahi", "Nahin", "Nahi ji", "Nahi chahiye", "Kuch nahi", "Kuch nahi chahiye",
    "Aur kuch nahi", "Bas", "Bas itna hi", "Bas ho gaya", "Nahi bas", "Zarurat nahi",
    "Koi zarurat nahi", "Rehne do",
    "नहीं", "नहीं जी", "नहीं चाहिए", "कुछ नहीं", "और कुछ नहीं", "बस", "बस इतना ही",
    "ज़रूरत नहीं", "कोई ज़रूरत नहीं", "रहने दो", "नहीं बस",
))
ADDRESS_VARIABLE = "address"
PERSON_VARIABLE = "contact_person"
# original ask → (gate condition id, record ask id, Studio label of the record ask)
VISIT_STEPS = {
    "n_raise": ("n_raise_has_record", "n_raise_record",
                "Paper roll: we raise it — confirm address & contact person from records"),
    "n_bat_visit": ("n_bat_visit_has_record", "n_bat_visit_record",
                    "Battery: visit details — confirm address & contact person from records"),
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


def _prefill_nodes(nodes: list[dict]) -> list[dict]:
    """Silent asks that copy an API-supplied field from the call context."""
    out = []
    for node in nodes:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if (node.get("kind") == "ask" and cfg.get("prefillOnly") is True
                and str(cfg.get("prefillFromContext") or "") in API_SLOT_BY_CONTEXT_KEY):
            out.append(node)
    return out


def slot_renames(definition: dict) -> dict[str, str]:
    """{prefill variable → API slot} for every prefill ask in the definition."""
    renames = {}
    for node in _prefill_nodes(definition.get("nodes") or []):
        cfg = node["config"]
        renames[str(cfg.get("variable"))] = API_SLOT_BY_CONTEXT_KEY[str(cfg["prefillFromContext"])]
    return renames


def _single_target(edges: list[dict], frm: str) -> str:
    targets = [str(e.get("to")) for e in edges if e.get("from") == frm]
    if len(targets) != 1:
        raise UpgradeError(f"expected exactly one edge out of {frm}, found {len(targets)}")
    return targets[0]


def upgrade_workflow(definition: dict) -> tuple[dict, list[str]]:
    """Step 1: return (definition with the lookup wired in, change list)."""
    if is_upgraded(definition):
        return definition, ["already upgraded (n_api_lookup present) — no change"]
    nodes = copy.deepcopy(list(definition.get("nodes") or []))
    edges = copy.deepcopy(list(definition.get("edges") or []))
    by_id = {n.get("id"): n for n in nodes}
    required = ("n_start", "n_ask_tid", "n_options", "n_bat_tid", "n_bat_check")
    missing = [nid for nid in required if nid not in by_id]
    if missing:
        raise UpgradeError(f"not the expected Wordline workflow; missing nodes {missing}")
    prefill_nodes = _prefill_nodes(nodes)
    if not prefill_nodes:
        raise UpgradeError("no context-prefill asks for the API-supplied fields found")
    changes: list[str] = []

    # 1. Drop the context prefill chain for the API-sourced fields and
    #    reconnect what came before it to what came after it.
    prefill = {n["id"] for n in prefill_nodes}
    into = [e for e in edges if e.get("to") in prefill and e.get("from") not in prefill]
    out = [e for e in edges if e.get("from") in prefill and e.get("to") not in prefill]
    if len(into) != 1 or len(out) != 1:
        raise UpgradeError("prefill chain is not linear (expected one entry and one exit edge)")
    renames = slot_renames(definition)
    nodes = [n for n in nodes if n.get("id") not in prefill]
    edges = [e for e in edges if e.get("from") not in prefill and e.get("to") not in prefill]
    edges.insert(0, _edge(str(into[0]["from"]), str(out[0]["to"])))
    changes.append(f"removed prefill asks {sorted(prefill)}; "
                   f"linked {into[0]['from']} → {out[0]['to']}")

    # 2. Read-backs, confirmations and conditions use the API-mapped slot names.
    renamed = 0
    for node in nodes:
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        raw = json.dumps(config, ensure_ascii=False)
        new = raw
        for old, fresh in renames.items():
            new = new.replace(f'"{old}"', f'"{fresh}"')
        if new != raw:
            node["config"] = json.loads(new)
            renamed += 1
    changes.append(f"renamed {renames} in {renamed} node(s)")

    # 3. Lookup blocks after both Terminal ID questions, in front of whatever
    #    followed them (combined read-back on v10, per-field chain on v12).
    pr_target = _single_target(edges, "n_ask_tid")
    bat_target = _single_target(edges, "n_bat_tid")
    edges = [e for e in edges if not (e.get("from") in ("n_ask_tid", "n_bat_tid"))]

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
        "n_", after="n_ask_tid", found_to=pr_target, continue_to="n_options_unverified",
        unverified_key="pr_unverified", unavailable_key="pr_unavailable",
        x=int(ask_tid.get("x", 0)) + 100, y=int(ask_tid.get("y", 0)) + 360, label="Paper roll")
    bat_nodes, bat_edges = _lookup_block(
        "n_bat_", after="n_bat_tid", found_to=bat_target, continue_to="n_bat_check",
        unverified_key="bat_unverified", unavailable_key="bat_unavailable",
        x=int(bat_tid.get("x", 0)) + 100, y=int(bat_tid.get("y", 0)) + 400, label="Battery")

    nodes.extend([*pr_nodes, unverified_options, *bat_nodes])
    edges.extend([*pr_edges, *option_edges, *bat_edges])
    changes.append(f"paper roll: n_ask_tid → {CONNECTION_NAME!r} lookup → found?/retry/"
                   f"unverified/unavailable → {pr_target} | n_options_unverified "
                   f"({len(option_edges)} option edges cloned)")
    changes.append(f"battery: n_bat_tid → {CONNECTION_NAME!r} lookup → found?/retry/"
                   f"unverified/unavailable → {bat_target} | n_bat_check")

    upgraded = {**definition, "nodes": nodes, "edges": edges}
    _validate(upgraded)
    return upgraded, changes


# ── step 2: Terminal ID capture ─────────────────────────────────────────────


def _terminal_asks(nodes: list[dict]) -> list[dict]:
    return [n for n in nodes if n.get("kind") == "ask"
            and str((n.get("config") or {}).get("variable")) in TERMINAL_VARIABLES]


def is_capture_upgraded(definition: dict) -> bool:
    asks = _terminal_asks(definition.get("nodes") or [])
    return bool(asks) and all(
        ((n.get("config") or {}).get("entity") or {}).get("regexPattern")
        == TERMINAL_ENTITY_PINNED["regexPattern"] for n in asks)


def _reaches(edges: list[dict], start: str, goal: str) -> bool:
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        if cur == goal:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(str(e.get("to")) for e in edges if e.get("from") == cur)
    return False


def upgrade_terminal_id_capture(definition: dict) -> tuple[dict, list[str]]:
    """Step 2 (requires step 1): pinned 7-8 digit Terminal ID pattern and a
    "wrong details" path for read-backs that lack one.  Idempotent."""
    if not is_upgraded(definition):
        raise UpgradeError("apply upgrade_workflow (the lookup wiring) first")
    if is_capture_upgraded(definition):
        return definition, ["already upgraded (Terminal ID pattern pinned) — no change"]
    nodes = copy.deepcopy(list(definition.get("nodes") or []))
    edges = copy.deepcopy(list(definition.get("edges") or []))
    changes: list[str] = []

    asks = _terminal_asks(nodes)
    for node in asks:
        entity = dict(node["config"].get("entity") or {})
        entity.update(TERMINAL_ENTITY_PINNED)
        node["config"]["entity"] = entity
    changes.append(f"pinned Terminal ID pattern to 7-8 digits on {[n['id'] for n in asks]}")

    api_slots = set(API_SLOT_BY_CONTEXT_KEY.values())
    added = []
    for node in nodes:
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if node.get("kind") != "intent" or not isinstance(cfg.get("readback"), dict):
            continue
        if not api_slots & set(re.findall(r'"variable": "([a-z_]+)"', json.dumps(cfg["readback"]))):
            continue
        outgoing = [e for e in edges if e.get("from") == node["id"]]
        if any(t in _NEGATIVE_TOKENS for e in outgoing
               for t in [x.strip().lower() for x in re.split(r"[/,|]", e.get("label") or "")]):
            continue  # the author already handled "wrong details" (v12 → disconnect)
        if _reaches(edges, node["id"], "n_options"):
            prefix, key, cont, label = "n_", "pr_wrong", "n_options_unverified", "Paper roll"
        elif _reaches(edges, node["id"], "n_bat_check"):
            prefix, key, cont, label = "n_bat_", "bat_wrong", "n_bat_check", "Battery"
        else:
            continue
        msg_id = f"{prefix}details_wrong"
        if msg_id not in {n["id"] for n in nodes}:
            nodes.append(_message(msg_id, f"{label}: details wrong — continue unverified", key,
                                  int(node.get("x", 0)) + 160, int(node.get("y", 0)) + 240))
            edges.append(_edge(msg_id, cont))
        edges.append(_edge(node["id"], msg_id, WRONG_DETAILS_LABEL))
        added.append(f"{node['id']} → {msg_id} → {cont}")
    changes.append(f"wrong-details edges added: {added or 'none needed'}")

    upgraded = {**definition, "nodes": nodes, "edges": edges}
    _validate(upgraded)
    return upgraded, changes


# ── step 3: record-aware visit-detail asks ──────────────────────────────────


def is_visit_upgraded(definition: dict) -> bool:
    ids = {n.get("id") for n in definition.get("nodes") or []}
    present = [orig for orig in VISIT_STEPS if orig in ids]
    return bool(present) and all(VISIT_STEPS[orig][1] in ids for orig in present)


def _visit_readback(texts: dict) -> dict:
    """Per-locale readback: one sentence group when both values exist, one
    for an address without a contact person.  The gate guarantees the
    address, so one of the two groups always applies."""
    readback = {}
    for lang in ("en", "hi"):
        readback[lang] = {
            "intro": "",
            "groups": [
                {"requires": [ADDRESS_VARIABLE, PERSON_VARIABLE],
                 "template": texts["both"][lang]},
                {"requires": [ADDRESS_VARIABLE], "absent": [PERSON_VARIABLE],
                 "template": texts["address_only"][lang]},
            ],
            "fields": [],
            "question": "",
        }
    return readback


def _record_ask(original: dict, record_id: str, label: str) -> dict:
    cfg = original.get("config") or {}
    texts = VISIT_TEXT[original["id"]]
    question = dict(texts["question"])
    config = {
        "question": question["en"],
        "variable": cfg.get("variable"),
        "entityType": cfg.get("entityType") or "text",
        "responseMode": cfg.get("responseMode") or "fixed",
        "textByLanguage": question,
        "readback": _visit_readback(texts),
        # "Can't hear you" re-ask: a static line (a re-ask cannot render the
        # record values); the caller can ask for the address and the LLM
        # answers from the verified record.
        "rejectedAnswerReply": VISIT_REJECT_PREFIX["en"] + question["en"],
        "rejectedAnswerReplyByLanguage": {
            lang: VISIT_REJECT_PREFIX[lang] + question[lang] for lang in ("en", "hi")},
    }
    if cfg.get("rejectAnswerPatterns"):
        config["rejectAnswerPatterns"] = copy.deepcopy(cfg["rejectAnswerPatterns"])
    return _node(record_id, "ask", label, config,
                 int(original.get("x", 0)) + 220, int(original.get("y", 0)) + 140)


def upgrade_visit_details(definition: dict) -> tuple[dict, list[str]]:
    """Step 3 (requires step 1): record-aware request / visit asks gated on
    ``address exists``.  Idempotent; the original asks stay as the fallback."""
    if not is_upgraded(definition):
        raise UpgradeError("apply upgrade_workflow (the lookup wiring) first")
    if is_visit_upgraded(definition):
        return definition, ["already upgraded (record-aware visit asks present) — no change"]
    present = {n.get("id") for n in definition.get("nodes") or []} & set(VISIT_STEPS)
    if not present:
        return definition, ["no request / visit-detail ask in this workflow — no change"]
    nodes = copy.deepcopy(list(definition.get("nodes") or []))
    edges = copy.deepcopy(list(definition.get("edges") or []))
    by_id = {n["id"]: n for n in nodes}
    changes: list[str] = []
    for orig_id, (gate_id, record_id, label) in VISIT_STEPS.items():
        original = by_id.get(orig_id)
        if original is None:
            changes.append(f"{orig_id} not in this workflow — skipped")
            continue
        if original.get("kind") != "ask":
            raise UpgradeError(f"{orig_id} is a {original.get('kind')} node, expected an ask")
        if record_id in by_id or gate_id in by_id:
            raise UpgradeError(f"{gate_id}/{record_id} already exist partially — refusing to guess")
        out = [e for e in edges if e.get("from") == orig_id]
        if not out:
            raise UpgradeError(f"{orig_id} has no outgoing edge")
        redirected = [e for e in edges if e.get("to") == orig_id]
        for edge in redirected:
            edge["to"] = gate_id            # ids and labels kept (Studio history)
        gate = _node(gate_id, "condition", f"{original.get('label') or orig_id}: address on record?",
                     {"variable": ADDRESS_VARIABLE, "operator": "exists"},
                     int(original.get("x", 0)), int(original.get("y", 0)) - 140)
        record = _record_ask(original, record_id, label)
        nodes.extend([gate, record])
        edges.append(_edge(gate_id, record_id, "true"))
        edges.append(_edge(gate_id, orig_id, "false"))
        for edge in out:
            new_edge = _edge(record_id, str(edge["to"]), edge.get("label") or None)
            edges.append(new_edge)
        changes.append(
            f"{[e['from'] for e in redirected]} → {gate_id} (address exists) → "
            f"true: {record_id} / false: {orig_id}; {record_id} → "
            f"{[str(e['to']) for e in out]} (variable {record['config']['variable']!r})")
    upgraded = {**definition, "nodes": nodes, "edges": edges}
    _validate(upgraded)
    return upgraded, changes


# ── step 4: option hubs (IVR question routing, closing edge) ────────────────


def is_option_hub_upgraded(definition: dict) -> bool:
    nodes = {n.get("id"): n for n in definition.get("nodes") or []}
    hubs = [h for h in OPTION_HUBS if h in nodes]
    edges = {(e.get("from"), e.get("to")) for e in definition.get("edges") or []}
    return bool(hubs) and all(
        (nodes[h].get("config") or {}).get("optionHub") is True and (h, CLOSE_NODE) in edges
        for h in hubs)


def upgrade_option_hubs(definition: dict) -> tuple[dict, list[str]]:
    """Step 4: ``optionHub`` on the option hubs + one closing edge each to
    ``n_end``.  Idempotent; nothing else is touched."""
    if is_option_hub_upgraded(definition):
        return definition, ["already upgraded (option hubs) — no change"]
    nodes = copy.deepcopy(list(definition.get("nodes") or []))
    edges = copy.deepcopy(list(definition.get("edges") or []))
    by_id = {n["id"]: n for n in nodes}
    if CLOSE_NODE not in by_id or by_id[CLOSE_NODE].get("kind") != "end":
        raise UpgradeError(f"{CLOSE_NODE} must be the workflow's end node")
    hubs = [h for h in OPTION_HUBS if h in by_id]
    if not hubs:
        return definition, ["no option hub in this workflow — no change"]
    changes: list[str] = []
    for hub_id in hubs:
        hub = by_id[hub_id]
        if hub.get("kind") != "intent":
            raise UpgradeError(f"{hub_id} is a {hub.get('kind')} node, expected an intent hub")
        hub.setdefault("config", {})["optionHub"] = True
        if any(e.get("from") == hub_id and e.get("to") == CLOSE_NODE for e in edges):
            changes.append(f"{hub_id}: optionHub on; closing edge already present")
            continue
        edge = {"id": f"e_{hub_id}__{CLOSE_NODE}__closing", "from": hub_id, "to": CLOSE_NODE,
                "label": CLOSING_LABEL}
        edges.append(edge)
        changes.append(f"{hub_id}: optionHub on; closing edge → {CLOSE_NODE} "
                       f"({len(CLOSING_LABEL.split('/'))} tokens)")
    upgraded = {**definition, "nodes": nodes, "edges": edges}
    _validate(upgraded)
    return upgraded, changes


def upgrade_all(definition: dict) -> tuple[dict, list[str]]:
    """All steps, each idempotent."""
    step1, changes = upgrade_workflow(definition)
    step2, more = upgrade_terminal_id_capture(step1)
    step3, visit = upgrade_visit_details(step2)
    step4, hubs = upgrade_option_hubs(step3)
    return step4, changes + more + visit + hubs


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
