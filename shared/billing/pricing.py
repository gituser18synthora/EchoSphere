"""Database-driven provider pricing and Decimal-safe cost calculation.

Selection rule for a (provider, capability, model, component) price:
active rows with effective_from <= as_of, newest effective_from wins.
Missing prices never fabricate a cost — the event is recorded with
pricing_status="missing_price" and surfaced to Super Admin.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.billing.currency import effective_rate
from shared.models.billing_models import BASE_CURRENCY, ProviderPricing

# Raw quantity -> priced unit divisor / multiplier.
# quantity is expressed in the component's natural unit (tokens, characters,
# seconds, requests); cost = quantity / divisor * unit_price.
_UNIT_DIVISORS: dict[str, Decimal] = {
    "per_token": Decimal(1),
    "per_1k_tokens": Decimal(1000),
    "per_1m_tokens": Decimal(1_000_000),
    "per_character": Decimal(1),
    "per_1k_characters": Decimal(1000),
    "per_1m_characters": Decimal(1_000_000),
    "per_second": Decimal(1),
    "per_minute": Decimal(60),
    "per_hour": Decimal(3600),
    "per_request": Decimal(1),
}

# Cost snapshot precision on usage events (Numeric(14, 6)).
_COST_QUANT = Decimal("0.000001")


class MissingPriceError(LookupError):
    """No active price configured for a component that has usage."""


@dataclass(frozen=True)
class PricedComponent:
    component: str
    quantity: Decimal
    unit: str
    # Native provider price/currency as configured (Sarvam quotes INR).
    unit_price: Decimal
    currency: str
    # Cost is always in USD; non-USD rows convert through fx_rate (USD->native).
    cost: Decimal
    # The exact provider_pricing row used — auditors can reproduce the math.
    price_id: str | None = None
    # Platform selling price and the resulting tenant charge (0 = no markup).
    selling_price: Decimal | None = None
    charge: Decimal = Decimal(0)
    fx_rate: Decimal | None = None


def quantities_for(
    capability: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cached_tokens: int = 0,
    total_tokens: int = 0,
    characters: int = 0,
    audio_seconds: Decimal | float | int = 0,
    requests: int = 1,
) -> dict[str, Decimal]:
    """Map raw usage figures to the pricing components of a capability.

    Only components with non-zero usage are returned, so an unpriced
    component with zero quantity never marks an event missing_price.
    """
    seconds = Decimal(str(audio_seconds))
    out: dict[str, Decimal] = {}
    if capability == "llm":
        # `input_tokens` is the provider's GROSS prompt count INCLUDING the
        # cached portion (the LLMStreamUsage convention; OpenAI/Gemini report
        # it that way natively, the Anthropic adapter normalizes). Cache hits
        # are billed at the cached rate ONLY — they must be netted out of the
        # full-rate input component or the cached tokens are charged twice.
        billable_input = max(input_tokens - cached_tokens, 0)
        if billable_input:
            out["input_tokens"] = Decimal(billable_input)
        if output_tokens:
            out["output_tokens"] = Decimal(output_tokens)
        if cached_tokens:
            out["cached_input_tokens"] = Decimal(cached_tokens)
        # Blended per-token pricing (legacy ApprovedModel-style) applies when
        # no split price exists; expose the total for that fallback.
        if not out and total_tokens:
            out["tokens"] = Decimal(total_tokens)
    elif capability == "embedding":
        tokens = total_tokens or input_tokens
        if tokens:
            out["tokens"] = Decimal(tokens)
    elif capability == "stt":
        if seconds:
            out["audio_seconds"] = seconds
    elif capability == "tts":
        if characters:
            out["characters"] = Decimal(characters)
        elif seconds:
            out["audio_seconds"] = seconds
    elif capability == "telephony":
        if seconds:
            out["call_seconds"] = seconds
    if not out and requests:
        out["requests"] = Decimal(requests)
    return out


def _active_prices(
    db: Session,
    provider_code: str,
    capability: str,
    model_code: str | None,
    as_of: datetime,
) -> dict[str, ProviderPricing]:
    stmt = (
        select(ProviderPricing)
        .where(
            ProviderPricing.provider_code == provider_code,
            ProviderPricing.capability == capability,
            ProviderPricing.model_code == (model_code or ""),
            ProviderPricing.status == "active",
            ProviderPricing.is_deleted.is_(False),
            ProviderPricing.effective_from <= as_of,
        )
        .order_by(ProviderPricing.effective_from.desc())
    )
    prices: dict[str, ProviderPricing] = {}
    for row in db.execute(stmt).scalars():
        prices.setdefault(row.component, row)  # newest effective_from wins
    return prices


def _component_price(
    prices: dict[str, ProviderPricing], component: str
) -> ProviderPricing | None:
    row = prices.get(component)
    if row is not None:
        return row
    # LLM split components fall back to a blended per-token price.
    if component in ("input_tokens", "output_tokens", "cached_input_tokens"):
        return prices.get("tokens")
    return None


def compute_cost(
    db: Session,
    *,
    provider_code: str,
    capability: str,
    model_code: str | None,
    quantities: dict[str, Decimal],
    as_of: datetime | None = None,
) -> tuple[Decimal, list[PricedComponent], list[str]]:
    """Cost the usage quantities against configured pricing.

    Returns (total_cost_usd, priced components, components with no price).
    Each priced component also carries the tenant charge derived from the
    row's optional selling price — total charge is the sum of `c.charge`.

    Prices are stored in the provider's native currency (Sarvam publishes
    INR-only rates). Non-USD rows convert to USD through the exchange rate
    in force at `as_of`; with no configured rate the component is treated
    as unpriced — a cost is never fabricated from a guessed rate.
    """
    as_of = as_of or datetime.utcnow()
    prices = _active_prices(db, provider_code, capability, model_code, as_of)
    priced: list[PricedComponent] = []
    missing: list[str] = []
    total = Decimal(0)
    fx_cache: dict[str, Decimal | None] = {}
    for component, quantity in quantities.items():
        if quantity <= 0:
            continue
        row = _component_price(prices, component)
        # A flat per-request price also covers the synthetic "requests"
        # component; but a missing "requests" price is only a problem when
        # nothing else was priced (requests is a fallback quantity).
        if row is None:
            if component != "requests" or not priced:
                missing.append(component)
            continue
        currency = row.currency_code or BASE_CURRENCY
        fx_rate: Decimal | None = None
        if currency != BASE_CURRENCY:
            if currency not in fx_cache:
                fx_cache[currency] = effective_rate(db, currency, as_of=as_of)
            fx_rate = fx_cache[currency]
            if fx_rate is None or fx_rate <= 0:
                missing.append(component)
                continue
        divisor = _UNIT_DIVISORS.get(row.unit)
        if divisor is None:
            missing.append(component)
            continue

        def _to_usd(price_native: Decimal) -> Decimal:
            amount = quantity / divisor * price_native
            if fx_rate is not None:
                amount = amount / fx_rate  # fx_rate is USD -> native
            return amount.quantize(_COST_QUANT, ROUND_HALF_UP)

        unit_price = Decimal(str(row.unit_price))
        cost = _to_usd(unit_price)
        selling_price = (
            Decimal(str(row.selling_price)) if row.selling_price is not None else None
        )
        charge = _to_usd(selling_price) if selling_price is not None else Decimal(0)
        priced.append(
            PricedComponent(
                component=component,
                quantity=quantity,
                unit=row.unit,
                unit_price=unit_price,
                currency=currency,
                cost=cost,
                price_id=row.id,
                selling_price=selling_price,
                charge=charge,
                fx_rate=fx_rate,
            )
        )
        total += cost
    return total, priced, missing
