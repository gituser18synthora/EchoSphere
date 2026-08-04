"""Customer collection context: per-customer account data for collection calls.

Tenant- and bot-scoped. This is the server-trusted "customer context" a
collection call runs against: the voice runtime loads it at call start
(matched by caller number or an explicit context id) and records the mutable
call-state flags back after the call.

Security model:
- every read resolves through the bot row and `assert_tenant_access`, so a
  context id can never be dereferenced across tenants (404, never 403);
- responses are always masked: the full phone and loan account number are
  write-only fields — the API returns `phoneMasked` / `loanAccountMasked`;
- writes require a tenant admin; the call-state subresource accepts only the
  closed set of runtime-owned flags.
"""

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    get_current_user,
    require_tenant_admin,
)
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.serializers import serialize_customer_context
from shared.customer_context import phone_tail
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.models import CustomerCollectionContext, User, VoiceBot
from shared.models.collections_models import PAYMENT_STATUSES

router = APIRouter(tags=["Customer Context"])

_PHONE_PATTERN = r"^\+?[0-9][0-9 \-]{6,18}$"
_LANGUAGE_PATTERN = r"^[a-z]{2}(-[A-Z]{2})?$"


def _get_bot(db: Session, user: User, bot_id: str) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("Bot")
    assert_tenant_access(user, bot.tenant_id)
    return bot


def _get_context(db: Session, user: User, context_id: str) -> CustomerCollectionContext:
    row = db.get(CustomerCollectionContext, context_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Customer context")
    assert_tenant_access(user, row.tenant_id)
    return row


class CustomerContextPayload(BaseModel):
    """Create/update payload. Unknown values are omitted/null — never ""/0."""

    customer_ref: str | None = Field(None, alias="customerRef", max_length=80)
    phone: str | None = Field(None, pattern=_PHONE_PATTERN)
    customer_name: str | None = Field(None, alias="customerName", max_length=150)
    dcs_name: str | None = Field(None, alias="dcsName", max_length=150)
    lender_name: str | None = Field(None, alias="lenderName", max_length=150)
    loan_account_number: str | None = Field(
        None, alias="loanAccountNumber", min_length=4, max_length=40
    )
    preferred_language: str | None = Field(
        None, alias="preferredLanguage", pattern=_LANGUAGE_PATTERN
    )
    overdue_amount: Decimal | None = Field(None, alias="overdueAmount", ge=0)
    total_outstanding: Decimal | None = Field(None, alias="totalOutstanding", ge=0)
    minimum_payable: Decimal | None = Field(None, alias="minimumPayable", ge=0)
    penal_charges: Decimal | None = Field(None, alias="penalCharges", ge=0)
    days_overdue: int | None = Field(None, alias="daysOverdue", ge=0, le=36500)
    due_date: date | None = Field(None, alias="dueDate")
    previous_promise_date: date | None = Field(None, alias="previousPromiseDate")
    partial_payment_allowed: bool | None = Field(None, alias="partialPaymentAllowed")
    payment_methods: list[str] | None = Field(None, alias="paymentMethods", max_length=10)
    secure_payment_link_available: bool | None = Field(
        None, alias="securePaymentLinkAvailable"
    )
    active_offers: list[dict] | None = Field(None, alias="activeOffers", max_length=10)
    offer_terms: str | None = Field(None, alias="offerTerms", max_length=4000)
    credit_reporting_status: str | None = Field(
        None, alias="creditReportingStatus", max_length=120
    )
    callback_number: str | None = Field(
        None, alias="callbackNumber", pattern=_PHONE_PATTERN
    )
    grievance_contact: str | None = Field(
        None, alias="grievanceContact", max_length=150
    )
    payment_status: str | None = Field(None, alias="paymentStatus")
    customer_verified: bool | None = Field(None, alias="customerVerified")
    recording_notice_required: bool | None = Field(
        None, alias="recordingNoticeRequired"
    )
    complaint_pending: bool | None = Field(None, alias="complaintPending")
    account_disputed: bool | None = Field(None, alias="accountDisputed")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("payment_status")
    @classmethod
    def _valid_status(cls, value):
        if value is not None and value not in PAYMENT_STATUSES:
            raise ValueError(
                f"paymentStatus must be one of {', '.join(PAYMENT_STATUSES)}"
            )
        return value

    @field_validator("payment_methods")
    @classmethod
    def _valid_methods(cls, value):
        if value is None:
            return value
        cleaned = [str(m).strip() for m in value if str(m).strip()]
        if any(len(m) > 40 for m in cleaned):
            raise ValueError("payment method labels must be at most 40 characters")
        return cleaned


class CallStatePayload(BaseModel):
    """Runtime-owned call-state flags. Deliberately the ONLY writable subset
    exposed to non-admin tooling — everything else needs a tenant admin."""

    customer_verified: bool | None = Field(None, alias="customerVerified")
    account_disputed: bool | None = Field(None, alias="accountDisputed")
    complaint_pending: bool | None = Field(None, alias="complaintPending")
    payment_status: str | None = Field(None, alias="paymentStatus")
    callback_requested: bool | None = Field(None, alias="callbackRequested")
    callback_requested_at: datetime | None = Field(None, alias="callbackRequestedAt")
    last_call_id: str | None = Field(None, alias="lastCallId", max_length=64)
    last_disposition: str | None = Field(None, alias="lastDisposition", max_length=40)
    is_final_transcript: bool | None = Field(None, alias="isFinalTranscript")
    interruption_detected: bool | None = Field(None, alias="interruptionDetected")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("payment_status")
    @classmethod
    def _valid_status(cls, value):
        if value is not None and value not in PAYMENT_STATUSES:
            raise ValueError(
                f"paymentStatus must be one of {', '.join(PAYMENT_STATUSES)}"
            )
        return value


@router.get("/bots/{bot_id}/customer-contexts")
def list_customer_contexts(
    bot_id: str,
    params: PageParams = Depends(page_params),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bot = _get_bot(db, user, bot_id)
    stmt = select(CustomerCollectionContext).where(
        CustomerCollectionContext.tenant_id == bot.tenant_id,
        CustomerCollectionContext.bot_id == bot.id,
        CustomerCollectionContext.is_deleted.is_(False),
    )
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(
            CustomerCollectionContext.customer_name.like(like)
            | CustomerCollectionContext.customer_ref.like(like)
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(CustomerCollectionContext.updated_at.desc())
        .offset(params.offset)
        .limit(params.page_size)
    ).all()
    return paginated(
        [serialize_customer_context(r) for r in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


@router.get("/bots/{bot_id}/customer-contexts/lookup")
def lookup_customer_context(
    bot_id: str,
    phone: str = Query(..., pattern=_PHONE_PATTERN),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The call-time contract: the context a call from ``phone`` would load."""
    bot = _get_bot(db, user, bot_id)
    tail = phone_tail(phone)
    if not tail:
        raise ApiError(400, "phone must contain at least 4 digits")
    rows = db.scalars(
        select(CustomerCollectionContext)
        .where(
            CustomerCollectionContext.tenant_id == bot.tenant_id,
            CustomerCollectionContext.bot_id == bot.id,
            CustomerCollectionContext.is_deleted.is_(False),
            CustomerCollectionContext.phone.isnot(None),
        )
        .order_by(CustomerCollectionContext.updated_at.desc())
        .limit(200)
    ).all()
    row = next((r for r in rows if phone_tail(r.phone) == tail), None)
    if row is None:
        raise NotFoundError("Customer context")
    return ok(serialize_customer_context(row))


@router.get("/customer-contexts/{context_id}")
def get_customer_context(
    context_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ok(serialize_customer_context(_get_context(db, user, context_id)))


@router.post("/bots/{bot_id}/customer-contexts", status_code=201)
def create_customer_context(
    bot_id: str,
    body: CustomerContextPayload,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    bot = _get_bot(db, user, bot_id)
    row = CustomerCollectionContext(
        id=new_id("cctx"),
        tenant_id=bot.tenant_id,
        bot_id=bot.id,
        created_by=user.id,
        updated_by=user.id,
        **body.model_dump(exclude_none=True),
    )
    db.add(row)
    record_audit(
        db, user=user, action="customer_context.create",
        entity_type="customer_context", entity_id=row.id,
        target_label=row.customer_name or row.customer_ref or row.id,
        tenant_id=bot.tenant_id, request=request,
    )
    db.commit()
    db.refresh(row)
    return ok(serialize_customer_context(row))


@router.patch("/customer-contexts/{context_id}")
def update_customer_context(
    context_id: str,
    body: CustomerContextPayload,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _get_context(db, user, context_id)
    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(row, key, value)
    row.updated_by = user.id
    record_audit(
        db, user=user, action="customer_context.update",
        entity_type="customer_context", entity_id=row.id,
        target_label=row.customer_name or row.customer_ref or row.id,
        tenant_id=row.tenant_id, new_value=sorted(updates.keys()), request=request,
    )
    db.commit()
    db.refresh(row)
    return ok(serialize_customer_context(row))


@router.patch("/customer-contexts/{context_id}/call-state")
def update_call_state(
    context_id: str,
    body: CallStatePayload,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update runtime-owned call-state flags (verification, dispute,
    complaint, payment status, callback, transcript/interruption telemetry)."""
    row = _get_context(db, user, context_id)
    updates = body.model_dump(exclude_unset=True)
    if not updates:
        raise ApiError(400, "no call-state fields provided")
    if updates.get("callback_requested") and "callback_requested_at" not in updates:
        updates["callback_requested_at"] = datetime.utcnow()
    for key, value in updates.items():
        setattr(row, key, value)
    row.updated_by = user.id
    record_audit(
        db, user=user, action="customer_context.call_state",
        entity_type="customer_context", entity_id=row.id,
        target_label=row.customer_name or row.customer_ref or row.id,
        tenant_id=row.tenant_id, new_value=sorted(updates.keys()), request=request,
    )
    db.commit()
    db.refresh(row)
    return ok(serialize_customer_context(row))


@router.delete("/customer-contexts/{context_id}")
def delete_customer_context(
    context_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = _get_context(db, user, context_id)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    record_audit(
        db, user=user, action="customer_context.delete",
        entity_type="customer_context", entity_id=row.id,
        target_label=row.customer_name or row.customer_ref or row.id,
        tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"deleted": True})
