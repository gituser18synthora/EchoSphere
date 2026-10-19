"""Jeeves — Airtel Broadband uninstallation bot (bot_44a50e8c4146):
structured post-call summary.

What this stage does (dry-run by default, idempotent):

  1. Workflow upgrade (v12 → next version). Silent bookkeeping nodes record
     the call's OUTCOME in the final workflow slots, because the engine only
     honours ``setSlots`` on message nodes (an end node just speaks):
       - ``visit_status``: incomplete (set when the flow starts) → booked |
         date_pending | callback_requested | address_mismatch | wrong_person |
         not_disconnected, set right before the matching end node.
       - ``address_verified``: "yes" on the explicit yes-edge of the address
         question, "no" on the mismatch branch; unset otherwise.
       - ``visit_day`` / ``visit_time`` always hold the FINALLY confirmed
         values: the "change the date" branch clears them and the second
         date ask captures into them again (the readback keeps its own
         ``visit_day_2`` / ``visit_time_2`` slots, so nothing spoken changes).
     Every marker is a ``message`` node with ``silent: true`` — the pattern
     the flow already uses for its address-mismatch sub-disposition.
  2. ``goalPolicy.summaryFields`` — the structured summary derived from those
     slots by shared/post_call/structured.py. All fields are slot-only
     (``allowLlm: false``): a value the flow did not record stays null.
  3. Visit day/time validation (second workflow upgrade, v13 → v14): the four
     date-capturing asks declare ``validation: {type: future_datetime,
     timezone: Asia/Kolkata}`` (shared/orchestration/visit_datetime.py), so a
     past or impossible day, or a past/ambiguous time for TODAY, is refused
     with a short bilingual line and asked again — keeping whatever was
     valid — instead of being read back and booked. Unparseable phrasing is
     accepted as spoken; nothing is invented.
  4. Spoken-number time capture (third workflow upgrade): the five time
     regexes are rebuilt from the parser's own vocabulary
     (shared.orchestration.visit_datetime), so "कल सुबह नौ बजे", "subah das
     baje", "साढ़े चार बजे", "half past four" are captured whole instead of
     collapsing to the period word; a bare hour is clarified ("सुबह या शाम?")
     and the answer is composed with the hour it clarifies.
     The connector between a period word and the hour accepts को/के
     (ko/ke): "शाम के 5 बजे".
  5. Confirmation wording (fourth workflow upgrade): the two readback nodes
     say the booking the way a person would ("हमारे engineer कल शाम के 5 बजे
     आपके registered address … पर … के लिए आएँगे। क्या ये आपके लिए ठीक रहेगा?"),
     state that the engineer confirms the exact time when only a day was
     given, and their yes/no edges accept the natural answers to that
     question (ठीक रहेगा, चलेगा, सही है …). Outcome logic is untouched.

A summary-only goal policy keeps the live Goal Engine "derived" — the bot
talks exactly as before (see shared.orchestration.goal_engine).

Run:
  env/bin/python jeeves/setup/01_structured_summary.py              # dry run
  env/bin/python jeeves/setup/01_structured_summary.py --apply      # write
  env/bin/python jeeves/setup/01_structured_summary.py --dump out.json

Env: ECHOSPHERE_API_BASE (default http://127.0.0.1:9001/api/v1),
     JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin).
Tests: tests/unit/test_jeeves_structured_summary.py (golden comparison of
every spoken reply v12 vs upgraded, final-slot and field derivation checks).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pathlib
import sys

BOT_ID = "bot_44a50e8c4146"
WORKFLOW_ID = "wf_9685ed07e312"
BASELINE_VERSION = 12
HERE = pathlib.Path(__file__).resolve().parent
BASELINE_PATH = HERE / "workflow_v12_bot_44a50e8c4146.json"
# Run as a script from anywhere: the repo root must be importable for the
# parser vocabulary the time regexes are built from.
_ROOT = str(HERE.parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── structured summary fields ────────────────────────────────────────────────

VISIT_STATUS_OPTIONS = [
    "booked", "date_pending", "callback_requested", "address_mismatch",
    "wrong_person", "not_disconnected", "incomplete",
]

SUMMARY_FIELDS = [
    {
        "name": "address_verified",
        "label": "Address verified",
        "type": "yes_no",
        "source": "address_verified",
        "allowLlm": False,
        "description": (
            "Did the customer explicitly confirm the registered address for "
            "the uninstallation visit? Yes only on an explicit confirmation, "
            "No only when the customer said the address is wrong; otherwise "
            "not determined."
        ),
    },
    {
        "name": "appointment_booked",
        "label": "Pickup confirmed",
        "type": "yes_no",
        "source": "visit_status",
        "values": {"booked": "Yes", "*": "No"},
        "allowLlm": False,
        "description": (
            "Did the customer confirm the engineer visit at the final "
            "confirmation step? No for every other outcome, including a call "
            "that ended mid-flow."
        ),
    },
    {
        "name": "appointment_date",
        "label": "Pickup date",
        "type": "text",
        "source": "visit_day",
        "allowLlm": False,
        "requires": {"appointment_booked": ["Yes"]},
        "description": (
            "The visit day the customer confirmed, in their own words "
            "(e.g. 'kal', 'parso', '16 October'). Only when the appointment "
            "was confirmed."
        ),
    },
    {
        "name": "appointment_time",
        "label": "Pickup time",
        "type": "text",
        "source": "visit_time",
        "allowLlm": False,
        "requires": {"appointment_booked": ["Yes"]},
        "description": (
            "The visit time the customer gave, in their own words; empty "
            "when only a day was given. Only when the appointment was "
            "confirmed."
        ),
    },
    {
        "name": "call_result",
        "label": "Call result",
        "type": "choice",
        "source": "visit_status",
        "options": VISIT_STATUS_OPTIONS,
        "allowLlm": False,
        "description": (
            "How the guided flow ended: booked (visit confirmed), "
            "date_pending (team to fix the date), callback_requested "
            "(customer busy), address_mismatch, wrong_person, "
            "not_disconnected, incomplete (call ended mid-flow)."
        ),
    },
    {
        "name": "callback_date",
        "label": "Callback day",
        "type": "text",
        "source": "callback_date",
        "allowLlm": False,
        "requires": {"call_result": ["callback_requested"]},
        "description": "The day the customer asked to be called back, as spoken.",
    },
    {
        "name": "callback_time",
        "label": "Callback time",
        "type": "text",
        "source": "callback_time",
        "allowLlm": False,
        "requires": {"call_result": ["callback_requested"]},
        "description": "The time the customer asked to be called back, as spoken.",
    },
    {
        "name": "callback_preference",
        "label": "Callback request (as spoken)",
        "type": "text",
        "source": "callback_preference",
        "allowLlm": False,
        "requires": {"call_result": ["callback_requested"]},
        "description": "The customer's full callback request, as spoken.",
    },
]

# ── workflow upgrade ─────────────────────────────────────────────────────────

# Silent marker placed right before an end node: (end node, marker id, slots).
END_MARKERS = [
    ("n_end_booked", "n_mark_booked", {"visit_status": "booked"}),
    ("n_end_team_callback", "n_mark_date_pending", {"visit_status": "date_pending"}),
    ("n_end_callback", "n_mark_callback", {"visit_status": "callback_requested"}),
    ("n_end_wrong_person", "n_mark_wrong_person", {"visit_status": "wrong_person"}),
    ("n_end_not_disconnected", "n_mark_not_disconnected",
     {"visit_status": "not_disconnected"}),
]
START_MARKER = "n_mark_call_started"
ADDRESS_OK_MARKER = "n_mark_address_ok"
RESET_MARKER = "n_reset_visit"
MARKER_IDS = [START_MARKER, ADDRESS_OK_MARKER, RESET_MARKER] + [m for _, m, _ in END_MARKERS]


def _marker(node_id: str, label: str, slots: dict, *, x: int, y: int) -> dict:
    return {
        "id": node_id, "kind": "message", "label": label, "x": x, "y": y,
        "config": {"silent": True, "setSlots": dict(slots)},
    }


def _edge(src: str, dst: str, label: str | None = None) -> dict:
    edge = {"id": f"e_{src}__{dst}", "from": src, "to": dst}
    if label:
        edge["label"] = label
        edge["id"] += f"__{label}"
    return edge


def _repoint(edge: dict, new_to: str) -> None:
    """Point an existing edge at ``new_to`` keeping its label; the id keeps
    the ``e_<from>__<to>[__suffix]`` convention when it followed it."""
    old_prefix = f"e_{edge['from']}__{edge['to']}"
    edge_id = str(edge.get("id") or "")
    if edge_id.startswith(old_prefix):
        edge["id"] = f"e_{edge['from']}__{new_to}" + edge_id[len(old_prefix):]
    edge["to"] = new_to


def upgrade_workflow(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return the upgraded (nodes, edges). Idempotent: an already upgraded
    definition comes back unchanged."""
    nodes = copy.deepcopy(nodes)
    edges = copy.deepcopy(edges)
    by_id = {str(n.get("id")): n for n in nodes}
    if START_MARKER in by_id:
        return nodes, edges
    for required in ("n_start", "n_identity_gate", "n_confirm_address", "n_ask_visit_date",
                     "n_final_confirm", "n_ask_visit_date_2", "n_mark_address_mismatch",
                     *[end for end, _, _ in END_MARKERS]):
        if required not in by_id:
            raise ValueError(f"baseline node {required!r} missing — not the expected workflow")

    def pos(node_id: str, dx: int = -110, dy: int = 0) -> tuple[int, int]:
        n = by_id[node_id]
        return int(n.get("x", 0)) + dx, int(n.get("y", 0)) + dy

    # 1. Flow entered → visit_status = incomplete until an end marker replaces it.
    start_edge = next(e for e in edges if e["from"] == "n_start" and e["to"] == "n_identity_gate")
    x, y = pos("n_identity_gate", dx=-130, dy=90)
    nodes.append(_marker(START_MARKER, "Summary: call started (silent)",
                         {"visit_status": "incomplete"}, x=x, y=y))
    _repoint(start_edge, START_MARKER)
    edges.append(_edge(START_MARKER, "n_identity_gate"))

    # 2. Explicit address confirmation → address_verified = yes.
    yes_edge = next(
        e for e in edges
        if e["from"] == "n_confirm_address" and e["to"] == "n_ask_visit_date"
        and str(e.get("label") or "").startswith("yes")
    )
    x, y = pos("n_ask_visit_date", dx=-130, dy=-90)
    nodes.append(_marker(ADDRESS_OK_MARKER, "Summary: address confirmed (silent)",
                         {"address_verified": "yes"}, x=x, y=y))
    _repoint(yes_edge, ADDRESS_OK_MARKER)
    edges.append(_edge(ADDRESS_OK_MARKER, "n_ask_visit_date"))
    # The existing mismatch marker records the denial and the outcome.
    mismatch = by_id["n_mark_address_mismatch"]
    mismatch.setdefault("config", {}).setdefault("setSlots", {}).update(
        {"address_verified": "no", "visit_status": "address_mismatch"}
    )

    # 3. "Change the date": forget the round-1 day/time, then let the second
    #    ask capture the final values into the same slots (overwrite).
    no_edge = next(
        e for e in edges
        if e["from"] == "n_final_confirm" and e["to"] == "n_ask_visit_date_2"
    )
    x, y = pos("n_ask_visit_date_2", dx=-130, dy=-90)
    nodes.append(_marker(RESET_MARKER, "Summary: date being changed (silent)",
                         {"visit_day": "", "visit_time": ""}, x=x, y=y))
    _repoint(no_edge, RESET_MARKER)
    edges.append(_edge(RESET_MARKER, "n_ask_visit_date_2"))
    ask2 = by_id["n_ask_visit_date_2"]["config"]
    captures = list(ask2.get("alsoCapture") or [])
    time_entity = next(
        c["entity"] for c in captures if c.get("variable") == "visit_time_2"
    )
    captures.append({"variable": "visit_day", "overwrite": True,
                     "entity": copy.deepcopy(ask2["entity"])})
    captures.append({"variable": "visit_time", "overwrite": True,
                     "entity": copy.deepcopy(time_entity)})
    ask2["alsoCapture"] = captures

    # 4. Outcome markers right before each end node.
    for end_id, marker_id, slots in END_MARKERS:
        x, y = pos(end_id, dx=-130, dy=90)
        nodes.append(_marker(marker_id, f"Summary: {slots['visit_status']} (silent)",
                             slots, x=x, y=y))
        for e in edges:
            if e["to"] == end_id:
                _repoint(e, marker_id)
        edges.append(_edge(marker_id, end_id))

    return nodes, edges


# ── visit day/time validation (stage 2) ──────────────────────────────────────

VALIDATION_REPLIES = {
    "day_unclear": {
        "hi": "मुझे date ठीक से समझ नहीं आई। आप दिन बताइए, जैसे कल, परसों, या कोई तारीख।",
        "en": "I did not quite catch the date. Please tell me the day, for example tomorrow, the day after, or a specific date.",
    },
    "day_missing": {
        "hi": "समय note कर लिया। और visit किस दिन करवाना चाहेंगे, जैसे कल या परसों?",
        "en": "Noted the time. And on which day would you like the visit, for example tomorrow or the day after?",
    },
    "day_past": {
        "hi": "ये date निकल चुकी है। आप आने वाली कोई date बताइए, जैसे कल या परसों।",
        "en": "That date has already passed. Please tell me an upcoming date, for example tomorrow or the day after.",
    },
    "day_invalid": {
        "hi": "ये date सही नहीं लग रही। आप date फिर से बताइए, जैसे कल या परसों।",
        "en": "That does not seem to be a valid date. Please tell me the date again, for example tomorrow or the day after.",
    },
    "time_past": {
        "hi": "आज का वो समय निकल चुका है। आप आज का कोई बाद का समय बताइए, या कोई और दिन।",
        "en": "That time today has already passed. Please tell me a later time today, or another day.",
    },
    "time_ambiguous": {
        "hi": "ये समय सुबह का है या शाम का? आप बताइए, जैसे शाम 4 बजे।",
        "en": "Is that in the morning or in the evening? Please tell me, for example 4 PM.",
    },
    "time_unclear": {
        "hi": "ये समय समझ नहीं आया। आप समय फिर से बताइए, जैसे शाम 4 बजे।",
        "en": "I could not understand that time. Please tell me the time again, for example 4 PM.",
    },
    "time_invalid": {
        "hi": "ये समय समझ नहीं आया। आप समय फिर से बताइए, जैसे शाम 4 बजे।",
        "en": "I could not understand that time. Please tell me the time again, for example 4 PM.",
    },
}

# ask node → (day slots, time slots): the first is the value checked, all are
# cleared on a rejection (the second date ask mirrors into visit_day/visit_time).
VALIDATED_ASKS = {
    "n_ask_visit_date": (["visit_day"], ["visit_time"]),
    "n_ask_visit_date_2": (["visit_day_2", "visit_day"], ["visit_time_2", "visit_time"]),
    "n_today_capture": (["visit_day"], ["visit_time"]),
    "n_today_capture_2": (["visit_day"], ["visit_time"]),
}


def validation_config(day_vars: list[str], time_vars: list[str]) -> dict:
    return {
        "type": "future_datetime", "timezone": "Asia/Kolkata",
        "dayVariables": list(day_vars), "timeVariables": list(time_vars),
        "replyByLanguage": copy.deepcopy(VALIDATION_REPLIES),
    }


# The authored retry prompt names a fixed example date ("10 October") that
# has since passed — and that the validation would now refuse. Replace it
# with a relative example that never rots. (Spoken text change, this bot only.)
EXAMPLE_DATE_FIX = {
    "जैसे कल, या 10 अक्टूबर।": "जैसे कल, या परसों।",
    "For example, tomorrow or 10 October.": "For example, tomorrow or the day after.",
}


def _fix_example_dates(node: dict, reverse: bool = False) -> None:
    config = node.get("config") or {}
    pairs = [(b, a) for a, b in EXAMPLE_DATE_FIX.items()] if reverse else list(EXAMPLE_DATE_FIX.items())
    for key in ("unmatchedReply",):
        if isinstance(config.get(key), str):
            for old, new in pairs:
                config[key] = config[key].replace(old, new)
    by_lang = config.get("unmatchedReplyByLanguage")
    if isinstance(by_lang, dict):
        for lang, text in list(by_lang.items()):
            if isinstance(text, str):
                for old, new in pairs:
                    text = text.replace(old, new)
                by_lang[lang] = text


def add_datetime_validation(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Stage 2: declare ``validation`` on the date-capturing asks and fix the
    rotting example date in their retry prompts. Idempotent; requires the
    stage-1 markers (the reset marker clears the mirrored slots)."""
    nodes = copy.deepcopy(nodes)
    edges = copy.deepcopy(edges)
    by_id = {str(n.get("id")): n for n in nodes}
    if START_MARKER not in by_id:
        raise ValueError("stage 1 (summary markers) must be applied first")
    for node_id, (day_vars, time_vars) in VALIDATED_ASKS.items():
        node = by_id.get(node_id)
        if node is None:
            raise ValueError(f"baseline node {node_id!r} missing — not the expected workflow")
        node.setdefault("config", {})["validation"] = validation_config(day_vars, time_vars)
        _fix_example_dates(node)
    return nodes, edges


def strip_stage2(nodes: list[dict]) -> list[dict]:
    """Stage-2 content with the validation and example-date fix undone — used
    to recognise an earlier stage-2 save (e.g. an older reply set) as upgradable."""
    nodes = copy.deepcopy(nodes)
    for n in nodes:
        if n.get("id") in VALIDATED_ASKS:
            (n.get("config") or {}).pop("validation", None)
            _fix_example_dates(n, reverse=True)
    return nodes


def has_validation(nodes: list[dict]) -> bool:
    return all(
        isinstance((n.get("config") or {}).get("validation"), dict)
        for n in nodes if n.get("id") in VALIDATED_ASKS
    )


# ── spoken-number time capture (stage 3) ─────────────────────────────────────
#
# Why a rebuilt regex: Sarvam writes Hindi numbers as words on live calls
# ("कल सुबह नौ बजे", "कल सुबह दस बजे", "ढाई बजे"), and the authored time regex
# matched the bare period word first, so the extractor's number-word rewrite
# never ran and the hour was lost. Every alternative below is anchored (a
# period before, a fraction before, बजे/baje/am/pm after, "at" before), so a
# number inside a date ("25 October") is never taken for a time.

import re as _re
from shared.orchestration import visit_datetime as _vd


def _guarded(words) -> str:
    parts = []
    for w in sorted(words, key=len, reverse=True):
        esc = _re.escape(w).replace(r"\ ", r"\s+")
        if _re.search(r"[\u0900-\u097f]", w):
            parts.append(rf"(?<![\u0900-\u097f]){esc}(?![\u0900-\u097f])")
        else:
            parts.append(rf"\b{esc}\b")
    return "(?:" + "|".join(parts) + ")"


PERIOD_SURFACE = [w for words in _vd.PERIOD_WORDS.values() for w in words
                  if w != "noon"] + ["सवेरे", "savere", "subha"]


def build_time_regex() -> str:
    period = _guarded(PERIOD_SURFACE)
    ko = r"(?:\s+(?:ko|को|mein|में|me))?"
    hour_digit = r"(?<![\d:.])\d{1,2}(?:[:.]\d{2})?(?![\d])"
    hour = f"(?:{hour_digit}|{_guarded(_vd.HOUR_WORDS)})"
    frac = _guarded(_vd.FRACTION_WORDS)
    special = _guarded(_vd.SPECIAL_TIMES)
    min_en = r"(?:\s+(?:thirty|fifteen|forty[\s-]?five))?"
    ampm = r"(?:\s*(?<![a-z])[ap]\.?\s?m\.?(?![a-z]))"
    suffix = rf"(?:\s*(?:बजे|बजकर|baje|bje|o'?\s?clock|hrs)){ampm}?|{ampm}"
    trail = rf"(?:\s+(?:in\s+the\s+)?{period}{ko})?"
    lead = rf"{period}\s+(?:ko\s+|को\s+|ke\s+|के\s+)?"
    alternatives = [
        rf"{lead}(?:{frac}\s+)?{hour}{min_en}(?:{suffix})?{trail}",     # सुबह नौ बजे / subah 10
        rf"{frac}\s+{hour}{min_en}(?:{suffix})?{trail}",                # साढ़े चार बजे / half past four
        rf"(?:{lead})?{special}(?:{suffix})?{trail}",                    # ढाई बजे / दोपहर डेढ़ बजे
        rf"{hour}{min_en}(?:{suffix}){trail}",                           # 4 PM / nau baje subah
        rf"{hour}{min_en}\s+(?:in\s+the\s+)?{period}{ko}",            # 10 in the morning / nau sham ko
        rf"(?<=\bat ){hour}{min_en}(?:{suffix})?{trail}",                 # (at) four
        rf"{_guarded(_vd.HOUR_WORDS_EN)}\s+(?:thirty|fifteen|forty[\s-]?five)\b",  # four thirty
        r"(?<![\d:.])(?:1[3-9]|2[0-3])[:.][0-5]\d(?![\d])",            # 16:30
        rf"{period}(?!\s+(?:ko\s+|को\s+|ke\s+|के\s+)?(?:{frac}|{hour}|{special}))|(?<![a-z])[ap]\.?\s?m\.?(?![a-z])",  # bare period / PM
    ]
    return "(?i)(" + "|".join(alternatives) + ")"


_OLD_TARIKH = r"\b\d{1,2}\s*(?:tarikh|taarikh|tareekh|tarik)\b"


def _new_tarikh() -> str:
    words = "|".join(sorted((_re.escape(w) for w in _vd.DAY_WORDS_ROMAN), key=len, reverse=True))
    return rf"\b(?:\d{{1,2}}|{words})\s*(?:tarikh|taarikh|tareekh|tarik)\b"


TIME_CAPTURE_VARIABLES = {"visit_time", "visit_time_2", "callback_time"}


def improve_time_capture(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Stage 3: rebuild the time regexes from the parser vocabulary, let the
    day regex read romanised day-of-month words ("pachees tarikh"), and make
    the reset marker also forget a parked ambiguous hour. Idempotent."""
    nodes = copy.deepcopy(nodes)
    edges = copy.deepcopy(edges)
    by_id = {str(n.get("id")): n for n in nodes}
    if not has_validation(nodes):
        raise ValueError("stage 2 (validation) must be applied first")
    time_regex = build_time_regex()
    for node_id in list(VALIDATED_ASKS) + ["n_ask_callback"]:
        node = by_id.get(node_id)
        if node is None:
            raise ValueError(f"baseline node {node_id!r} missing — not the expected workflow")
        config = node.setdefault("config", {})
        for capture in config.get("alsoCapture") or []:
            if capture.get("variable") in TIME_CAPTURE_VARIABLES:
                capture.setdefault("entity", {})["regexPattern"] = time_regex
        entity = config.get("entity") or {}
        if isinstance(entity.get("regexPattern"), str) and _OLD_TARIKH in entity["regexPattern"]:
            entity["regexPattern"] = entity["regexPattern"].replace(_OLD_TARIKH, _new_tarikh())
    reset = by_id[RESET_MARKER]
    reset["config"]["setSlots"].update({"visit_time_pending": "", "visit_time_2_pending": ""})
    return nodes, edges


def has_time_capture(nodes: list[dict]) -> bool:
    regex = build_time_regex()
    return all(
        any(c.get("variable") in TIME_CAPTURE_VARIABLES
            and (c.get("entity") or {}).get("regexPattern") == regex
            for c in ((n.get("config") or {}).get("alsoCapture") or []))
        for n in nodes if n.get("id") in VALIDATED_ASKS
    )


# ── confirmation wording (stage 4) ───────────────────────────────────────────
#
# What the caller heard on live call cv_37ce8d991411: "… और हमारे engineer
# आपके यहाँ 17 अक्टूबर शाम आएँगे। क्या हम आगे बढ़ें?" — stilted, and silent
# about the fact that the engineer still confirms the exact time. The
# templates below keep every placeholder the renderer fills ({locality},
# {new_address}, the day/time slots) and the group rules (a group applies
# only when its ``requires`` slots are filled and its ``absent`` slots are
# empty), so a day-only booking never invents a time.

CONFIRM_NODES = {"n_final_confirm": ("visit_day", "visit_time"),
                 "n_final_confirm_2": ("visit_day_2", "visit_time_2")}
CONFIRM_PROMPT = "Let me confirm the details. Does that work for you?"
YES_TOKENS_EXTRA = ["ठीक रहेगा", "हाँ ठीक रहेगा", "ठीक है जी", "चलेगा", "हाँ चलेगा", "सही है", "हाँ सही है",
                    "theek rahega", "thik rahega", "haan theek rahega", "chalega", "haan chalega",
                    "sahi hai", "haan sahi hai", "that works", "works for me", "that's fine", "fine"]
NO_TOKENS_EXTRA = ["ठीक नहीं रहेगा", "नहीं चलेगा", "theek nahi rahega", "nahi chalega",
                   "doesn't work", "does not work", "not fine"]


def build_readback(day: str, time: str) -> dict:
    hi_at = "आपके registered address {locality} पर"
    hi_new = "आपके बताए हुए पते {new_address} पर"
    en_at = "to your registered address in {locality}"
    en_new = "to {new_address}"
    hi_time_note = "visit का exact समय engineer आपको call करके बताएँगे।"
    en_time_note = "The engineer will call you to confirm the exact time."

    def hi(where, with_time):
        when = f"{{{day}}} {{{time}}}" if with_time else f"{{{day}}}"
        line = f"हमारे engineer {when} {where} Airtel Broadband device के uninstallation और pickup के लिए आएँगे।"
        return line if with_time else f"{line} {hi_time_note}"

    def en(where, with_time):
        when = f"{{{day}}} at {{{time}}}" if with_time else f"{{{day}}}"
        line = f"Our engineer will come {where} {when} for the Airtel Broadband device uninstallation and pickup."
        return line if with_time else f"{line} {en_time_note}"

    def groups(render):
        return [
            {"absent": [], "requires": ["new_address", day, time], "template": render(True, True)},
            {"absent": [time], "requires": ["new_address", day], "template": render(True, False)},
            {"absent": ["new_address"], "requires": [day, time], "template": render(False, True)},
            {"absent": ["new_address", time], "requires": [day], "template": render(False, False)},
        ]
    return {
        "en": {"intro": "Let me confirm the details.",
               "groups": groups(lambda new, t: en(en_new if new else en_at, t)),
               "question": "Does that work for you?"},
        "hi": {"intro": "ठीक है, एक बार confirm कर लेते हैं।",
               "groups": groups(lambda new, t: hi(hi_new if new else hi_at, t)),
               "question": "क्या ये आपके लिए ठीक रहेगा?"},
    }


def _extend_label(label: str, extra: list[str]) -> str:
    tokens = [t.strip() for t in str(label or "").split("/") if t.strip()]
    for t in extra:
        if t not in tokens:
            tokens.append(t)
    return "/".join(tokens)


def refine_confirmation_wording(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Stage 4: natural confirmation readback on both confirm nodes and the
    natural answers to it on their yes/no edges. Idempotent."""
    nodes = copy.deepcopy(nodes)
    edges = copy.deepcopy(edges)
    by_id = {str(n.get("id")): n for n in nodes}
    if not has_time_capture(nodes):
        raise ValueError("stage 3 (time capture) must be applied first")
    for node_id, (day, time) in CONFIRM_NODES.items():
        node = by_id.get(node_id)
        if node is None:
            raise ValueError(f"baseline node {node_id!r} missing — not the expected workflow")
        node.setdefault("config", {})["readback"] = build_readback(day, time)
        node["config"]["prompt"] = CONFIRM_PROMPT
    for e in edges:
        if e.get("from") not in CONFIRM_NODES:
            continue
        if e.get("to") == "n_mark_booked":
            e["label"] = _extend_label(e.get("label"), YES_TOKENS_EXTRA)
        elif e.get("to") in (RESET_MARKER, "n_mark_date_pending"):
            e["label"] = _extend_label(e.get("label"), NO_TOKENS_EXTRA)
    return nodes, edges


def has_confirmation_wording(nodes: list[dict]) -> bool:
    return all(
        (n.get("config") or {}).get("readback") == build_readback(*CONFIRM_NODES[n["id"]])
        for n in nodes if n.get("id") in CONFIRM_NODES
    )


def normalise_to_stage1(nodes: list[dict], edges: list[dict]) -> tuple[list[dict], list[dict]]:
    """Undo everything stages 2-4 may have written (validation, retry prompt
    text, capture regexes, pending slots, readback/prompt, edge tokens) by
    restoring those keys from the stage-1 output. A saved workflow from any
    earlier stage then equals the stage-1 output; anything else differs."""
    base = load_baseline()
    s1_nodes, s1_edges = upgrade_workflow(base["nodes"], base["edges"])
    ref_nodes = {n["id"]: n for n in s1_nodes}
    ref_labels = {(e["from"], e["to"]): e.get("label") for e in s1_edges}
    nodes = copy.deepcopy(nodes)
    edges = copy.deepcopy(edges)
    for n in nodes:
        ref = ref_nodes.get(n.get("id"))
        if ref is None:
            continue
        cfg = n.setdefault("config", {})
        rcfg = ref.get("config") or {}
        cfg.pop("validation", None)
        for key in ("unmatchedReply", "unmatchedReplyByLanguage", "entity", "alsoCapture",
                    "readback", "prompt"):
            if key in rcfg:
                cfg[key] = copy.deepcopy(rcfg[key])
        if n.get("id") == RESET_MARKER and "setSlots" in rcfg:
            cfg["setSlots"] = copy.deepcopy(rcfg["setSlots"])
    for e in edges:
        if e.get("from") in CONFIRM_NODES and (e.get("from"), e.get("to")) in ref_labels:
            e["label"] = ref_labels[(e["from"], e["to"])]
    return nodes, edges


def load_baseline() -> dict:
    with open(BASELINE_PATH, encoding="utf-8") as f:
        return json.load(f)


TARGET_STAGE = 4


def upgraded_definition(version: int = BASELINE_VERSION + 1, *, stage: int = TARGET_STAGE) -> dict:
    """The upgraded definition in the engine's shape (tests + --dump):
    stage 1 = summary markers (v13), stage 2 = + day/time validation (v14/v15),
    stage 3 = + spoken-number time capture (v16), stage 4 = + confirmation wording."""
    base = load_baseline()
    nodes, edges = upgrade_workflow(base["nodes"], base["edges"])
    if stage >= 2:
        nodes, edges = add_datetime_validation(nodes, edges)
    if stage >= 3:
        nodes, edges = improve_time_capture(nodes, edges)
    if stage >= 4:
        nodes, edges = refine_confirmation_wording(nodes, edges)
    return {"id": base["id"], "version": version, "name": base["name"],
            "nodes": nodes, "edges": edges}


def describe(old_nodes, old_edges, new_nodes, new_edges) -> list[str]:
    old_ids = {n["id"] for n in old_nodes}
    lines = [f"nodes {len(old_nodes)} → {len(new_nodes)}, edges {len(old_edges)} → {len(new_edges)}"]
    for n in new_nodes:
        if n["id"] not in old_ids:
            lines.append(f"  + {n['id']}: setSlots {json.dumps(n['config']['setSlots'], ensure_ascii=False)}")
    old_by_id = {n["id"]: n for n in old_nodes}
    for n in new_nodes:
        o = old_by_id.get(n["id"])
        if o is not None and o != n:
            new_cfg, old_cfg = n.get("config") or {}, o.get("config") or {}
            changed_keys = sorted(k for k in set(new_cfg) | set(old_cfg) if new_cfg.get(k) != old_cfg.get(k))
            if "validation" in changed_keys:
                v = new_cfg["validation"]
                lines.append(f"  ~ {n['id']}: validation {v['type']} {v['timezone']} "
                             f"day={v['dayVariables']} time={v['timeVariables']} "
                             f"replies={sorted(v['replyByLanguage'])}")
            other = [k for k in changed_keys if k != "validation"]
            if other:
                lines.append(f"  ~ {n['id']}: text changed in {other}")
    old_e = {(e["from"], e["to"], e.get("label")) for e in old_edges}
    new_e = {(e["from"], e["to"], e.get("label")) for e in new_edges}
    old_pairs = {(e[0], e[1]) for e in old_e}
    new_pairs = {(e[0], e[1]) for e in new_e}
    for e in sorted(old_e - new_e, key=str):
        if (e[0], e[1]) in new_pairs:
            lines.append(f"  ~ edge {e[0]} → {e[1]}: label tokens extended")
        else:
            lines.append(f"  - edge {e[0]} → {e[1]}" + (f" [{str(e[2])[:30]}…]" if e[2] else ""))
    for e in sorted(new_e - old_e, key=str):
        if (e[0], e[1]) not in old_pairs:
            lines.append(f"  + edge {e[0]} → {e[1]}" + (f" [{str(e[2])[:30]}…]" if e[2] else ""))
    return lines


# ── CLI ──────────────────────────────────────────────────────────────────────

def _client(base: str):
    import httpx

    email = os.environ.get("JEEVES_SETUP_EMAIL")
    password = os.environ.get("JEEVES_SETUP_PASSWORD")
    if not email or not password:
        sys.exit("set JEEVES_SETUP_EMAIL / JEEVES_SETUP_PASSWORD (a Jeeves tenant admin)")
    c = httpx.Client(base_url=base, timeout=60)
    r = c.post("/auth/login", json={"email": email, "password": password})
    if r.status_code >= 300:
        sys.exit(f"login failed: {r.status_code} {r.text[:300]}")
    c.headers["Authorization"] = f"Bearer {r.json()['data']['token']}"
    return c


def _check(r, what: str):
    if r.status_code >= 300:
        sys.exit(f"FAIL {what}: {r.status_code} {r.text[:500]}")
    print(f"ok   {what}")
    return r.json().get("data")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--base", default=os.environ.get("ECHOSPHERE_API_BASE", "http://127.0.0.1:9001/api/v1"))
    parser.add_argument("--dump", metavar="PATH", help="write the upgraded definition JSON and exit")
    parser.add_argument("--allow-version", type=int, default=None,
                        help="also accept this live workflow version as the baseline")
    args = parser.parse_args(argv)

    if args.dump:
        with open(args.dump, "w", encoding="utf-8") as f:
            json.dump(upgraded_definition(), f, ensure_ascii=False, indent=1)
        print(f"wrote {args.dump}")
        return

    c = _client(args.base)
    wf = _check(c.get(f"/bots/{BOT_ID}/workflow"), f"read workflow of {BOT_ID}")
    nodes, edges = wf.get("nodes") or [], wf.get("edges") or []
    print(f"     {wf.get('id')} v{wf.get('version')} status={wf.get('status')} "
          f"({len(nodes)} nodes / {len(edges)} edges)")
    if wf.get("id") != WORKFLOW_ID:
        sys.exit(f"unexpected workflow id {wf.get('id')!r}")
    baseline = load_baseline()
    stage1 = upgraded_definition(stage=1)
    target = upgraded_definition(stage=TARGET_STAGE)
    at_baseline = nodes == baseline["nodes"] and edges == baseline["edges"]
    at_target = nodes == target["nodes"] and edges == target["edges"]
    # Any earlier stage (or an older wording of one) normalises back to the
    # stage-1 output; a workflow edited elsewhere does not, and is refused.
    norm_nodes, norm_edges = normalise_to_stage1(nodes, edges)
    at_known_stage = norm_nodes == stage1["nodes"] and norm_edges == stage1["edges"]
    if at_target:
        print(f"     workflow already at stage {TARGET_STAGE} — nothing to change")
    elif not (at_baseline or at_known_stage):
        sys.exit("workflow content matches neither the committed v12 baseline nor a known "
                 "upgrade stage — refusing to upgrade blindly")
    else:
        if at_baseline:
            expected = {BASELINE_VERSION, args.allow_version}
            if wf.get("version") not in expected:
                sys.exit(f"workflow is v{wf.get('version')}, baseline is v{BASELINE_VERSION}; "
                         "re-export the baseline (or pass --allow-version) before upgrading")
        new_nodes, new_edges = target["nodes"], target["edges"]
        print("     applying:", f"stages 1-{TARGET_STAGE} (markers, validation, time capture, wording)"
              if at_baseline else f"upgrade to stage {TARGET_STAGE} on top of the saved stage")
        for line in describe(nodes, edges, new_nodes, new_edges):
            print("    ", line)
        if args.apply:
            saved = _check(c.put(f"/bots/{BOT_ID}/workflow", json={
                "nodes": new_nodes, "edges": new_edges, "status": wf.get("status") or "approved",
            }), "save upgraded workflow")
            print(f"     now v{saved.get('version')} status={saved.get('status')} "
                  f"issues={saved.get('issues')}")

    settings = _check(c.get(f"/bots/{BOT_ID}/voice-settings"), "read voice settings")
    policy = dict(settings.get("goalPolicy") or {})
    current = policy.get("summaryFields")
    if current == SUMMARY_FIELDS:
        print("     summaryFields already in place — nothing to change")
    else:
        print(f"     goalPolicy keys now: {sorted(policy) or '(none)'}; "
              f"summaryFields: {len(current or [])} → {len(SUMMARY_FIELDS)}")
        for spec in SUMMARY_FIELDS:
            print(f"       {spec['name']:<22} {spec['type']:<7} ← {spec['source']}"
                  + (f"  requires {spec['requires']}" if spec.get("requires") else ""))
        if args.apply:
            policy["summaryFields"] = SUMMARY_FIELDS
            _check(c.put(f"/bots/{BOT_ID}/voice-settings", json={"goalPolicy": policy}),
                   f"goalPolicy.summaryFields ({len(SUMMARY_FIELDS)})")
    if not args.apply:
        print("dry run — re-run with --apply to write")


if __name__ == "__main__":
    main()
