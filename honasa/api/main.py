"""Honasa mock commerce service.

Test doubles for the order systems referenced by the Honasa/Aurexion FAQ
response bank (scripts/Honasa_Aurexion_Bot_FAQ_Response_Bank.xlsx), scoped to
the two POC categories:

  1. Order / Information — order status, ETA, tracking, order amount,
     discount/cashback, refund status  →  ``POST /api/v1/orders/lookup`` and
     ``POST /api/v1/orders/{order_id}/tracking-link``
  2. Return / Replacement — return eligibility (seven-day window), damaged /
     wrong / missing / defective-or-expired product resolutions
     →  ``POST /api/v1/orders/{order_id}/returns``

Escalation targets (``POST /api/v1/support/escalations``) back the workflow's
failure/agent paths. All static records live in ``honasa/data/orders.json`` —
date fields are stored as day offsets and materialized here at read time so
the scenario suite never rots.

Business rules enforced server-side (the bot never decides these itself):
  - change-of-mind returns require the order to be delivered, the item
    category to be returnable, and at most RETURN_WINDOW_DAYS since delivery;
  - damaged / wrong / missing / defective-or-expired resolutions require a
    delivered order within QUALITY_WINDOW_DAYS of delivery;
  - every accepted request "sends" the return link over WhatsApp to the
    registered number (recorded in runtime_state.json).

Run:  env/bin/uvicorn honasa.api.main:app --port 9022   (or ./honasa/run.sh)
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STATE_FILE = DATA_DIR / "runtime_state.json"

RETURN_WINDOW_DAYS = 7   # change-of-mind returns (FAQ row: "within 7 days of delivery")
QUALITY_WINDOW_DAYS = 7  # damaged / wrong / missing / defective complaints

ISSUE_TYPES = ("no_longer_needed", "damaged", "wrong_item", "missing_item",
               "defective_expired")
RESOLUTIONS = ("return", "replacement")

app = FastAPI(title="Honasa Mock Commerce", version="1.0.0")


# ── data access ──────────────────────────────────────────────────────────────


def _orders() -> dict:
    """Read fresh on every call so data edits apply without a restart."""
    payload = json.loads((DATA_DIR / "orders.json").read_text(encoding="utf-8"))
    payload.pop("_comment", None)
    return payload


def _state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"resolution_requests": [], "tracking_links": [], "escalations": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(days_delta: int) -> str:
    return (date.today() + timedelta(days=days_delta)).isoformat()


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return ("X" * max(0, len(digits) - 4)) + digits[-4:] if digits else ""


def _err(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message, **extra})


async def _body(request: Request) -> dict:
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001 — empty/invalid body is fine for mocks
        return {}


# ── order materialization ────────────────────────────────────────────────────


def _product_summary(items: list[dict]) -> str:
    names = [str(i.get("name") or "").strip() for i in items if i.get("name")]
    if not names:
        return "your order"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{len(names)} items including {names[0]}"


def _materialize(record: dict) -> dict:
    """Flatten one raw order record into the API view with real dates.

    Every field is scalar on purpose: the voice workflow maps them straight
    into conversation slots.
    """
    status = record["order_status"]
    delivered_ago = record.get("delivered_days_ago")
    out = {
        "verified": True,
        "order_id": record["order_id"],
        "customer_name": record["customer_name"],
        "order_status": status,
        "product_summary": _product_summary(record.get("items") or []),
        "item_count": len(record.get("items") or []),
        "order_placed_on": _iso(-int(record.get("placed_days_ago", 0))),
        "order_amount_inr": record["order_amount_inr"],
        "payment_mode": record["payment_mode"],
        "discount_inr": record.get("discount_inr", 0),
        "cashback_inr": record.get("cashback_inr", 0),
        "registered_phone_masked": _mask_phone(record.get("phone", "")),
    }
    if record.get("shipped_days_ago") is not None:
        out["shipped_on"] = _iso(-int(record["shipped_days_ago"]))
    if record.get("eta_days_ahead") is not None and status != "delivered":
        out["expected_delivery_date"] = _iso(int(record["eta_days_ahead"]))
    if record.get("courier_name"):
        out["courier_name"] = record["courier_name"]
    tracking_available = bool(record.get("awb")) and status in (
        "shipped", "out_for_delivery", "delivered")
    out["tracking_available"] = tracking_available

    if delivered_ago is not None and status == "delivered":
        days_since = int(delivered_ago)
        out["delivered_on"] = _iso(-days_since)
        out["days_since_delivery"] = days_since
    else:
        days_since = None

    # Change-of-mind return eligibility (FAQ: "within 7 days of delivery,
    # subject to the applicable policy").
    if status != "delivered":
        out["return_eligible"] = False
        out["return_ineligible_reason"] = "not_delivered"
    elif not record.get("returnable", True):
        out["return_eligible"] = False
        out["return_ineligible_reason"] = "category_not_returnable"
        out["non_returnable_reason"] = record.get(
            "non_returnable_reason", "category not returnable")
    elif days_since is not None and days_since > RETURN_WINDOW_DAYS:
        out["return_eligible"] = False
        out["return_ineligible_reason"] = "window_closed"
        out["return_window_days"] = RETURN_WINDOW_DAYS
    else:
        out["return_eligible"] = True
        out["return_window_days"] = RETURN_WINDOW_DAYS
        if days_since is not None:
            out["return_window_days_left"] = max(0, RETURN_WINDOW_DAYS - days_since)

    refund = record.get("refund")
    if refund:
        out["refund_status"] = refund.get("status", "in_process")
        out["refund_amount_inr"] = refund.get("amount_inr")
        if refund.get("initiated_days_ago") is not None:
            out["refund_initiated_on"] = _iso(-int(refund["initiated_days_ago"]))
        if refund.get("expected_days_ahead") is not None:
            out["refund_expected_by"] = _iso(int(refund["expected_days_ahead"]))
        out["refund_mode"] = refund.get("mode", "original payment method")
    else:
        out["refund_status"] = "none"
    return out


# ── health ───────────────────────────────────────────────────────────────────


@app.get("/api/v1/health")
def health() -> dict:
    return {"status": "ok", "service": "honasa-mock", "at": _now()}


# ── order lookup (Order / Information) ───────────────────────────────────────


def _digit_runs(value: object) -> list[str]:
    return re.findall(r"\d+", str(value or ""))


@app.post("/api/v1/orders/lookup")
async def lookup_order(request: Request):
    """Resolve an order by order ID or registered mobile number.

    The workflow sends its whole slot state (order_ref2 wins over order_ref —
    the retry ask must beat a failed first attempt). Values may arrive as
    whole utterances ("it's 7001001, the serum one"), so digit runs are
    extracted, and a spelled-out mobile number matches on its last ten digits.
    """
    body = await _body(request)
    orders = _orders()
    by_phone: dict[str, list[dict]] = {}
    for record in orders.values():
        by_phone.setdefault(re.sub(r"\D", "", record.get("phone", ""))[-10:],
                            []).append(record)

    candidates: list[str] = []
    for key in ("order_ref2", "order_ref", "identifier", "order_id",
                "caller_phone", "phone"):
        raw = body.get(key)
        if raw is None or str(raw).strip() in ("", "{" + key + "}"):
            continue
        candidates.extend(_digit_runs(raw))
        joined = "".join(_digit_runs(raw))
        if joined and joined not in candidates:
            candidates.append(joined)

    for digits in candidates:
        if digits in orders:
            return _materialize(orders[digits])
    for digits in candidates:
        if len(digits) >= 10:
            group = by_phone.get(digits[-10:])
            if group:
                latest = min(group, key=lambda r: r.get("placed_days_ago", 999))
                view = _materialize(latest)
                view["multiple_orders_on_phone"] = len(group) > 1
                view["orders_on_phone"] = len(group)
                return view
    return _err(404, "No order found for the shared order ID or mobile number.",
                verified=False)


# ── tracking link (Order / Information: "Can you share the tracking link?") ──


@app.post("/api/v1/orders/{order_id}/tracking-link")
async def send_tracking_link(order_id: str, request: Request):
    digits = "".join(_digit_runs(order_id))
    record = _orders().get(digits)
    if record is None:
        return _err(404, "Order not found.", sent=False)
    view = _materialize(record)
    if not view["tracking_available"]:
        return _err(409, "Tracking is not live for this order yet.", sent=False,
                    order_status=view["order_status"])
    state = _state()
    entry = {
        "link_id": f"TL-{uuid.uuid4().hex[:8].upper()}",
        "order_id": digits,
        "channel": "whatsapp",
        "sent_to_masked": view["registered_phone_masked"],
        "courier_name": view.get("courier_name"),
        "awb": record.get("awb"),
        "sent_at": _now(),
    }
    state["tracking_links"].append(entry)
    _save_state(state)
    return {"sent": True, "channel": "whatsapp",
            "whatsapp_number_masked": view["registered_phone_masked"],
            "link_id": entry["link_id"]}


# ── returns / replacements (Return / Replacement) ────────────────────────────


@app.post("/api/v1/orders/{order_id}/returns")
async def create_resolution_request(order_id: str, request: Request):
    digits = "".join(_digit_runs(order_id))
    record = _orders().get(digits)
    if record is None:
        return _err(404, "Order not found.", created=False)
    body = await _body(request)
    issue_type = str(body.get("issue_type") or "").strip().lower()
    resolution = str(body.get("resolution") or "").strip().lower()
    if issue_type not in ISSUE_TYPES:
        return _err(422, f"Unknown issue_type '{issue_type}'.", created=False)
    if resolution not in RESOLUTIONS:
        return _err(422, f"Unknown resolution '{resolution}'.", created=False)

    view = _materialize(record)
    if issue_type == "no_longer_needed":
        # Change-of-mind returns follow the published seven-day policy.
        if not view["return_eligible"]:
            return _err(409, "This order is not eligible for a return.",
                        created=False,
                        reason=view.get("return_ineligible_reason", "ineligible"))
    else:
        # Quality complaints need a delivered order within the quality window.
        if view["order_status"] != "delivered":
            return _err(409, "The order has not been delivered yet.",
                        created=False, reason="not_delivered")
        if int(view.get("days_since_delivery", 0)) > QUALITY_WINDOW_DAYS:
            return _err(409, "The quality-issue window for this order has closed.",
                        created=False, reason="quality_window_closed")

    state = _state()
    entry = {
        "request_id": f"RR-{uuid.uuid4().hex[:8].upper()}",
        "order_id": digits,
        "issue_type": issue_type,
        "resolution": resolution,
        "details": str(body.get("details") or "").strip() or None,
        "whatsapp_link_sent": True,
        "whatsapp_number_masked": view["registered_phone_masked"],
        "created_at": _now(),
    }
    state["resolution_requests"].append(entry)
    _save_state(state)
    return {
        "created": True,
        "request_id": entry["request_id"],
        "order_id": digits,
        "issue_type": issue_type,
        "resolution": resolution,
        "whatsapp_link_sent": True,
        "whatsapp_number_masked": view["registered_phone_masked"],
    }


@app.get("/api/v1/orders/{order_id}/returns")
def list_resolution_requests(order_id: str):
    digits = "".join(_digit_runs(order_id))
    return {"requests": [r for r in _state()["resolution_requests"]
                         if r["order_id"] == digits]}


# ── support escalation (workflow failure / agent paths) ─────────────────────


@app.post("/api/v1/support/escalations")
async def create_escalation(request: Request):
    body = await _body(request)
    state = _state()
    entry = {
        "ticket_id": f"TCK-{uuid.uuid4().hex[:8].upper()}",
        "queue": str(body.get("queue") or "customer_support"),
        "order_id": "".join(_digit_runs(body.get("order_id"))) or None,
        "call_state": body,
        "created_at": _now(),
    }
    state["escalations"].append(entry)
    _save_state(state)
    return {"created": True, "ticket_id": entry["ticket_id"],
            "queue": entry["queue"]}


# ── debug ────────────────────────────────────────────────────────────────────


@app.get("/api/v1/state")
def debug_state():
    return _state()
