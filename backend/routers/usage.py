"""Tenant-wise AI/API usage, cost and currency endpoints.

- /usage/summary: tenant-scoped usage + cost (tenant roles locked to their
  own tenant; super admins pass ?tenantId=).
- /usage/platform: platform-wide usage by tenant/provider/model/capability
  (super admin only).
- /currency/rates: active display currencies + current USD->X rates so any
  authenticated user can render converted amounts. Conversions are computed
  server-side; the stored base cost never changes with display currency.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from backend.core.deps import (
    get_current_user,
    require_super_admin,
    resolve_tenant_id,
)
from backend.core.responses import ok
from shared.billing import convert_from_usd, effective_rates_from_usd
from shared.billing.currency import active_display_currencies
from shared.db.mysql import get_db
from shared.models import Tenant, UsageEvent, User
from shared.models.billing_models import BASE_CURRENCY, USAGE_CAPABILITIES

router = APIRouter(tags=["Usage"])


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.utcnow()
    return end - timedelta(days=days), end


_QUANTITY_COLUMNS = (
    func.coalesce(func.sum(UsageEvent.requests), 0).label("requests"),
    func.coalesce(func.sum(UsageEvent.input_tokens), 0).label("input_tokens"),
    func.coalesce(func.sum(UsageEvent.output_tokens), 0).label("output_tokens"),
    func.coalesce(func.sum(UsageEvent.cached_tokens), 0).label("cached_tokens"),
    func.coalesce(func.sum(UsageEvent.total_tokens), 0).label("total_tokens"),
    func.coalesce(func.sum(UsageEvent.characters), 0).label("characters"),
    func.coalesce(func.sum(UsageEvent.audio_seconds), 0).label("audio_seconds"),
    func.coalesce(func.sum(UsageEvent.cost_usd), 0).label("cost_usd"),
    func.sum(
        case((UsageEvent.pricing_status == "missing_price", 1), else_=0)
    ).label("missing_price_events"),
)


def _row_payload(row) -> dict:
    return {
        "requests": int(row.requests),
        "inputTokens": int(row.input_tokens),
        "outputTokens": int(row.output_tokens),
        "cachedTokens": int(row.cached_tokens),
        "totalTokens": int(row.total_tokens),
        "characters": int(row.characters),
        "audioSeconds": float(row.audio_seconds),
        "costUsd": float(row.cost_usd),
        "missingPriceEvents": int(row.missing_price_events or 0),
    }


def _converted(db: Session, amount_usd: Decimal | float) -> dict:
    """Server-side conversions of a USD amount into every configured currency."""
    rates = effective_rates_from_usd(db)
    return {
        code: float(convert_from_usd(amount_usd, rate)) for code, rate in rates.items()
    }


@router.get("/usage/summary")
def usage_summary(
    days: int = Query(30, ge=1, le=365),
    tenant_id: str | None = Query(None, alias="tenantId"),
    bot_id: str | None = Query(None, alias="botId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    start, end = _window(days)

    filters = [
        UsageEvent.tenant_id == tid,
        UsageEvent.occurred_at >= start,
        UsageEvent.occurred_at <= end,
    ]
    if bot_id:
        filters.append(UsageEvent.bot_id == bot_id)

    by_capability = db.execute(
        select(UsageEvent.capability, *_QUANTITY_COLUMNS)
        .where(*filters)
        .group_by(UsageEvent.capability)
    ).all()
    by_provider = db.execute(
        select(
            UsageEvent.capability,
            UsageEvent.provider_code,
            UsageEvent.model_code,
            *_QUANTITY_COLUMNS,
        )
        .where(*filters)
        .group_by(UsageEvent.capability, UsageEvent.provider_code, UsageEvent.model_code)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
    ).all()

    capabilities = {row.capability: _row_payload(row) for row in by_capability}
    for cap in USAGE_CAPABILITIES:
        capabilities.setdefault(cap, {
            "requests": 0, "inputTokens": 0, "outputTokens": 0, "cachedTokens": 0,
            "totalTokens": 0, "characters": 0, "audioSeconds": 0.0, "costUsd": 0.0,
            "missingPriceEvents": 0,
        })

    total_usd = sum(Decimal(str(row.cost_usd)) for row in by_capability) or Decimal(0)
    missing_total = sum(int(row.missing_price_events or 0) for row in by_capability)

    return ok({
        "tenantId": tid,
        "period": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "days": days},
        "baseCurrency": BASE_CURRENCY,
        "totalCostUsd": float(total_usd),
        "totalCostConverted": _converted(db, total_usd),
        "missingPriceEvents": missing_total,
        "capabilities": capabilities,
        "byProviderModel": [
            {
                "capability": row.capability,
                "provider": row.provider_code,
                "model": row.model_code or "",
                **_row_payload(row),
            }
            for row in by_provider
        ],
    })


@router.get("/usage/platform")
def platform_usage(
    days: int = Query(30, ge=1, le=365),
    capability: str | None = Query(None),
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    start, end = _window(days)
    filters = [UsageEvent.occurred_at >= start, UsageEvent.occurred_at <= end]
    if capability:
        filters.append(UsageEvent.capability == capability)
    if tenant_id:
        filters.append(UsageEvent.tenant_id == tenant_id)

    by_tenant = db.execute(
        select(UsageEvent.tenant_id, Tenant.name, *_QUANTITY_COLUMNS)
        .join(Tenant, Tenant.id == UsageEvent.tenant_id)
        .where(*filters, Tenant.is_deleted.is_(False))
        .group_by(UsageEvent.tenant_id, Tenant.name)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
    ).all()
    by_capability = db.execute(
        select(UsageEvent.capability, *_QUANTITY_COLUMNS)
        .where(*filters)
        .group_by(UsageEvent.capability)
    ).all()
    by_provider_model = db.execute(
        select(
            UsageEvent.capability,
            UsageEvent.provider_code,
            UsageEvent.model_code,
            *_QUANTITY_COLUMNS,
        )
        .where(*filters)
        .group_by(UsageEvent.capability, UsageEvent.provider_code, UsageEvent.model_code)
        .order_by(func.sum(UsageEvent.cost_usd).desc())
    ).all()

    total_usd = sum(Decimal(str(row.cost_usd)) for row in by_capability) or Decimal(0)

    return ok({
        "period": {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "days": days},
        "baseCurrency": BASE_CURRENCY,
        "totalCostUsd": float(total_usd),
        "totalCostConverted": _converted(db, total_usd),
        "missingPriceEvents": sum(int(r.missing_price_events or 0) for r in by_capability),
        "byTenant": [
            {"tenantId": row.tenant_id, "tenant": row.name, **_row_payload(row)}
            for row in by_tenant
        ],
        "byCapability": {row.capability: _row_payload(row) for row in by_capability},
        "byProviderModel": [
            {
                "capability": row.capability,
                "provider": row.provider_code,
                "model": row.model_code or "",
                **_row_payload(row),
            }
            for row in by_provider_model
        ],
    })


@router.get("/currency/rates")
def currency_rates(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Active display currencies + the USD rates currently in force.

    Read-only and safe for every authenticated role: it powers the display
    currency selector. Managing rates stays super-admin-only via /master.
    """
    currencies = active_display_currencies(db)
    rates = effective_rates_from_usd(db)
    return ok({
        "baseCurrency": BASE_CURRENCY,
        "currencies": [
            {
                "code": c.code,
                "name": c.name,
                "symbol": c.symbol,
                "decimalPlaces": c.decimal_places,
                "isBase": c.is_base,
                # A display currency is usable when it's the base or has a rate.
                "hasRate": c.code == BASE_CURRENCY or c.code in rates,
            }
            for c in currencies
        ],
        "rates": {code: float(rate) for code, rate in rates.items()},
    })
