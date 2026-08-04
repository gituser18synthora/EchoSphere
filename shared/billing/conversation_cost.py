"""Auditable per-conversation cost, reconstructed from its usage events.

`usage_events` is the source of truth: one row per (capability, engine) for a
call, each carrying the quantities the provider reported AND a
`pricing_snapshot` recording the exact `provider_pricing` row, unit, rate,
currency and FX rate applied at the time. That snapshot is what makes a cost
auditable months later — re-pricing from today's rate table would silently
restate history, so the snapshot is always preferred and the live table is only
consulted when a snapshot is absent (events written before snapshots existed).

The conversation total is the SUM of its events' `cost_usd`, including
telephony. `ConversationSession.cost_usd` is a denormalised cache of exactly
that sum, so the list view and the detail view can never disagree — the list
reads the cache, the detail view reads the breakdown, and both are the same
number by construction.

Everything is Decimal end to end. USD is the base currency; display conversion
applies one configured USD→target rate (never a chained cross-rate) and is
reported alongside the base amount rather than replacing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.billing.currency import effective_rate
from shared.models.billing_models import BASE_CURRENCY, UsageEvent

# Match usage_events.cost_usd — the widest precision anything is stored at.
COST_QUANT = Decimal("0.000001")

# A call costing more than this is worth a second look before anyone reads it
# as normal. Not a cap and never applied to the arithmetic: purely a flag the
# UI and reports can surface, so an outlier is investigated instead of hidden.
HIGH_COST_USD = Decimal("0.50")

# Human labels for the components the pricing engine produces.
COMPONENT_LABELS = {
    "audio_seconds": "Audio",
    "call_seconds": "Call time",
    "characters": "Characters",
    "input_tokens": "Input tokens",
    "output_tokens": "Output tokens",
    "cached_input_tokens": "Cached input tokens",
    "tokens": "Tokens",
    "requests": "Requests",
}

CAPABILITY_LABELS = {
    "stt": "Speech to text",
    "llm": "Language model",
    "tts": "Text to speech",
    "telephony": "Telephony",
    "embedding": "Embeddings",
}


def _dec(value) -> Decimal:
    if value is None:
        return Decimal(0)
    return Decimal(str(value))


@dataclass
class CostLine:
    """One priced component of one usage event."""

    capability: str
    provider: str
    model: str
    voice: str | None
    component: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    currency: str            # the rate's native currency (Sarvam quotes INR)
    fx_rate: Decimal | None  # USD -> native, as applied when charged
    cost_usd: Decimal
    priced: bool = True
    note: str | None = None

    def as_dict(self) -> dict:
        return {
            "capability": self.capability,
            "capabilityLabel": CAPABILITY_LABELS.get(self.capability, self.capability),
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "component": self.component,
            "componentLabel": COMPONENT_LABELS.get(self.component, self.component),
            "quantity": str(self.quantity),
            "unit": self.unit,
            "unitPrice": str(self.unit_price),
            "rateCurrency": self.currency,
            "fxRate": str(self.fx_rate) if self.fx_rate is not None else None,
            "costUsd": str(self.cost_usd),
            "priced": self.priced,
            "note": self.note,
        }


@dataclass
class ConversationCost:
    """Full auditable costing for one conversation."""

    session_id: str | None
    total_usd: Decimal = Decimal(0)
    lines: list[CostLine] = field(default_factory=list)
    by_capability: dict[str, Decimal] = field(default_factory=dict)
    unpriced: list[str] = field(default_factory=list)
    event_count: int = 0

    @property
    def high_cost(self) -> bool:
        return self.total_usd > HIGH_COST_USD

    def display(
        self, *, currency: str, rate: Decimal | None
    ) -> tuple[Decimal | None, str]:
        """(converted total, currency) for the configured display currency.

        Falls back to USD when the target has no configured rate — a cost is
        never converted through a guessed rate.
        """
        if currency == BASE_CURRENCY:
            return self.total_usd, BASE_CURRENCY
        if rate is None or rate <= 0:
            return self.total_usd, BASE_CURRENCY
        return (self.total_usd * rate).quantize(COST_QUANT, ROUND_HALF_UP), currency

    def as_dict(self, *, currency: str = BASE_CURRENCY, rate: Decimal | None = None) -> dict:
        converted, display_currency = self.display(currency=currency, rate=rate)
        return {
            "sessionId": self.session_id,
            "baseCurrency": BASE_CURRENCY,
            "totalUsd": str(self.total_usd),
            "displayCurrency": display_currency,
            "displayTotal": str(converted) if converted is not None else None,
            "displayRate": str(rate) if rate is not None and display_currency != BASE_CURRENCY else None,
            "byCapability": {
                capability: {
                    "label": CAPABILITY_LABELS.get(capability, capability),
                    "costUsd": str(amount),
                }
                for capability, amount in sorted(self.by_capability.items())
            },
            "lines": [line.as_dict() for line in self.lines],
            "unpriced": self.unpriced,
            "eventCount": self.event_count,
            "highCost": self.high_cost,
            "highCostThresholdUsd": str(HIGH_COST_USD),
        }


def _lines_from_snapshot(event: UsageEvent) -> list[CostLine]:
    """Rebuild the priced components from the event's own pricing snapshot."""
    snapshot = event.pricing_snapshot or {}
    lines: list[CostLine] = []
    for component, detail in snapshot.items():
        if component == "missing" or not isinstance(detail, dict):
            continue
        lines.append(
            CostLine(
                capability=event.capability,
                provider=event.provider_code,
                model=event.model_code or "",
                voice=event.voice_code,
                component=component,
                quantity=_dec(detail.get("quantity")),
                unit=str(detail.get("unit") or ""),
                unit_price=_dec(detail.get("unitPrice")),
                currency=str(detail.get("currency") or BASE_CURRENCY),
                fx_rate=_dec(detail["fxRate"]) if detail.get("fxRate") else None,
                cost_usd=_dec(detail.get("cost")),
            )
        )
    return lines


def _unpriced_line(event: UsageEvent, component: str) -> CostLine:
    """A component with usage but no configured price.

    Surfaced at zero cost with an explicit note rather than omitted: an
    unpriced component is a configuration gap the reader must be able to see,
    not a free one.
    """
    quantity = Decimal(0)
    if component in ("audio_seconds", "call_seconds"):
        quantity = _dec(event.audio_seconds)
    elif component == "characters":
        quantity = Decimal(event.characters or 0)
    elif component == "input_tokens":
        quantity = Decimal(max((event.input_tokens or 0) - (event.cached_tokens or 0), 0))
    elif component == "output_tokens":
        quantity = Decimal(event.output_tokens or 0)
    elif component == "cached_input_tokens":
        quantity = Decimal(event.cached_tokens or 0)
    elif component == "tokens":
        quantity = Decimal(event.total_tokens or 0)
    elif component == "requests":
        quantity = Decimal(event.requests or 0)
    return CostLine(
        capability=event.capability,
        provider=event.provider_code,
        model=event.model_code or "",
        voice=event.voice_code,
        component=component,
        quantity=quantity,
        unit="",
        unit_price=Decimal(0),
        currency=BASE_CURRENCY,
        fx_rate=None,
        cost_usd=Decimal(0),
        priced=False,
        note="No active price configured — usage recorded but not costed.",
    )


def conversation_cost(
    db: Session, session_id: str | None, *, include_zero_events: bool = True
) -> ConversationCost:
    """Reconstruct one conversation's costing from its usage events.

    `session_id` is the VOICE session id (`usage_events.session_id`), not the
    control-plane conversation id. A conversation with no linked session (or
    none recorded) returns an empty costing rather than an error — a missing
    link is a data gap, not a failure.
    """
    result = ConversationCost(session_id=session_id)
    if not session_id:
        return result

    events = list(
        db.execute(
            select(UsageEvent)
            .where(UsageEvent.session_id == session_id)
            .order_by(UsageEvent.capability, UsageEvent.occurred_at)
        ).scalars()
    )
    result.event_count = len(events)
    for event in events:
        lines = _lines_from_snapshot(event)
        missing = list((event.pricing_snapshot or {}).get("missing") or [])
        for component in missing:
            result.unpriced.append(f"{event.capability}:{event.provider_code}:{component}")
            if include_zero_events:
                lines.append(_unpriced_line(event, component))
        if not lines and include_zero_events and event.pricing_status != "priced":
            # No snapshot at all (pre-snapshot event, or nothing priceable).
            lines.append(_unpriced_line(event, "requests"))
        result.lines.extend(lines)

        # The event's own stored cost is authoritative for the total: the
        # snapshot components are what it was built from, and a rounding
        # difference must never let the breakdown disagree with the charge.
        cost = _dec(event.cost_usd)
        result.total_usd += cost
        if cost or include_zero_events:
            result.by_capability[event.capability] = (
                result.by_capability.get(event.capability, Decimal(0)) + cost
            )
    result.total_usd = result.total_usd.quantize(COST_QUANT, ROUND_HALF_UP)
    return result


def display_rate(
    db: Session, currency: str, *, as_of: datetime | None = None
) -> Decimal | None:
    """The USD→currency rate for display, or None when unconfigured."""
    if not currency or currency == BASE_CURRENCY:
        return None
    return effective_rate(db, currency, as_of=as_of)
