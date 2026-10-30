"""Customer collection context — load/record functions shared by API and runtime.

Same trust model as shared.bot_config: the voice worker never accepts customer
identity from the client. The context row is resolved server-side, keyed by the
session's bot + tenant plus either an explicit context id (validated against
that tenant/bot) or the caller's phone number (matched on the trailing 10
digits, the stable part of Indian numbers across "+91 / 0 / bare" formats).

The runtime consumes an immutable :class:`CustomerContextSnapshot`; sensitive
fields are pre-masked here (`loan_account_masked`, `phone_masked`) and the raw
values are deliberately NOT part of the snapshot, so nothing downstream — the
prompt, transcripts, events — can ever leak them.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DIGITS = re.compile(r"\d")

# Call-state columns the runtime may write back after a call. A closed set so
# a bug (or a crafted payload upstream) can never turn this into a generic
# row-update primitive.
_WRITABLE_CALL_STATE = frozenset({
    "customer_verified", "account_disputed", "complaint_pending",
    "payment_status", "callback_requested", "callback_requested_at",
    "last_call_id", "last_disposition", "is_final_transcript",
    "interruption_detected",
})


def phone_tail(number: str | None, digits: int = 10) -> str | None:
    """Trailing digits of a dialable number ("+91-98765 43210" → "9876543210")."""
    if not number:
        return None
    found = _DIGITS.findall(str(number))
    if len(found) < 4:
        return None
    return "".join(found)[-digits:]


def mask_tail(value: str | None, keep: int = 4, prefix: str = "XX") -> str | None:
    """Mask all but the last ``keep`` characters ("LN00123456" → "XX3456")."""
    if not value:
        return None
    tail = str(value).strip()
    if not tail:
        return None
    return f"{prefix}{tail[-keep:]}"


@dataclass(frozen=True)
class CustomerContextSnapshot:
    """Immutable, pre-masked per-call view of one customer_contexts row."""

    context_id: str
    tenant_id: str
    bot_id: str
    customer_ref: str | None = None
    customer_name: str | None = None
    dcs_name: str | None = None
    lender_name: str | None = None
    loan_account_masked: str | None = None
    phone_masked: str | None = None
    phone_last4: str | None = None
    preferred_language: str | None = None
    overdue_amount: float | None = None
    total_outstanding: float | None = None
    minimum_payable: float | None = None
    penal_charges: float | None = None
    days_overdue: int | None = None
    due_date: str | None = None  # ISO date
    previous_promise_date: str | None = None
    partial_payment_allowed: bool | None = None
    payment_methods: tuple = ()
    secure_payment_link_available: bool | None = None
    active_offers: tuple = ()
    offer_terms: str | None = None
    credit_reporting_status: str | None = None
    callback_number_masked: str | None = None
    grievance_contact: str | None = None
    payment_status: str = "unknown"
    customer_verified: bool = False
    recording_notice_required: bool = True
    complaint_pending: bool = False
    account_disputed: bool = False
    callback_requested: bool = False
    previous_promise_pending: bool = False


def _snapshot_from_row(row) -> CustomerContextSnapshot:
    def _num(value):
        return float(value) if value is not None else None

    def _iso(value):
        return value.isoformat() if value is not None else None

    offers = tuple(row.active_offers or ()) if isinstance(row.active_offers, list) else ()
    methods = tuple(
        str(m) for m in (row.payment_methods or ())
    ) if isinstance(row.payment_methods, list) else ()
    tail = phone_tail(row.phone)
    return CustomerContextSnapshot(
        context_id=row.id,
        tenant_id=row.tenant_id,
        bot_id=row.bot_id,
        customer_ref=row.customer_ref,
        customer_name=row.customer_name,
        dcs_name=row.dcs_name,
        lender_name=row.lender_name,
        loan_account_masked=mask_tail(row.loan_account_number),
        phone_masked=mask_tail(tail, keep=4, prefix="XXXXXX"),
        phone_last4=(tail or "")[-4:] or None,
        preferred_language=row.preferred_language,
        overdue_amount=_num(row.overdue_amount),
        total_outstanding=_num(row.total_outstanding),
        minimum_payable=_num(row.minimum_payable),
        penal_charges=_num(row.penal_charges),
        days_overdue=row.days_overdue,
        due_date=_iso(row.due_date),
        previous_promise_date=_iso(row.previous_promise_date),
        partial_payment_allowed=row.partial_payment_allowed,
        payment_methods=methods,
        secure_payment_link_available=row.secure_payment_link_available,
        active_offers=offers,
        offer_terms=row.offer_terms,
        credit_reporting_status=row.credit_reporting_status,
        callback_number_masked=mask_tail(phone_tail(row.callback_number),
                                         keep=4, prefix="XXXXXX"),
        grievance_contact=row.grievance_contact,
        payment_status=row.payment_status or "unknown",
        customer_verified=bool(row.customer_verified),
        recording_notice_required=bool(row.recording_notice_required),
        complaint_pending=bool(row.complaint_pending),
        account_disputed=bool(row.account_disputed),
        callback_requested=bool(row.callback_requested),
        previous_promise_pending=row.previous_promise_date is not None,
    )


def _load_sync(
    tenant_id: str,
    bot_id: str,
    *,
    context_id: str | None = None,
    phone: str | None = None,
) -> CustomerContextSnapshot | None:
    from shared.db.mysql import get_sessionmaker
    from shared.models import CustomerCollectionContext

    session = get_sessionmaker()()
    try:
        row = None
        if context_id:
            row = session.get(CustomerCollectionContext, context_id)
            # Tenant/bot scoping is part of the lookup, not a serializer
            # concern: a context id from another tenant simply does not exist.
            if row is not None and (
                row.tenant_id != tenant_id or row.bot_id != bot_id or row.is_deleted
            ):
                row = None
        if row is None and phone:
            tail = phone_tail(phone)
            if tail:
                candidates = (
                    session.query(CustomerCollectionContext)
                    .filter(
                        CustomerCollectionContext.tenant_id == tenant_id,
                        CustomerCollectionContext.bot_id == bot_id,
                        CustomerCollectionContext.is_deleted.is_(False),
                        CustomerCollectionContext.phone.isnot(None),
                    )
                    .order_by(CustomerCollectionContext.updated_at.desc())
                    .limit(200)
                    .all()
                )
                row = next(
                    (c for c in candidates if phone_tail(c.phone) == tail), None
                )
        return _snapshot_from_row(row) if row is not None else None
    finally:
        session.close()


async def load_customer_context(
    tenant_id: str,
    bot_id: str,
    *,
    context_id: str | None = None,
    phone: str | None = None,
    timeout_seconds: float = 3.0,
) -> CustomerContextSnapshot | None:
    """Resolve the customer context for a call, or None.

    Bounded by ``timeout_seconds`` and never raises: a context lookup failure
    degrades the call to generic behavior instead of blocking the greeting.
    """
    if not context_id and not phone:
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _load_sync, tenant_id, bot_id,
                context_id=context_id, phone=phone,
            ),
            timeout=timeout_seconds,
        )
    except Exception:  # noqa: BLE001 — a data hiccup must not kill the call
        logger.exception(
            "customer context load failed (tenant=%s bot=%s)", tenant_id, bot_id
        )
        return None


def record_call_state_sync(context_id: str, **updates) -> bool:
    """Write mutable call-state fields back to the context row.

    Only whitelisted columns are writable; unknown keys are dropped (logged).
    Returns True when a row was updated.
    """
    from shared.db.mysql import get_sessionmaker
    from shared.models import CustomerCollectionContext
    from shared.models.collections_models import PAYMENT_STATUSES

    clean = {}
    for key, value in updates.items():
        if key not in _WRITABLE_CALL_STATE:
            logger.warning("ignoring non-writable call-state field %r", key)
            continue
        if key == "payment_status" and value not in PAYMENT_STATUSES:
            logger.warning("ignoring invalid payment_status %r", value)
            continue
        clean[key] = value
    if not clean:
        return False
    if clean.get("callback_requested") and "callback_requested_at" not in clean:
        clean["callback_requested_at"] = datetime.now(timezone.utc).replace(tzinfo=None)

    session = get_sessionmaker()()
    try:
        row = session.get(CustomerCollectionContext, context_id)
        if row is None or row.is_deleted:
            return False
        for key, value in clean.items():
            setattr(row, key, value)
        session.commit()
        return True
    except Exception:  # noqa: BLE001 — state write-back is best-effort
        logger.exception("customer context call-state write failed (%s)", context_id)
        session.rollback()
        return False
    finally:
        session.close()


async def record_call_state(context_id: str, **updates) -> bool:
    return await asyncio.to_thread(record_call_state_sync, context_id, **updates)
