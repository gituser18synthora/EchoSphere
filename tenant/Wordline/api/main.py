"""Wordline merchant data API — standalone tenant service.

Serves the merchant terminal records converted from ``Wordline.csv``
(``data/merchants.json``) to the Wordline voice bot.  It is a *tenant*
module: nothing in the platform imports it.  The platform reaches it the
same way it reaches any customer system — through an ``api_connections``
row executed by a workflow ``api`` node (``setup/01_api_connection.py``).

Endpoints (all under ``/api/v1``; every data endpoint needs ``X-API-Key``):

    GET  /health                         liveness + record count (no key)
    POST /merchants/lookup               resolve a merchant from workflow slots
    GET  /merchants/{terminal_id}        exact / digit-key terminal lookup
    GET  /merchants/by-contact/{number}  registered contact number lookup
    GET  /merchants                      filtered listing, contact numbers masked,
                                         no address / contact person

``POST /merchants/lookup`` is what the workflow calls.  The executor posts
the whole scalar slot state, so the body is a loose object; the first usable
identifier wins in this order: ``terminal_id_retry``,
``spoken_terminal_id_retry``, ``terminal_id``, ``spoken_terminal_id``,
``tid``, then ``contact_number``, ``phone``, ``caller_phone``, ``mobile``.
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
  ``lookup_status`` to be ``found`` or ``not_found``, so every other body —
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
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from tenant.Wordline.api.speech import spoken_address, spoken_model, spoken_name
from tenant.Wordline.api.store import MerchantStore, Match, clean_value, mask_contact

API_KEY_ENV = "WORDLINE_MERCHANT_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[3]
TERMINAL_KEYS = ("terminal_id_retry", "spoken_terminal_id_retry", "terminal_id",
                 "spoken_terminal_id", "tid")
CONTACT_KEYS = ("contact_number", "phone", "caller_phone", "mobile")
MERCHANT_FIELDS = ("terminal_id", "outlet_name", "bank_name", "asset_type",
                   "contact_number", "issue_type", "issue_code")
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

app = FastAPI(title="Wordline Merchant Data API", version="1.1.0",
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
    view["contact_number_masked"] = mask_contact(str(record.get("contact_number") or ""))
    view.update({k: spoken[k] for k in VISIT_FIELDS})
    return view


def masked_view(record: dict) -> dict:
    spoken = spoken_fields(record)
    view = {k: spoken.get(k, record.get(k)) for k in MERCHANT_FIELDS if k != "contact_number"}
    view["contact_number_masked"] = mask_contact(str(record.get("contact_number") or ""))
    return view


def _not_found(hint: str) -> JSONResponse:
    """Unknown resource on the GET routes."""
    return _err(404, "No merchant found for the shared details.",
                lookup_status="not_found", hint=hint)


def _lookup_miss(hint: str) -> dict:
    """POST /lookup completed and matched nothing — a 200 with no mapped keys
    (see the module docstring for why this is not a 404)."""
    return {"lookup_status": "not_found", "found_count": 0,
            "message": "No merchant found for the shared details.", "hint": hint}


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


@app.post("/api/v1/merchants/lookup", dependencies=[Depends(require_api_key)])
async def lookup(request: Request):
    body = await _body(request)
    tried = []
    for key in TERMINAL_KEYS:
        value = clean_value(body.get(key))
        if not value:
            continue
        tried.append(key)
        match = store.find_terminal(value)
        if match:
            logger.info("lookup hit via %s (%s)", key, match.matched_by)
            return merchant_view(match)
    for key in CONTACT_KEYS:
        value = clean_value(body.get(key))
        if not value:
            continue
        tried.append(key)
        match = store.find_contact(value)
        if match:
            logger.info("lookup hit via %s", key)
            return merchant_view(match)
    if not tried:
        return _err(422, "Send a terminal id or a registered contact number.",
                    lookup_status="no_identifier")
    logger.info("lookup miss (keys tried: %s)", ",".join(tried))
    return _lookup_miss("Terminal ids are 7-8 characters; check the digits dictated.")


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
