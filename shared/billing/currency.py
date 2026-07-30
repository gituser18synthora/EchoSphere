"""Exchange-rate selection and Decimal-safe display conversion.

The platform base currency is USD. A conversion uses exactly one configured
USD->target rate (active, effective_from <= as_of, newest wins) — chained
cross-currency conversions are never performed.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.billing_models import BASE_CURRENCY, Currency, ExchangeRate

# Converted display amounts keep cost-level precision; UI trims further.
_AMOUNT_QUANT = Decimal("0.000001")


def active_display_currencies(db: Session) -> list[Currency]:
    stmt = (
        select(Currency)
        .where(Currency.status == "active", Currency.is_deleted.is_(False))
        .order_by(Currency.sort_order.asc(), Currency.code.asc())
    )
    return list(db.execute(stmt).scalars())


def effective_rate(
    db: Session,
    target_code: str,
    *,
    base_code: str = BASE_CURRENCY,
    as_of: datetime | None = None,
) -> Decimal | None:
    """The rate in force for base->target at `as_of`, or None if unconfigured."""
    if target_code == base_code:
        return Decimal(1)
    as_of = as_of or datetime.utcnow()
    stmt = (
        select(ExchangeRate.rate)
        .where(
            ExchangeRate.base_code == base_code,
            ExchangeRate.target_code == target_code,
            ExchangeRate.status == "active",
            ExchangeRate.is_deleted.is_(False),
            ExchangeRate.effective_from <= as_of,
        )
        .order_by(ExchangeRate.effective_from.desc())
        .limit(1)
    )
    rate = db.execute(stmt).scalar_one_or_none()
    return Decimal(str(rate)) if rate is not None else None


def effective_rates_from_usd(
    db: Session, *, as_of: datetime | None = None
) -> dict[str, Decimal]:
    """Current USD->code rates for every active non-base currency."""
    rates: dict[str, Decimal] = {}
    for currency in active_display_currencies(db):
        if currency.code == BASE_CURRENCY:
            continue
        rate = effective_rate(db, currency.code, as_of=as_of)
        if rate is not None:
            rates[currency.code] = rate
    return rates


def convert_from_usd(
    amount_usd: Decimal | float | int,
    rate: Decimal,
) -> Decimal:
    """Convert a USD amount with an already-selected rate (Decimal-safe)."""
    return (Decimal(str(amount_usd)) * rate).quantize(_AMOUNT_QUANT, ROUND_HALF_UP)
