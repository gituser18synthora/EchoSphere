"""OYO mock integration service.

Test doubles for the backend systems referenced by the OYO Booking
Confirmation documents (Booking_Confirmation.docx + Booking Confirmation
prompt.docx): Booking Details, Customer Verification, Booking Voucher,
property occupancy / status / pricing (7-day ARR + complimentary amount),
PM / Stock-Team outbound-call orchestration, Shift API, CRM disposition and
IVR transfer.

All static responses live in ``oyo/data/*.json`` — handlers only read the
relevant file and shape the reply; nothing is hardcoded here.

Cross-bot context passing: the Property Verification / Stock Validation bots
POST their call outcome to ``/api/v1/verification-reports``. When the
customer-facing bot later invokes ``/api/v1/calls/property-manager`` (or
``/calls/stock-team``) for the same booking, a live report wins over the
scripted outcome in ``pm_call_outcomes.json`` / ``stock_team_outcomes.json``.

Run:  env/bin/uvicorn oyo.api.main:app --port 9021
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "runtime_state.json"

app = FastAPI(title="OYO Mock Integrations", version="1.0.0")


# ── data access ──────────────────────────────────────────────────────────────


def _load(name: str) -> dict:
    """Read a data file fresh on every call so edits apply without restart."""
    payload = json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))
    payload.pop("_comment", None)
    return payload


def _state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"verification_reports": [], "dispositions": [], "vouchers": [],
            "shifts": [], "ivr_transfers": [], "complimentary": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_booking_id(value: object) -> str:
    """'BK 601001' / '6 0 1 0 0 1' / 'booking id is 601001' → '601001'."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": message, **extra})


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 — empty/invalid body is fine for mocks
        return {}


# ── health ───────────────────────────────────────────────────────────────────


@app.get("/api/v1/health")
def health() -> dict:
    return {"status": "ok", "service": "oyo-mock", "at": _now()}


# ── customer verification (spec Flow 2) ──────────────────────────────────────


@app.post("/api/v1/customers/verify")
async def verify_customer(request: Request):
    body = await _body(request)
    booking_id = _norm_booking_id(body.get("booking_id"))
    record = _load("customers").get(booking_id)
    if record is None:
        return _err(404, "No booking found for this booking ID.",
                    verified=False)

    def _contains(provided: object, expected: str) -> bool:
        p = str(provided or "").strip().lower()
        e = (expected or "").strip().lower()
        if not p or not e:
            return False
        return e in p or p in e or all(tok in p for tok in e.split())

    phone_ok = _norm_booking_id(body.get("caller_phone")) and (
        _norm_booking_id(body.get("caller_phone"))[-10:]
        == _norm_booking_id(record.get("phone"))[-10:]
    )
    name_ok = _contains(body.get("guest_name"), record.get("guest_name", ""))
    hotel_ok = _contains(body.get("hotel_name"), record.get("hotel_name", ""))
    date_ok = _contains(body.get("checkin_date"), record.get("checkin_date", ""))

    if phone_ok or name_ok or hotel_ok or date_ok:
        return {
            "verified": True,
            "booking_id": booking_id,
            "matched_on": ("phone" if phone_ok else "guest_name" if name_ok
                           else "hotel_name" if hotel_ok else "checkin_date"),
        }
    return _err(401, "The details shared do not match this booking. "
                     "No booking information can be disclosed.",
                verified=False)


# ── booking details (spec Flow 3 / Flow 5) ───────────────────────────────────


@app.get("/api/v1/bookings/{booking_id}")
def booking_details(booking_id: str):
    booking = _load("bookings").get(_norm_booking_id(booking_id))
    if booking is None:
        return _err(404, "Booking not found.")
    return booking


# ── booking voucher (spec Flow 4) ────────────────────────────────────────────


@app.post("/api/v1/bookings/{booking_id}/voucher")
async def send_voucher(booking_id: str, request: Request):
    booking = _load("bookings").get(_norm_booking_id(booking_id))
    if booking is None:
        return _err(404, "Booking not found.")
    body = await _body(request)
    # Precedence: customer-corrected address > generic email slot > the email
    # mapped from the booking > the booking record itself. Values may arrive
    # as whole utterances or unresolved "{placeholder}" literals — only a
    # string that actually contains an email address counts.
    email = ""
    for candidate in (body.get("email_address"), body.get("email"),
                      body.get("guest_email"), booking.get("guest_email")):
        match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
                          str(candidate or ""))
        if match:
            email = match.group(0)
            break
    if not email:
        return _err(422, "No valid email address available for this booking.",
                    sent=False)
    state = _state()
    voucher = {
        "voucher_id": f"VCH-{uuid.uuid4().hex[:8].upper()}",
        "booking_id": booking["booking_id"],
        "email": email,
        "sent_at": _now(),
    }
    state["vouchers"].append(voucher)
    _save_state(state)
    return {"sent": True, **voucher}


# ── property backend (occupancy / status / pricing / alternates) ─────────────


@app.get("/api/v1/properties/{property_id}/occupancy")
def property_occupancy(property_id: str):
    prop = _load("properties").get(property_id.strip().upper())
    if prop is None:
        return _err(404, "Property not found.")
    return {"property_id": prop["property_id"], **prop["occupancy"]}


@app.get("/api/v1/properties/{property_id}/status")
def property_status(property_id: str):
    prop = _load("properties").get(property_id.strip().upper())
    if prop is None:
        return _err(404, "Property not found.")
    status = prop["status"]
    return {
        "property_id": prop["property_id"],
        "operational_status": status["operational_status"],
        "under_maintenance": status["operational_status"] == "maintenance",
        "hold_reasons": status["hold_reasons"],
    }


@app.get("/api/v1/properties/{property_id}/pricing")
def property_pricing(property_id: str, booking_id: str | None = Query(None)):
    prop = _load("properties").get(property_id.strip().upper())
    if prop is None:
        return _err(404, "Property not found.")
    pricing = prop["pricing"]
    booking = _load("bookings").get(_norm_booking_id(booking_id)) or {}
    nights_rate = None
    if booking:
        try:
            nights = max(
                1,
                (datetime.fromisoformat(booking["checkout_date"])
                 - datetime.fromisoformat(booking["checkin_date"])).days,
            )
            nights_rate = round(booking["booking_amount"] / nights)
        except (KeyError, ValueError):
            nights_rate = booking.get("booking_amount")
    result = {
        "property_id": prop["property_id"],
        "arr_7day": pricing["arr_7day"],
        "complimentary_amount": pricing["complimentary_amount"],
        "currency": "INR",
    }
    if nights_rate is not None:
        result["booking_rate"] = nights_rate
        result["rate_vs_arr"] = ("meets" if nights_rate >= pricing["arr_7day"]
                                 else "below")
    return result


@app.get("/api/v1/properties/{property_id}/alternates")
def property_alternates(property_id: str):
    properties = _load("properties")
    prop = properties.get(property_id.strip().upper())
    if prop is None:
        return _err(404, "Property not found.")
    alternates = [
        {"property_id": p["property_id"], "name": p["name"], "city": p["city"]}
        for pid in prop.get("alternates", [])
        if (p := properties.get(pid)) is not None
    ]
    if not alternates:
        return _err(404, "No alternate OYO properties available nearby.",
                    count=0)
    return {
        "property_id": prop["property_id"],
        "count": len(alternates),
        "alternates": alternates,
        "top_alternate_id": alternates[0]["property_id"],
        "top_alternate_name": alternates[0]["name"],
    }


@app.post("/api/v1/bookings/{booking_id}/complimentary")
async def add_complimentary(booking_id: str, request: Request):
    bid = _norm_booking_id(booking_id)
    booking = _load("bookings").get(bid)
    if booking is None:
        return _err(404, "Booking not found.")
    prop = _load("properties").get(booking["property_id"], {})
    amount = prop.get("pricing", {}).get("complimentary_amount", 0)
    state = _state()
    entry = {"booking_id": bid, "amount": amount, "currency": "INR",
             "added_at": _now()}
    state["complimentary"].append(entry)
    _save_state(state)
    return {"added": True, **entry}


# ── outbound-call orchestration (PM / Stock Team) ────────────────────────────


def _latest_report(channel: str, booking_id: str) -> dict | None:
    reports = [r for r in _state()["verification_reports"]
               if r.get("channel") == channel
               and r.get("booking_id") == booking_id]
    return reports[-1] if reports else None


_DENY_KEYWORDS = (
    ("overbooked", ("overbook", "full", "sold out", "no rooms", "house full")),
    ("maintenance", ("maintenance", "renovat", "repair", "blocked", "closed")),
    ("price_low", ("price", "rate", "tariff", "low", "amount")),
)


def _classify_deny_reason(text: str) -> str:
    lowered = (text or "").lower()
    for reason, needles in _DENY_KEYWORDS:
        if any(n in lowered for n in needles):
            return reason
    return "other"


@app.post("/api/v1/calls/property-manager")
async def call_property_manager(request: Request):
    body = await _body(request)
    booking_id = _norm_booking_id(body.get("booking_id"))
    if not _load("bookings").get(booking_id):
        return _err(404, "Booking not found.")

    live = _latest_report("pm", booking_id)
    if live is not None:
        honored = live.get("outcome") == "honored"
        deny_reason = _classify_deny_reason(str(live.get("deny_reason") or ""))
        return {
            "call_status": "completed",
            "booking_honored": honored,
            "deny_reason": "none" if honored and not live.get("deny_reason")
                           else deny_reason,
            "resolution": live.get("resolution")
                          or ("confirmed" if honored else "not_honored"),
            "notes": "Outcome reported live by the OYO Property Verification bot.",
            "source": "live_report",
        }

    scripted = _load("pm_call_outcomes").get(booking_id)
    if scripted is None:
        scripted = {"call_status": "completed", "booking_honored": True,
                    "deny_reason": "none", "resolution": "confirmed",
                    "notes": "Default scripted outcome."}
    return {**scripted, "source": "scripted"}


@app.post("/api/v1/calls/stock-team")
async def call_stock_team(request: Request):
    body = await _body(request)
    booking_id = _norm_booking_id(body.get("booking_id"))
    if not _load("bookings").get(booking_id):
        return _err(404, "Booking not found.")

    live = _latest_report("stock", booking_id)
    if live is not None:
        confirmed = live.get("outcome") == "honored"
        return {
            "call_status": "completed",
            "stock_status": "confirmed" if confirmed else "unavailable",
            "notes": "Outcome reported live by the OYO Stock Team Validation bot.",
            "source": "live_report",
        }

    scripted = _load("stock_team_outcomes").get(booking_id)
    if scripted is None:
        scripted = {"call_status": "no_answer", "stock_status": "unavailable",
                    "notes": "Default scripted outcome — stock team unreachable."}
    return {**scripted, "source": "scripted"}


# ── verification reports (posted by the PM / Stock bots) ─────────────────────


@app.post("/api/v1/verification-reports")
async def record_verification_report(
    request: Request,
    channel: str = Query("pm", pattern="^(pm|stock)$"),
    outcome: str = Query(..., pattern="^(honored|not_honored)$"),
):
    body = await _body(request)
    booking_id = _norm_booking_id(body.get("booking_id"))
    report = {
        "report_id": f"VR-{uuid.uuid4().hex[:8].upper()}",
        "channel": channel,
        "outcome": outcome,
        "booking_id": booking_id,
        "deny_reason": body.get("deny_reason"),
        "resolution": body.get("resolution"),
        "details": body,
        "recorded_at": _now(),
    }
    state = _state()
    state["verification_reports"].append(report)
    _save_state(state)
    return {"recorded": True, "report_id": report["report_id"],
            "channel": channel, "outcome": outcome}


@app.get("/api/v1/verification-reports")
def list_verification_reports():
    return {"reports": _state()["verification_reports"]}


# ── shift flow (spec Flow 8) ─────────────────────────────────────────────────


@app.post("/api/v1/bookings/{booking_id}/shift")
async def shift_booking(booking_id: str, request: Request):
    bid = _norm_booking_id(booking_id)
    booking = _load("bookings").get(bid)
    if booking is None:
        return _err(404, "Booking not found.")
    properties = _load("properties")
    prop = properties.get(booking["property_id"], {})
    alternates = [
        properties[pid] for pid in prop.get("alternates", [])
        if pid in properties
    ]
    if not alternates:
        return _err(409, "No alternate property available to shift to.",
                    shifted=False)
    target = alternates[0]
    state = _state()
    shift = {
        "shift_id": f"SH-{uuid.uuid4().hex[:8].upper()}",
        "booking_id": bid,
        "from_property_id": booking["property_id"],
        "new_property_id": target["property_id"],
        "new_property_name": target["name"],
        "status": "shift_initiated",
        "initiated_at": _now(),
    }
    state["shifts"].append(shift)
    _save_state(state)
    return {"shifted": True, **shift}


# ── CRM disposition (spec Flow 9) ────────────────────────────────────────────


@app.post("/api/v1/crm/dispositions")
async def record_disposition(request: Request):
    body = await _body(request)
    state = _state()
    disposition = {
        "disposition_id": f"DSP-{uuid.uuid4().hex[:8].upper()}",
        "booking_id": _norm_booking_id(body.get("booking_id")) or None,
        "call_state": body,
        "recorded_at": _now(),
    }
    state["dispositions"].append(disposition)
    _save_state(state)
    return {"recorded": True, "disposition_id": disposition["disposition_id"]}


@app.get("/api/v1/crm/dispositions")
def list_dispositions():
    return {"dispositions": _state()["dispositions"]}


# ── IVR transfer ─────────────────────────────────────────────────────────────


@app.post("/api/v1/ivr/transfer")
async def ivr_transfer(request: Request):
    body = await _body(request)
    state = _state()
    transfer = {
        "transfer_id": f"TR-{uuid.uuid4().hex[:8].upper()}",
        "queue": str(body.get("queue") or "customer_support"),
        "booking_id": _norm_booking_id(body.get("booking_id")) or None,
        "requested_at": _now(),
    }
    state["ivr_transfers"].append(transfer)
    _save_state(state)
    return {"transferred": True, **transfer}
