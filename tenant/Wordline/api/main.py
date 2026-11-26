"""Wordline merchant data API — standalone tenant service.

Serves the merchant terminal records converted from ``Wordline.csv``
(``data/merchants.json``) to the Wordline voice bot.  It is a *tenant*
module: nothing in the platform imports it.  The platform reaches it the
same way it reaches any customer system — through an ``api_connections``
row executed by a workflow ``api`` node (``setup/01_api_connection.py``).

Endpoints (all under ``/api/v1``; every data endpoint needs ``X-API-Key``):

    GET  /health                         liveness + record count (no key)
    POST /merchants/lookup               resolve a merchant from workflow slots,
                                         or one explicit ``search_by`` + ``query``
    GET  /merchants/{terminal_id}        exact / digit-key terminal lookup
    GET  /merchants/by-merchant-id/{mid} exact / digit-key merchant id lookup
    GET  /merchants/search?name=…        merchant-name search, every match
    GET  /merchants/by-contact/{number}  registered contact number lookup
    GET  /merchants                      filtered listing, contact numbers masked,
                                         no address / contact person

Three independent searches (2026-11-26, ``Wordline_with_distance.csv``):

* **Terminal ID** (``ANCHORAPPTID``) — exact, case-insensitive, separators
  ignored; the digit key when the STT lost the letter (unchanged behaviour);
* **Merchant ID** (``ANCHORAPPMID``) — exact the same way; the digit key only
  when the caller gave no letter at all;
* **Merchant name** (``MERCHANT``) — case-insensitive, partial (exact →
  partial → phonetic tiers, ``store.MerchantStore.find_name``).  A name that
  matches several merchants is answered ``lookup_status: "multiple"`` with
  every candidate; the service never picks one.

Explicit form — one search, nothing else consulted::

    {"search_by": "terminal_id" | "merchant_id" | "merchant_name", "query": "…"}

(``search_by`` also accepts the CSV column names ``ANCHORAPPTID`` /
``ANCHORAPPMID`` / ``MERCHANT``).  An unknown ``search_by``, an empty query
or a value that cannot be that identifier is ``422`` ``invalid_query``.

``POST /merchants/lookup`` is what the workflow calls.  The executor posts
the whole scalar slot state, so the body is a loose object; the keys are
tried in this order and the first one that resolves (one merchant, or
several for a name) decides: ``terminal_id_retry``,
``spoken_terminal_id_retry``, ``terminal_id``, ``spoken_terminal_id``,
``ANCHORAPPTID``, ``tid``; then the merchant id keys ``merchant_id``,
``mid``, ``ANCHORAPPMID``; then the name keys ``merchant_name``,
``MERCHANT``; then the bot's free-text "MID and outlet name" answers
``pr_merchant_outlet_retry``, ``bat_merchant_outlet_retry``,
``pr_merchant_outlet``, ``bat_merchant_outlet``; then ``contact_number``,
``phone``, ``caller_phone``, ``mobile``.  A free-text answer is searched in
two steps: the id dictated inside it (10+ digits = merchant id, 4-9 =
terminal id) and, when that finds nothing, the rest of the answer as an
outlet name (with the *words* tier, see ``store.py``).  Bodies with only
Terminal ID / contact keys behave exactly as before.
Values may be dictated ("9 1 3 7 9 7 8 4") and may be unresolved templates
(``{{spoken_terminal_id_retry}}``), which are ignored.  ``tid`` (lowest
precedence) is what the platform's connection "Test" button fills: the
connection's body template sets it from the ``{{customer_phone}}`` test value,
which is never a live call-context key, so on real calls it arrives as the
literal placeholder and is ignored.

Response contract — this matters for the workflow:

* a hit is ``200`` with ``lookup_status: "found"``, ``found: true`` /
  ``verified: true`` plus the merchant fields the connection maps into
  workflow variables — ``terminal_id``, ``outlet_name``, ``bank_name``,
  ``asset_type``, ``contact_number``, ``address``, ``contact_person`` …;
  ``outlet_name``, ``asset_type``, ``address`` and ``contact_person`` come
  back TTS-ready (``api/speech.py``: "Agarwal Public School", "Paytivo 7210
  2G plus WiFi", "…, Indore, Madhya Pradesh 452016", "K C Tiwari") because
  the bot speaks them verbatim and the TTS spells ALL-CAPS words letter by
  letter.  The stored JSON keeps the CSV text;
* several merchants for a name are ``200`` with ``lookup_status:
  "multiple"``, ``ambiguous: true``, ``found_count``, ``candidates`` (id,
  merchant id, outlet, bank, issue, distance — no contact details) and
  ``candidates_spoken`` (up to three TTS-ready names for the bot to read),
  again with no ``found`` / ``verified`` / merchant keys;
* a miss is ``200`` with ``lookup_status: "not_found"`` and **no** ``found``
  / ``verified`` / merchant keys.  A lookup that finds nothing is a *completed*
  query, not a failed request: the platform (Studio "Send test request",
  the tenant dashboard's "failing API" recommendation) reads any non-2xx as
  "endpoint failing", and the Studio test can only send the fixed sample
  value ``+10000000000`` for ``{{customer_phone}}``, which no terminal
  matches.  A 404 miss therefore flagged a healthy service as failing on
  every test click (DEMO, 2026-11-07).  The workflow never routes on the
  status: the engine applies mapped fields with ``setdefault`` and keeps the
  first value for the rest of the call, so a miss must not write ``found:
  false`` (the retry lookup could then never set ``found: true``); the api
  node takes its *success* edge and the following condition node
  (``merchant_found equals true``) routes hit vs. miss.  The connection's
  success condition is ``status<400`` and its *response schema* requires
  ``lookup_status`` to be ``found``, ``multiple`` or ``not_found``, so every
  other body —
  ``401``/``403`` (``unauthorized``), ``422`` (``no_identifier``), ``429``, a
  proxy's non-JSON page, a foreign 200 — fails validation and takes the
  *failure* edge together with transport errors, timeouts and ``5xx``;
* a body without any usable identifier is ``422`` (``lookup_status:
  "no_identifier"``) — the unavailable branch, which cannot happen from the
  workflow (it always posts the Terminal ID slots);
* the resource routes ``GET /merchants/{id}`` and ``/merchants/by-contact/``
  keep REST semantics and answer ``404`` for an unknown id.

Security: binds to loopback (``run.sh``), constant-time API-key check,
``503`` while no key is configured (fail closed), contact numbers never
logged, listing responses masked.

Run:  ./tenant/Wordline/run.sh        (port 9023; WORDLINE_MERCHANT_API_KEY
from the environment, else from the repo ``.env``)
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from tenant.Wordline.api.speech import spoken_address, spoken_model, spoken_name
from tenant.Wordline.api.store import (
    MAX_NAME_CANDIDATES, MerchantStore, Match, NameMatches, clean_value, identifier_in_text,
    mask_contact, merchant_id_keys, terminal_keys, without_identifier)

API_KEY_ENV = "WORDLINE_MERCHANT_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[3]
TERMINAL_KEYS = ("terminal_id_retry", "spoken_terminal_id_retry", "terminal_id",
                 "spoken_terminal_id", "ANCHORAPPTID", "tid")
MERCHANT_ID_KEYS = ("merchant_id", "mid", "ANCHORAPPMID")
NAME_KEYS = ("merchant_name", "MERCHANT")
# The bot's answers to "your MID and your outlet name" (workflow v57+): the
# second answer (after "several merchants match") before the first.
FREE_TEXT_KEYS = ("pr_merchant_outlet_retry", "bat_merchant_outlet_retry",
                  "pr_merchant_outlet", "bat_merchant_outlet")
CONTACT_KEYS = ("contact_number", "phone", "caller_phone", "mobile")
SEARCH_BY = {
    "terminal_id": "terminal_id", "tid": "terminal_id", "anchorapptid": "terminal_id",
    "merchant_id": "merchant_id", "mid": "merchant_id", "anchorappmid": "merchant_id",
    "merchant_name": "merchant_name", "name": "merchant_name", "merchant": "merchant_name",
}
MERCHANT_FIELDS = ("terminal_id", "merchant_id", "outlet_name", "bank_name", "asset_type",
                   "contact_number", "issue_type", "issue_code", "distance")
CANDIDATE_FIELDS = ("terminal_id", "merchant_id", "outlet_name", "bank_name",
                    "issue_type", "distance")
SPOKEN_CANDIDATES = 3
# Returned by the single-merchant views only (lookup, GET by id / contact):
# the bot needs them for the visit / request-raising steps; the bulk listing
# does not carry personal details.
VISIT_FIELDS = ("address", "contact_person")

logger = logging.getLogger("wordline.merchant_api")
if not logger.handlers:  # standalone service: INFO lines to stderr next to uvicorn's
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

app = FastAPI(title="Wordline Merchant Data API", version="1.2.0",
              docs_url=None, redoc_url=None, openapi_url=None)
store = MerchantStore()


# ── auth ─────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _key_from_dotenv() -> str:
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover — python-dotenv is a platform dep
        return ""
    return str(dotenv_values(REPO_ROOT / ".env").get(API_KEY_ENV) or "").strip()


def configured_api_key() -> str:
    return os.environ.get(API_KEY_ENV, "").strip() or _key_from_dotenv()


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


async def require_api_key(x_api_key: str | None = Header(default=None)):
    expected = configured_api_key()
    if not expected:
        raise _AuthProblem(503, "API key is not configured on the service.")
    if not x_api_key or not hmac.compare_digest(x_api_key.strip(), expected):
        raise _AuthProblem(401, "A valid X-API-Key header is required.")


class _AuthProblem(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message


@app.exception_handler(_AuthProblem)
async def _auth_problem(_request: Request, exc: _AuthProblem):
    return _err(exc.status, exc.message, lookup_status="unauthorized")


# ── views ────────────────────────────────────────────────────────────────────


def spoken_fields(record: dict) -> dict:
    """The four fields the bot reads out, shaped for TTS (``api/speech.py``).

    ``outlet_name`` (MERCHANT), ``asset_type`` (ASSETMODEL), ``address`` and
    ``contact_person``: ALL-CAPS words re-cased, acronyms / model numbers
    kept, abbreviations expanded, the export's address labels and repeats
    removed.  The stored record (``data/merchants.json``) keeps the CSV text.
    An absent address / contact person is ``None`` — the connection maps
    nothing for it, so the workflow's ``exists`` checks stay false.
    """
    person = spoken_name(record.get("contact_person"))
    return {
        "outlet_name": spoken_name(record.get("outlet_name")) or record.get("outlet_name"),
        "asset_type": spoken_model(record.get("asset_type")) or record.get("asset_type"),
        "address": spoken_address(record.get("address"), record.get("contact_person")) or None,
        "contact_person": person or None,
    }


def merchant_view(match: Match) -> dict:
    record = match.record
    spoken = spoken_fields(record)
    view = {"lookup_status": "found", "found": True, "verified": True,
            "matched_by": match.matched_by}
    view.update({k: spoken.get(k, record.get(k)) for k in MERCHANT_FIELDS})
    view["merchant_id"] = record.get("merchant_id") or None  # older data: absent → unmapped
    view["contact_number_masked"] = mask_contact(str(record.get("contact_number") or ""))
    view.update({k: spoken[k] for k in VISIT_FIELDS})
    return view


def masked_view(record: dict) -> dict:
    spoken = spoken_fields(record)
    view = {k: spoken.get(k, record.get(k)) for k in MERCHANT_FIELDS if k != "contact_number"}
    view["contact_number_masked"] = mask_contact(str(record.get("contact_number") or ""))
    return view


def candidate_view(record: dict) -> dict:
    view = {k: record.get(k) for k in CANDIDATE_FIELDS}
    view["outlet_name"] = spoken_name(record.get("outlet_name")) or record.get("outlet_name")
    return view


def multiple_view(found: NameMatches, source: str) -> dict:
    """Several merchants match a name: report all of them, choose none.
    No ``found`` / ``verified`` / merchant keys — the workflow must not map a
    merchant from an ambiguous answer."""
    names = [candidate_view(r)["outlet_name"] for r in found.records]
    shown = found.records[:MAX_NAME_CANDIDATES]
    return {
        # No ``matched_by``: it is mapped to a slot (first value wins), and an
        # ambiguous answer has not matched anything yet.
        "lookup_status": "multiple", "ambiguous": True, "searched_by": "merchant_name",
        "name_match": found.tier, "searched_field": source, "query": " ".join(found.query_words),
        # ``match_count`` exists ONLY on this body: a miss carries
        # ``found_count: 0``, and the engine keeps a slot's first value, so
        # mapping ``found_count`` would pin 0 after any earlier miss.
        "found_count": len(found.records), "match_count": len(found.records),
        "candidates": [candidate_view(r) for r in shown],
        "truncated": len(found.records) > len(shown),
        "candidates_spoken": ", ".join(names[:SPOKEN_CANDIDATES]),
        "message": f"{len(found.records)} merchants match this name.",
        "hint": "Ask for the complete registered name, the Merchant ID or the Terminal ID.",
    }


def _not_found(hint: str) -> JSONResponse:
    """Unknown resource on the GET routes."""
    return _err(404, "No merchant found for the shared details.",
                lookup_status="not_found", hint=hint)


def _lookup_miss(hint: str, invalid: list[dict] | None = None) -> dict:
    """POST /lookup completed and matched nothing — a 200 with no mapped keys
    (see the module docstring for why this is not a 404).  ``invalid`` lists
    the values that could not be an identifier of their kind (and why)."""
    body = {"lookup_status": "not_found", "found_count": 0,
            "message": "No merchant found for the shared details.", "hint": hint}
    if invalid:
        body["invalid"] = invalid
    return body


_HINTS = {
    "terminal_id": "Terminal ids are 7-8 characters; check the digits dictated.",
    "merchant_id": "Merchant ids are 12-15 characters: digits with at most one letter.",
    "merchant_name": "Use the registered outlet name; a part of it is enough.",
    "contact_number": "Use the registered 10-digit mobile number.",
}


@dataclass
class _Outcome:
    """One identifier searched: a merchant, several (names only), or a miss."""
    field: str
    match: Match | None = None
    several: NameMatches | None = None
    problem: str = ""          # why the value cannot be this kind of identifier


def _free_text(value: str) -> _Outcome:
    """A caller's "MID and outlet name" answer: the id inside it first, then
    the rest as a name.  Several names are reported, never chosen."""
    kind, ident = identifier_in_text(value)
    rest = value
    if kind:
        outcome = _search(kind, ident)
        if outcome.match:
            return outcome
        rest = without_identifier(value, ident)
    found = store.find_name(rest, loose=True)
    if len(found.records) == 1:
        return _Outcome("merchant_name", match=Match(found.records[0], "merchant_name"))
    if found.records:
        return _Outcome("merchant_name", several=found)
    if kind or not found.too_short:
        return _Outcome(kind or "merchant_name")          # a completed search: miss
    return _Outcome("merchant_name", problem="no merchant id or outlet name in the answer")


def _search(field: str, value: str) -> _Outcome:
    if field == "free_text":
        return _free_text(value)
    if field == "terminal_id":
        exact, digits = terminal_keys(value)
        if not exact:
            return _Outcome(field, problem="not a terminal id")
        return _Outcome(field, match=store.find_terminal(value))
    if field == "merchant_id":
        if not merchant_id_keys(value)[0]:
            return _Outcome(field, problem="not a merchant id (10+ digits, at most one letter)")
        return _Outcome(field, match=store.find_merchant_id(value))
    if field == "merchant_name":
        found = store.find_name(value)
        if found.too_short:
            return _Outcome(field, problem="name too short (3+ letters needed)")
        if len(found.records) == 1:
            return _Outcome(field, match=Match(found.records[0], "merchant_name"))
        return _Outcome(field, several=found if found.records else None)
    return _Outcome(field, match=store.find_contact(value))


def _found(outcome: _Outcome, source: str) -> dict:
    view = merchant_view(outcome.match)
    view["searched_field"] = source
    return view


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — empty or invalid body = no identifiers
        return {}
    return payload if isinstance(payload, dict) else {}


# ── endpoints ────────────────────────────────────────────────────────────────


@app.get("/api/v1/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "wordline-merchant-api",
        "records": len(store),
        "data_loaded_at": store.loaded_at,
        "data_generated_at": store.meta.get("generated_at"),
        "api_key_configured": bool(configured_api_key()),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _explicit(body: dict):
    """``{"search_by": …, "query": …}``: that one search and nothing else."""
    field = SEARCH_BY.get(str(body.get("search_by") or "").strip().lower())
    query = clean_value(body.get("query"))
    if field is None:
        return _err(422, "search_by must be terminal_id, merchant_id or merchant_name.",
                    lookup_status="invalid_query")
    if not query:
        return _err(422, f"query is empty: send the {field.replace('_', ' ')} to search.",
                    lookup_status="invalid_query", search_by=field)
    outcome = _search(field, query)
    if outcome.problem:
        return _err(422, f"query is {outcome.problem}.", lookup_status="invalid_query",
                    search_by=field, hint=_HINTS[field])
    logger.info("explicit lookup by %s: %s", field,
                "hit" if outcome.match else "several" if outcome.several else "miss")
    if outcome.match:
        return _found(outcome, "query")
    if outcome.several:
        return multiple_view(outcome.several, "query")
    return _lookup_miss(_HINTS[field])


@app.post("/api/v1/merchants/lookup", dependencies=[Depends(require_api_key)])
async def lookup(request: Request):
    body = await _body(request)
    if "search_by" in body:
        return _explicit(body)
    tried: list[str] = []
    invalid: list[dict] = []
    last_field = "terminal_id"
    for keys, field in ((TERMINAL_KEYS, "terminal_id"), (MERCHANT_ID_KEYS, "merchant_id"),
                        (NAME_KEYS, "merchant_name"), (FREE_TEXT_KEYS, "free_text"),
                        (CONTACT_KEYS, "contact_number")):
        for key in keys:
            value = clean_value(body.get(key))
            if not value:
                continue
            tried.append(key)
            outcome = _search(field, value)
            if outcome.problem:
                if field != "terminal_id":   # unchanged TID behaviour: a silent miss
                    invalid.append({"field": key, "reason": outcome.problem})
                continue
            last_field = outcome.field
            if outcome.match:
                logger.info("lookup hit via %s (%s)", key, outcome.match.matched_by)
                return _found(outcome, key)
            if outcome.several:
                logger.info("lookup via %s: %d merchants match", key,
                            len(outcome.several.records))
                return multiple_view(outcome.several, key)
    if not tried:
        return _err(422, "Send a terminal id, merchant id, merchant name or a registered "
                         "contact number.", lookup_status="no_identifier")
    logger.info("lookup miss (keys tried: %s)", ",".join(tried))
    return _lookup_miss(_HINTS[last_field], invalid)


@app.get("/api/v1/merchants/by-merchant-id/{merchant_id}", dependencies=[Depends(require_api_key)])
def by_merchant_id(merchant_id: str):
    if not merchant_id_keys(merchant_id)[0]:
        return _err(422, "Not a merchant id.", lookup_status="invalid_query",
                    hint=_HINTS["merchant_id"])
    match = store.find_merchant_id(merchant_id)
    return merchant_view(match) if match else _not_found(_HINTS["merchant_id"])


@app.get("/api/v1/merchants/search", dependencies=[Depends(require_api_key)])
def search_by_name(name: str = Query(default="", description="merchant name or a part of it")):
    """Every merchant whose name matches — one, several or none (200 / 404)."""
    found = store.find_name(name)
    if found.too_short:
        return _err(422, "name too short (3+ letters needed).", lookup_status="invalid_query",
                    hint=_HINTS["merchant_name"])
    if not found.records:
        return _not_found(_HINTS["merchant_name"])
    if len(found.records) == 1:
        return merchant_view(Match(found.records[0], "merchant_name"))
    return multiple_view(found, "name")


@app.get("/api/v1/merchants/by-contact/{number}", dependencies=[Depends(require_api_key)])
def by_contact(number: str):
    match = store.find_contact(number)
    return merchant_view(match) if match else _not_found("Use the registered 10-digit mobile number.")


@app.get("/api/v1/merchants/{terminal_id}", dependencies=[Depends(require_api_key)])
def by_terminal(terminal_id: str):
    match = store.find_terminal(terminal_id)
    return merchant_view(match) if match else _not_found("Terminal ids are 7-8 characters.")


@app.get("/api/v1/merchants", dependencies=[Depends(require_api_key)])
def list_merchants(
    issue: str | None = Query(default=None, description="issue_code, e.g. paper_roll"),
    bank: str | None = Query(default=None, description="bank name substring"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = store.search(issue_code=issue, bank=bank)
    return {
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "records": [masked_view(r) for r in rows[offset:offset + limit]],
    }
