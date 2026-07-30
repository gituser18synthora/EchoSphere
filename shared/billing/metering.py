"""Record tenant-attributed usage events and maintain the daily rollup.

One call = one logical billable provider operation (a call's LLM turns, a
TTS generation batch, one embedding request). Callers pass a deterministic
`request_id` so stream reconnects, frontend retries and finalize re-runs
never double-count: the unique index turns re-submission into a no-op.
"""

from __future__ import annotations

import logging
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.billing.pricing import compute_cost, quantities_for
from shared.ids import new_id
from shared.models.billing_models import UsageEvent
from shared.models.ops_models import UsageRecord

logger = logging.getLogger("echosphere.billing")

_COST_COLUMNS = {
    "llm": "cost_llm",
    "embedding": "cost_embedding",
    "stt": "cost_stt",
    "tts": "cost_tts",
    "telephony": "cost_telephony",
}


def record_usage_event(
    db: Session,
    *,
    tenant_id: str,
    capability: str,
    provider_code: str,
    model_code: str | None = None,
    bot_id: str | None = None,
    session_id: str | None = None,
    voice_code: str | None = None,
    request_id: str | None = None,
    occurred_at: datetime | None = None,
    requests: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    total_tokens: int = 0,
    characters: int = 0,
    audio_seconds: Decimal | float | int = 0,
    usage_source: str = "provider",
    usage_metadata: dict | None = None,
    commit: bool = True,
) -> UsageEvent | None:
    """Persist one usage event, cost it from configured pricing, roll it up.

    Returns the created event, or None when `request_id` was already
    recorded (idempotent replay). Never raises on missing pricing — the
    quantities are kept and the event is marked pricing_status=missing_price.
    """
    if not tenant_id:
        raise ValueError("usage event requires a tenant_id")
    # Truncate to whole seconds: MySQL DATETIME(0) ROUNDS fractional seconds,
    # so an event stamped hh:mm:ss.5+ would be stored one second in the future
    # and fall outside an occurred_at <= now() summary window queried in the
    # same instant.
    occurred_at = (occurred_at or datetime.utcnow()).replace(microsecond=0)
    if not total_tokens and (input_tokens or output_tokens or cached_tokens):
        total_tokens = input_tokens + output_tokens + cached_tokens

    if request_id:
        existing = db.execute(
            select(UsageEvent.id).where(UsageEvent.request_id == request_id)
        ).scalar_one_or_none()
        if existing is not None:
            return None

    quantities = quantities_for(
        capability,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        characters=characters,
        audio_seconds=audio_seconds,
        requests=requests,
    )
    cost_usd, priced, missing = compute_cost(
        db,
        provider_code=provider_code,
        capability=capability,
        model_code=model_code,
        quantities=quantities,
        as_of=occurred_at,
    )
    charge_usd = sum((p.charge for p in priced), Decimal(0))
    snapshot = {
        p.component: {
            "priceId": p.price_id,
            "unit": p.unit,
            "unitPrice": str(p.unit_price),
            "sellingPrice": str(p.selling_price) if p.selling_price is not None else None,
            "currency": p.currency,
            # USD -> native rate applied for non-USD native prices (e.g. INR).
            "fxRate": str(p.fx_rate) if p.fx_rate is not None else None,
            "quantity": str(p.quantity),
            "cost": str(p.cost),
            "charge": str(p.charge),
        }
        for p in priced
    }
    if missing:
        snapshot["missing"] = missing

    event = UsageEvent(
        id=new_id("ue"),
        tenant_id=tenant_id,
        bot_id=bot_id,
        session_id=session_id,
        capability=capability,
        provider_code=provider_code,
        model_code=model_code,
        voice_code=voice_code,
        request_id=request_id,
        occurred_at=occurred_at,
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        characters=characters,
        audio_seconds=Decimal(str(audio_seconds)),
        usage_source=usage_source,
        usage_metadata=usage_metadata,
        pricing_status="missing_price" if missing else "priced",
        pricing_snapshot=snapshot or None,
        cost_usd=cost_usd,
        charge_usd=charge_usd,
    )
    db.add(event)

    if cost_usd:
        _rollup_cost(db, tenant_id, bot_id, occurred_at.date(), capability, cost_usd)

    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError:
        # A concurrent writer recorded the same request_id first.
        db.rollback()
        logger.info("usage event %s already recorded, skipping", request_id)
        return None
    return event


def _get_or_create_rollup(
    db: Session, tenant_id: str, bot_id: str | None, day: date_type
) -> UsageRecord:
    stmt = (
        select(UsageRecord)
        .where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.bot_id.is_(None) if bot_id is None else UsageRecord.bot_id == bot_id,
            UsageRecord.date == day,
        )
        .with_for_update()
    )
    record = db.execute(stmt).scalars().first()
    if record is None:
        record = UsageRecord(
            id=new_id("ur"), tenant_id=tenant_id, bot_id=bot_id, date=day
        )
        db.add(record)
    return record


def _rollup_cost(
    db: Session,
    tenant_id: str,
    bot_id: str | None,
    day: date_type,
    capability: str,
    cost_usd: Decimal,
) -> None:
    column = _COST_COLUMNS.get(capability)
    if column is None:
        return
    targets = [None] if bot_id is None else [None, bot_id]
    for target in targets:
        record = _get_or_create_rollup(db, tenant_id, target, day)
        setattr(record, column, Decimal(str(getattr(record, column) or 0)) + cost_usd)


def rollup_call(
    db: Session,
    *,
    tenant_id: str,
    bot_id: str | None,
    day: date_type,
    calls: int = 1,
    contained: bool = False,
    escalated: bool = False,
    minutes: Decimal | float = 0,
) -> None:
    """Fold one completed call into the tenant + bot daily rollup rows."""
    targets = [None] if bot_id is None else [None, bot_id]
    for target in targets:
        record = _get_or_create_rollup(db, tenant_id, target, day)
        record.calls = (record.calls or 0) + calls
        if contained:
            record.contained_calls = (record.contained_calls or 0) + 1
        if escalated:
            record.escalations = (record.escalations or 0) + 1
        record.minutes = Decimal(str(record.minutes or 0)) + Decimal(str(minutes))
