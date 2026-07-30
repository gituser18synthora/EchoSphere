"""Tenant-wise usage metering, provider pricing and currency conversion.

All monetary arithmetic uses decimal.Decimal. The platform base currency is
USD; display conversions go base -> target with a configured rate, never
chained through a third currency.
"""

from shared.billing.currency import (
    active_display_currencies,
    convert_from_usd,
    effective_rate,
    effective_rates_from_usd,
)
from shared.billing.metering import record_usage_event
from shared.billing.pricing import (
    MissingPriceError,
    PricedComponent,
    compute_cost,
    quantities_for,
)

__all__ = [
    "record_usage_event",
    "compute_cost",
    "quantities_for",
    "PricedComponent",
    "MissingPriceError",
    "effective_rate",
    "effective_rates_from_usd",
    "convert_from_usd",
    "active_display_currencies",
]
