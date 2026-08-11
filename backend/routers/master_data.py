"""Super Admin master-data management: industries, countries, data regions, plans,
AI configuration profiles, providers, supported languages and voice profiles.

Every mutation is permission-guarded server-side, audited, and reference-safe:
records used by tenants/bots/subscriptions can be deactivated or archived but
never permanently deleted.
"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import get_current_user, require_permission
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from shared.db.mysql import get_db
from shared.models import (
    AiConfigProfile,
    AuditLog,
    BotLanguage,
    Country,
    Currency,
    DataRegion,
    GuardrailProfile,
    ExchangeRate,
    Industry,
    Plan,
    ProviderDef,
    ProviderModel,
    ProviderPricing,
    Subscription,
    SupportedLanguage,
    Tenant,
    User,
    VoiceBot,
    VoiceBotSetting,
    VoiceProfile,
)
from shared.models.billing_models import (
    BASE_CURRENCY,
    PRICING_COMPONENTS,
    PRICING_UNITS,
    USAGE_CAPABILITIES,
)
from backend.serializers import (
    serialize_ai_profile,
    serialize_country,
    serialize_currency,
    serialize_data_region,
    serialize_exchange_rate,
    serialize_industry,
    serialize_language,
    serialize_plan,
    serialize_provider,
    serialize_provider_model,
    serialize_provider_pricing,
    serialize_tenant,
    serialize_voice,
)

router = APIRouter(tags=["Master Data"])


# ── Usage counting (reference protection) ────────────────────────────────────


def _industry_usage(db: Session, row: Industry) -> int:
    return db.scalar(
        select(func.count()).select_from(Tenant).where(
            or_(Tenant.industry == row.code, Tenant.industry == row.name),
            Tenant.is_deleted.is_(False),
        )
    ) or 0


def _country_usage(db: Session, row: Country) -> int:
    return db.scalar(
        select(func.count()).select_from(DataRegion).where(
            or_(DataRegion.country_id == row.id, DataRegion.country == row.name),
            DataRegion.is_deleted.is_(False),
        )
    ) or 0


def _region_usage(db: Session, row: DataRegion) -> int:
    return db.scalar(
        select(func.count()).select_from(Tenant).where(
            or_(Tenant.region == row.code, Tenant.region == row.name),
            Tenant.is_deleted.is_(False),
        )
    ) or 0


def _plan_usage(db: Session, row: Plan) -> int:
    return db.scalar(
        select(func.count()).select_from(Subscription).where(
            Subscription.plan_id == row.id, Subscription.is_deleted.is_(False)
        )
    ) or 0


def _ai_profile_usage(db: Session, row: AiConfigProfile) -> int:
    return db.scalar(
        select(func.count()).select_from(Tenant).where(
            Tenant.ai_profile_code == row.code, Tenant.is_deleted.is_(False)
        )
    ) or 0


def _provider_usage(db: Session, row: ProviderDef) -> int:
    profiles = db.scalar(
        select(func.count()).select_from(AiConfigProfile).where(
            AiConfigProfile.is_deleted.is_(False),
            or_(
                AiConfigProfile.stt_provider == row.code,
                AiConfigProfile.tts_provider == row.code,
                AiConfigProfile.llm_provider == row.code,
                AiConfigProfile.embedding_provider == row.code,
            ),
        )
    ) or 0
    bots = db.scalar(
        select(func.count()).select_from(VoiceBotSetting).where(
            or_(
                VoiceBotSetting.stt_provider == row.code,
                VoiceBotSetting.tts_provider == row.code,
                VoiceBotSetting.llm_provider == row.code,
                VoiceBotSetting.fallback_provider == row.code,
            )
        )
    ) or 0
    return profiles + bots


def _provider_model_usage(db: Session, row: ProviderModel) -> int:
    profiles = db.scalar(
        select(func.count()).select_from(AiConfigProfile).where(
            AiConfigProfile.is_deleted.is_(False),
            or_(
                (AiConfigProfile.stt_provider == row.provider_code)
                & (AiConfigProfile.stt_model == row.code),
                (AiConfigProfile.tts_provider == row.provider_code)
                & (AiConfigProfile.tts_model == row.code),
                (AiConfigProfile.llm_provider == row.provider_code)
                & (AiConfigProfile.llm_model == row.code),
                (AiConfigProfile.embedding_provider == row.provider_code)
                & (AiConfigProfile.embedding_model == row.code),
            ),
        )
    ) or 0
    bots = db.scalar(
        select(func.count()).select_from(VoiceBotSetting).where(
            or_(
                (VoiceBotSetting.stt_provider == row.provider_code)
                & (VoiceBotSetting.stt_model == row.code),
                (VoiceBotSetting.tts_provider == row.provider_code)
                & (VoiceBotSetting.tts_model == row.code),
                (VoiceBotSetting.llm_provider == row.provider_code)
                & (VoiceBotSetting.llm_model == row.code),
                (VoiceBotSetting.fallback_provider == row.provider_code)
                & (VoiceBotSetting.fallback_model == row.code),
            )
        )
    ) or 0
    return profiles + bots


def _language_usage(db: Session, row: SupportedLanguage) -> int:
    return db.scalar(
        select(func.count()).select_from(BotLanguage).where(
            BotLanguage.language_code == row.code
        )
    ) or 0


def _currency_usage(db: Session, row: Currency) -> int:
    """A currency is referenced by exchange rates, provider pricing and plans."""
    rates = db.scalar(
        select(func.count()).select_from(ExchangeRate).where(
            or_(ExchangeRate.base_code == row.code, ExchangeRate.target_code == row.code),
            ExchangeRate.is_deleted.is_(False),
        )
    ) or 0
    pricing = db.scalar(
        select(func.count()).select_from(ProviderPricing).where(
            ProviderPricing.currency_code == row.code,
            ProviderPricing.is_deleted.is_(False),
        )
    ) or 0
    plans = db.scalar(
        select(func.count()).select_from(Plan).where(
            Plan.currency == row.code, Plan.is_deleted.is_(False)
        )
    ) or 0
    return rates + pricing + plans


def _exchange_rate_usage(db: Session, row: ExchangeRate) -> int:
    # Usage events snapshot the rate they used — no live references to protect.
    return 0


def _provider_pricing_usage(db: Session, row: ProviderPricing) -> int:
    # Usage events snapshot prices at recording time — rows can be superseded.
    return 0


def _voice_usage(db: Session, row: VoiceProfile) -> int:
    bots = db.scalar(
        select(func.count()).select_from(VoiceBot).where(
            VoiceBot.voice_id == row.id, VoiceBot.is_deleted.is_(False)
        )
    ) or 0
    settings = db.scalar(
        select(func.count()).select_from(VoiceBotSetting).where(
            VoiceBotSetting.voice_id == row.id
        )
    ) or 0
    return bots + settings


# ── Type registry ─────────────────────────────────────────────────────────────

_EDITABLE = {
    "industries": {
        "name", "description", "icon", "sort_order",
        "default_prompt_template_id", "default_guardrail_profile_id",
        "default_workflow_template_id",
    },
    "countries": {"name", "iso2", "iso3", "sort_order"},
    "data-regions": {
        "name", "description", "country_id", "country", "region", "cloud_provider",
        "storage_region", "database_region", "recording_region",
        "transcript_region", "infrastructure_ready", "sort_order",
    },
    "plans": {
        "name", "description", "price_monthly", "price_annual", "currency",
        "bot_limit", "minutes_included", "seats_included", "kb_limit",
        "storage_gb_included", "languages_included", "concurrent_call_limit",
        "monthly_call_limit", "monthly_token_limit", "monthly_embedding_limit",
        "recording_retention_days", "transcript_retention_days",
        "analytics_retention_days", "features", "overage_rates", "is_public",
        "is_recommended", "sort_order",
    },
    "ai-profiles": {
        "name", "description", "stt_provider", "stt_model", "llm_provider",
        "llm_model", "tts_provider", "tts_model", "default_voice",
        "embedding_provider", "embedding_model", "embedding_dimension",
        "reranking_model", "retrieval_top_k", "retrieval_threshold",
        "temperature", "max_output_tokens", "response_timeout_ms",
        "fallback_providers", "cost_category", "sort_order",
    },
    "providers": {
        "name", "description", "website", "requires_api_key", "secret_ref",
        "config", "sort_order",
    },
    "provider-models": {
        "display_name", "description", "languages", "codecs", "sample_rates",
        "streaming", "params_schema", "is_default", "sort_order",
    },
    "languages": {
        "name", "native_name", "iso_code", "script", "direction",
        "provider_support", "is_default", "sort_order",
    },
    "voices": {
        "name", "gender", "languages", "locale", "accent", "styles",
        "description", "latency_ms", "premium", "sample_text", "provider",
        "provider_voice_id", "speaking_rate", "pitch", "is_default",
        "sort_order", "model_codes", "provider_settings",
    },
    # is_base is intentionally not editable — USD stays the platform base.
    "currencies": {"name", "symbol", "decimal_places", "sort_order"},
    "exchange-rates": {
        "base_code", "target_code", "rate", "effective_from", "sort_order",
    },
    "provider-pricing": {
        "provider_code", "capability", "model_code", "component", "unit",
        "unit_price", "selling_price", "currency_code", "effective_from",
        "sort_order",
    },
}

_TYPES: dict[str, dict] = {
    "industries": dict(
        model=Industry, serializer=serialize_industry, usage=_industry_usage,
        perm="manage_industries", label="Industry", search=("code", "name", "description"),
    ),
    "countries": dict(
        model=Country, serializer=serialize_country, usage=_country_usage,
        perm="manage_data_regions", label="Country", search=("iso2", "iso3", "name", "region"),
    ),
    "data-regions": dict(
        model=DataRegion, serializer=serialize_data_region, usage=_region_usage,
        perm="manage_data_regions", label="Data Region", search=("code", "name", "country"),
    ),
    "plans": dict(
        model=Plan, serializer=serialize_plan, usage=_plan_usage,
        perm="manage_plans", label="Plan", search=("code", "name", "description"),
    ),
    "ai-profiles": dict(
        model=AiConfigProfile, serializer=serialize_ai_profile, usage=_ai_profile_usage,
        perm="manage_ai_profiles", label="AI Configuration Profile",
        search=("code", "name", "description"),
    ),
    "providers": dict(
        model=ProviderDef, serializer=serialize_provider, usage=_provider_usage,
        perm="manage_master_data", label="Provider", search=("code", "name", "kind"),
    ),
    "provider-models": dict(
        model=ProviderModel, serializer=serialize_provider_model,
        usage=_provider_model_usage, perm="manage_master_data",
        label="Provider Model", search=("code", "display_name", "provider_code"),
    ),
    "languages": dict(
        model=SupportedLanguage, serializer=serialize_language, usage=_language_usage,
        perm="manage_languages", label="Language", search=("code", "name", "native_name"),
    ),
    "voices": dict(
        model=VoiceProfile, serializer=serialize_voice, usage=_voice_usage,
        perm="manage_master_data", label="Voice", search=("name", "provider", "accent"),
    ),
    "currencies": dict(
        model=Currency, serializer=serialize_currency, usage=_currency_usage,
        perm="manage_currencies", label="Currency", search=("code", "name", "symbol"),
    ),
    "exchange-rates": dict(
        model=ExchangeRate, serializer=serialize_exchange_rate, usage=_exchange_rate_usage,
        perm="manage_exchange_rates", label="Exchange Rate",
        search=("base_code", "target_code", "source"),
    ),
    "provider-pricing": dict(
        model=ProviderPricing, serializer=serialize_provider_pricing,
        usage=_provider_pricing_usage, perm="manage_pricing", label="Provider Price",
        search=("provider_code", "model_code", "capability", "component"),
    ),
}

# Languages/voices don't share the exact same lifecycle columns.
_STATUSLESS = {"languages"}  # uses `enabled` instead of `status`

# Types without a user-supplied name column — their display label is derived.
_NAMELESS = {"exchange-rates", "provider-pricing"}


def _row_label(mtype: str, row) -> str:
    if mtype == "exchange-rates":
        return f"{row.base_code} → {row.target_code}"
    if mtype == "provider-pricing":
        return f"{row.provider_code}/{row.model_code or '—'} · {row.component}"
    return getattr(row, "name", None) or getattr(row, "display_name", row.id)


def _status_priority(model, mtype: str):
    """Primary ORDER BY key normalizing every lifecycle column to a status
    group: ``0`` for available records (``status == 'active'`` — or, for the
    ``enabled``-based types, ``enabled is True``) and ``1`` for every
    unavailable one (inactive / deactivated / disabled / archived).

    This is what pushes deactivated records to the bottom of *every* master
    list, regardless of their ``sort_order``. Returns ``None`` for models with
    no lifecycle column so their ordering is unaffected."""
    if mtype in _STATUSLESS:
        enabled = getattr(model, "enabled", None)
        return case((enabled.is_(True), 0), else_=1) if enabled is not None else None
    status = getattr(model, "status", None)
    return case((status == "active", 0), else_=1) if status is not None else None


# ── Value validation ──────────────────────────────────────────────────────────

SUPPORTED_CURRENCIES = ("INR", "USD", "EUR", "GBP", "AED")

# Numeric fields that must never go below zero, per master type (snake_case).
# Backend enforcement — the frontend min=0 inputs are advisory only.
_NON_NEGATIVE: dict[str, set[str]] = {
    "industries": {"sort_order"},
    "countries": {"sort_order"},
    "data-regions": {"sort_order"},
    "plans": {
        "price_monthly", "price_annual", "bot_limit", "minutes_included",
        "seats_included", "kb_limit", "storage_gb_included", "languages_included",
        "concurrent_call_limit", "monthly_call_limit", "monthly_token_limit",
        "monthly_embedding_limit", "recording_retention_days",
        "transcript_retention_days", "analytics_retention_days", "sort_order",
    },
    "ai-profiles": {
        "embedding_dimension", "retrieval_top_k", "retrieval_threshold",
        "temperature", "max_output_tokens", "response_timeout_ms", "sort_order",
    },
    "providers": {"sort_order"},
    "provider-models": {"sort_order"},
    "languages": {"sort_order"},
    "voices": {"latency_ms", "speaking_rate", "pitch", "sort_order"},
    "currencies": {"decimal_places", "sort_order"},
    "exchange-rates": {"sort_order"},   # rate itself must be > 0, checked separately
    "provider-pricing": {"sort_order"},  # unit_price must be > 0, checked separately
}

# (provider field, model field, catalog capability) triplets validated together.
_AI_PROFILE_STACK = (
    ("stt_provider", "stt_model", "stt"),
    ("tts_provider", "tts_model", "tts"),
    ("llm_provider", "llm_model", "llm"),
    ("embedding_provider", "embedding_model", "embedding"),
)

# Fields where an explicit empty string clears the column (needed when a
# provider change invalidates the previously selected model).
_CLEARABLE: dict[str, set[str]] = {
    # Clearing the default profile removes the industry's recommendation.
    "industries": {"default_guardrail_profile_id"},
    "ai-profiles": {f for pair in _AI_PROFILE_STACK for f in pair[:2]} | {"default_voice"},
    "voices": {"locale", "provider_voice_id", "accent"},
    # Clearing the selling price returns the row to cost-only (no markup).
    "provider-pricing": {"selling_price"},
}


def _camel(snake: str) -> str:
    parts = snake.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _validate_payload(db: Session, mtype: str, payload: dict, current=None) -> None:
    """Field-level validation shared by create and update.

    ``payload`` uses snake_case keys and holds only the incoming changes;
    ``current`` (the existing row, update only) supplies effective values so a
    model change is validated against the already-saved provider and vice versa.
    Raises ApiError(422) with a per-field errors list.
    """
    errors: list[dict] = []

    def effective(field: str):
        if field in payload:
            value = payload[field]
            return (str(value).strip() or None) if isinstance(value, str) else value
        return getattr(current, field, None) if current is not None else None

    for field in sorted(_NON_NEGATIVE.get(mtype, ())):
        value = payload.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            try:
                value = float(value)
                payload[field] = value
            except ValueError:
                errors.append({"field": _camel(field), "message": "Must be a number."})
                continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append({"field": _camel(field), "message": "Must be a number."})
        elif value < 0:
            errors.append({"field": _camel(field), "message": "Must be zero or greater."})

    if mtype == "industries" and payload.get("default_guardrail_profile_id"):
        profile = db.scalar(
            select(GuardrailProfile).where(
                GuardrailProfile.id == payload["default_guardrail_profile_id"],
                GuardrailProfile.is_deleted.is_(False),
            )
        )
        if profile is None or profile.status != "active":
            errors.append({
                "field": "defaultGuardrailProfileId",
                "message": "Select an active guardrail profile.",
            })

    if mtype == "plans" and payload.get("currency") is not None:
        code = str(payload["currency"]).strip().upper()
        # DB-driven currency catalog; the static tuple stays as a bootstrap
        # fallback for databases seeded before the currencies table existed.
        active_codes = set(
            db.scalars(
                select(Currency.code).where(
                    Currency.status == "active", Currency.is_deleted.is_(False)
                )
            )
        ) or set(SUPPORTED_CURRENCIES)
        if code not in active_codes:
            errors.append({
                "field": "currency",
                "message": f"Unsupported currency. Use one of: {', '.join(sorted(active_codes))}.",
            })
        else:
            payload["currency"] = code

    if mtype == "countries":
        for field, length, example in (("iso2", 2, "IN"), ("iso3", 3, "IND")):
            value = str(effective(field) or "").strip().upper()
            if len(value) != length or not value.isalpha():
                errors.append({
                    "field": field,
                    "message": f"Use a {length}-letter ISO country code, for example {example}.",
                })
            else:
                payload[field] = value
                duplicate = db.scalar(select(Country).where(getattr(Country, field) == value))
                if duplicate is not None and (current is None or duplicate.id != current.id):
                    errors.append({"field": field, "message": f"{value} is already in use."})

        name = str(effective("name") or "").strip()
        duplicate_name = db.scalar(select(Country).where(Country.name == name)) if name else None
        if duplicate_name is not None and (current is None or duplicate_name.id != current.id):
            errors.append({"field": "name", "message": f"{name} is already in use."})
        requested_region = str(payload.get("region") or "Asia").strip()
        if requested_region.casefold() != "asia":
            errors.append({
                "field": "region",
                "message": "Only the Asia region is available right now.",
            })
        payload["region"] = "Asia"

    if mtype == "data-regions":
        country_changed = (
            current is None
            or "country_id" in payload
            or "country_code" in payload  # legacy API input
            or "country" in payload       # legacy API input
        )
        if country_changed:
            selected_id = payload.get("country_id")
            selected_code = payload.get("country_code")
            selected_name = payload.get("country")
            selected = selected_id or selected_code or selected_name
            error_field = "countryId" if selected_id is not None or selected is None else "countryCode"
            if not selected:
                errors.append({
                    "field": error_field,
                    "message": "Select a country from the country master.",
                })
            else:
                country = None
                if selected_id is not None:
                    try:
                        country_id = int(selected_id)
                    except (TypeError, ValueError):
                        country_id = 0
                    country = db.get(Country, country_id) if country_id > 0 else None
                else:
                    value = str(selected).strip()
                    country = db.scalar(select(Country).where(or_(
                        func.upper(Country.iso2) == value.upper(),
                        func.upper(Country.iso3) == value.upper(),
                        Country.name == value,
                    )))
                if country is not None and (country.status != "active" or country.is_deleted):
                    country = None
                if country is None:
                    errors.append({
                        "field": error_field,
                        "message": "Select an active country from the Asia country master.",
                    })
                else:
                    # Persist the numeric FK and canonical display metadata.
                    payload["country_id"] = country.id
                    payload["country"] = country.name
                    payload["region"] = country.region

    if mtype == "languages" and payload.get("direction") not in (None, "", "ltr", "rtl"):
        errors.append({"field": "direction", "message": "Direction must be 'ltr' or 'rtl'."})

    if mtype == "ai-profiles":
        from backend.core.provider_catalog import get_model, get_provider, list_models

        for provider_field, model_field, capability in _AI_PROFILE_STACK:
            provider = effective(provider_field)
            model = effective(model_field)
            if model and not provider:
                errors.append({
                    "field": _camel(model_field),
                    "message": "Select a provider before choosing a model.",
                })
                continue
            if not provider:
                continue
            if get_provider(db, capability, provider) is None:
                errors.append({
                    "field": _camel(provider_field),
                    "message": f"Unknown or inactive {capability.upper()} provider '{provider}'.",
                })
                continue
            if model and get_model(db, capability, provider, model) is None:
                available = [m.code for m in list_models(db, capability, provider)]
                detail = (
                    f" Available models: {', '.join(available)}." if available
                    else f" '{provider}' has no configured models — add them to the provider catalog first."
                )
                errors.append({
                    "field": _camel(model_field),
                    "message": f"Model '{model}' does not belong to provider '{provider}'.{detail}",
                })

    if mtype == "provider-models":
        _validate_provider_model_payload(db, payload, effective, errors, current)

    if mtype == "voices":
        _validate_voice_payload(db, payload, effective, errors)

    if mtype == "currencies":
        _validate_currency_payload(db, payload, effective, errors, current)

    if mtype == "exchange-rates":
        _validate_exchange_rate_payload(db, payload, effective, errors, current)

    if mtype == "provider-pricing":
        _validate_provider_pricing_payload(db, payload, effective, errors, current)

    if errors:
        raise ApiError("Validation failed.", 422, errors=errors)


def _parse_effective_from(payload: dict, errors: list[dict]) -> None:
    """Normalize an incoming effectiveFrom value to a datetime in payload."""
    from datetime import datetime, timedelta

    if "effective_from" not in payload or payload["effective_from"] is None:
        return
    value = payload["effective_from"]
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            value = value.replace(tzinfo=None)
        except ValueError:
            errors.append({
                "field": "effectiveFrom",
                "message": "Use an ISO date/time, for example 2026-07-24T00:00.",
            })
            return
    if not isinstance(value, datetime):
        errors.append({"field": "effectiveFrom", "message": "Must be a date/time."})
        return
    if value.year < 2000:
        errors.append({"field": "effectiveFrom", "message": "Effective date is too far in the past."})
        return
    if value > datetime.utcnow() + timedelta(days=366):
        errors.append({
            "field": "effectiveFrom",
            "message": "Effective date cannot be more than a year in the future.",
        })
        return
    payload["effective_from"] = value


def _positive_decimal(payload: dict, field: str, camel: str, errors: list[dict],
                      max_value: float) -> None:
    """Coerce a required-positive decimal field; rejects zero and negatives."""
    from decimal import Decimal, InvalidOperation

    if field not in payload or payload[field] is None:
        return
    try:
        value = Decimal(str(payload[field]).strip())
    except (InvalidOperation, ValueError):
        errors.append({"field": camel, "message": "Must be a number."})
        return
    if value <= 0:
        errors.append({"field": camel, "message": "Must be greater than zero."})
    elif value > max_value:
        errors.append({"field": camel, "message": f"Must be at most {max_value}."})
    else:
        payload[field] = value


def _active_currency(db: Session, code: str) -> Currency | None:
    return db.scalar(
        select(Currency).where(
            Currency.code == code,
            Currency.status == "active",
            Currency.is_deleted.is_(False),
        )
    )


def _validate_currency_payload(db: Session, payload: dict, effective, errors: list[dict],
                               current) -> None:
    if current is None:
        code = str(payload.get("code") or "").strip().upper()
        if len(code) != 3 or not code.isalpha():
            errors.append({
                "field": "code",
                "message": "Use a 3-letter ISO 4217 code, for example USD.",
            })
        else:
            payload["code"] = code
            duplicate = db.scalar(select(Currency).where(Currency.code == code))
            if duplicate is not None:
                errors.append({"field": "code", "message": f"{code} already exists."})
        if not str(payload.get("symbol") or "").strip():
            errors.append({"field": "symbol", "message": "Symbol is required."})
    if "symbol" in payload and payload["symbol"] is not None:
        symbol = str(payload["symbol"]).strip()
        if not symbol or len(symbol) > 8:
            errors.append({"field": "symbol", "message": "Symbol must be 1-8 characters."})
        else:
            payload["symbol"] = symbol
    places = payload.get("decimal_places")
    if places is not None and (not isinstance(places, (int, float)) or isinstance(places, bool)
                               or int(places) != places or not 0 <= int(places) <= 4):
        errors.append({"field": "decimalPlaces", "message": "Must be a whole number from 0 to 4."})


def _validate_exchange_rate_payload(db: Session, payload: dict, effective, errors: list[dict],
                                    current) -> None:
    from datetime import datetime

    _parse_effective_from(payload, errors)
    _positive_decimal(payload, "rate", "rate", errors, max_value=10_000_000)

    for field, camel in (("base_code", "baseCode"), ("target_code", "targetCode")):
        if field in payload and payload[field] is not None:
            payload[field] = str(payload[field]).strip().upper()

    base = effective("base_code")
    target = effective("target_code")
    if current is None:
        if not base:
            errors.append({"field": "baseCode", "message": "Base currency is required."})
        if not target:
            errors.append({"field": "targetCode", "message": "Target currency is required."})
        if payload.get("rate") is None:
            errors.append({"field": "rate", "message": "Exchange rate is required."})
    if base and target and base == target:
        errors.append({
            "field": "targetCode",
            "message": "Base and target currencies must differ.",
        })
    if base and base != BASE_CURRENCY:
        errors.append({
            "field": "baseCode",
            "message": f"Rates are configured from the platform base currency ({BASE_CURRENCY}).",
        })
    for field, camel, code in (("base_code", "baseCode", base), ("target_code", "targetCode", target)):
        if code and _active_currency(db, code) is None:
            errors.append({
                "field": camel,
                "message": f"'{code}' is not an active currency. Add or activate it first.",
            })
    if errors:
        return
    # Same pair + same effective timestamp must not exist twice.
    if base and target:
        effective_from = payload.get("effective_from") or getattr(current, "effective_from", None)
        if isinstance(effective_from, datetime):
            duplicate = db.scalar(
                select(ExchangeRate).where(
                    ExchangeRate.base_code == base,
                    ExchangeRate.target_code == target,
                    ExchangeRate.effective_from == effective_from,
                    ExchangeRate.is_deleted.is_(False),
                )
            )
            if duplicate is not None and (current is None or duplicate.id != current.id):
                errors.append({
                    "field": "effectiveFrom",
                    "message": f"A {base} → {target} rate with this effective date already exists.",
                })


def _validate_provider_pricing_payload(db: Session, payload: dict, effective, errors: list[dict],
                                       current) -> None:
    from datetime import datetime

    _parse_effective_from(payload, errors)
    _positive_decimal(payload, "unit_price", "unitPrice", errors, max_value=1_000_000)
    # Optional platform selling price; an explicit "" clears the markup.
    if str(payload.get("selling_price") or "").strip() == "":
        if "selling_price" in payload:
            payload["selling_price"] = None
    else:
        _positive_decimal(payload, "selling_price", "sellingPrice", errors, max_value=1_000_000)

    capability = effective("capability")
    provider_code = effective("provider_code")
    if current is None:
        if capability not in USAGE_CAPABILITIES:
            errors.append({
                "field": "capability",
                "message": f"Capability must be one of: {', '.join(USAGE_CAPABILITIES)}.",
            })
        if not provider_code:
            errors.append({"field": "providerCode", "message": "Provider is required."})
        if payload.get("unit_price") is None:
            errors.append({"field": "unitPrice", "message": "Unit price is required."})
    else:
        for field in ("capability", "provider_code", "model_code", "component"):
            if field in payload and payload[field] != getattr(current, field):
                errors.append({
                    "field": _camel(field),
                    "message": "Cannot be changed after creation — deactivate this price "
                               "and create a new one instead.",
                })

    # AI capabilities must reference a configured provider; telephony providers
    # live outside the provider_defs catalog (kept as free codes).
    if capability in ("llm", "embedding", "stt", "tts") and provider_code:
        provider_row = db.scalar(
            select(ProviderDef).where(
                ProviderDef.kind == capability,
                ProviderDef.code == provider_code,
                ProviderDef.is_deleted.is_(False),
            )
        )
        if provider_row is None:
            errors.append({
                "field": "providerCode",
                "message": f"'{provider_code}' is not a configured {capability} provider.",
            })
        else:
            # Model-level prices must match a catalog model of that provider
            # and capability. Governance-inactive models stay priceable —
            # their historical usage still needs costing.
            model_code = str(
                payload.get("model_code")
                if "model_code" in payload
                else getattr(current, "model_code", "") or ""
            ).strip()
            if model_code:
                model_row = db.scalar(
                    select(ProviderModel).where(
                        ProviderModel.capability == capability,
                        ProviderModel.provider_code == provider_code,
                        ProviderModel.code == model_code,
                        ProviderModel.is_deleted.is_(False),
                    )
                )
                if model_row is None:
                    errors.append({
                        "field": "modelCode",
                        "message": f"Model '{model_code}' is not a configured "
                                   f"{capability} model of provider '{provider_code}'.",
                    })

    component = effective("component")
    if component is not None or current is None:
        if component not in PRICING_COMPONENTS:
            errors.append({
                "field": "component",
                "message": f"Component must be one of: {', '.join(PRICING_COMPONENTS)}.",
            })
    unit = effective("unit")
    if unit is not None or current is None:
        if unit not in PRICING_UNITS:
            errors.append({
                "field": "unit",
                "message": f"Unit must be one of: {', '.join(PRICING_UNITS)}.",
            })

    currency_code = payload.get("currency_code")
    if currency_code is not None:
        currency_code = str(currency_code).strip().upper()
        payload["currency_code"] = currency_code
        if _active_currency(db, currency_code) is None:
            errors.append({
                "field": "currencyCode",
                "message": f"'{currency_code}' is not an active currency.",
            })
        elif currency_code != BASE_CURRENCY:
            # Native non-USD prices (e.g. Sarvam's INR rates) cost through the
            # USD->currency exchange rate at usage time — require one up front
            # so events never silently land as missing_price.
            from shared.billing.currency import effective_rate

            if effective_rate(db, currency_code) is None:
                errors.append({
                    "field": "currencyCode",
                    "message": f"No {BASE_CURRENCY}→{currency_code} exchange rate is "
                               "configured. Add one under Regional & Currency Settings "
                               "before pricing in this currency.",
                })

    if "model_code" in payload:
        payload["model_code"] = str(payload["model_code"] or "").strip()
    elif current is None:
        payload["model_code"] = ""  # telephony/flat prices don't need a model

    if errors:
        return
    if current is None and capability and provider_code:
        effective_from = payload.get("effective_from")
        if isinstance(effective_from, datetime):
            duplicate = db.scalar(
                select(ProviderPricing).where(
                    ProviderPricing.provider_code == provider_code,
                    ProviderPricing.capability == capability,
                    ProviderPricing.model_code == (payload.get("model_code") or ""),
                    ProviderPricing.component == component,
                    ProviderPricing.effective_from == effective_from,
                    ProviderPricing.is_deleted.is_(False),
                )
            )
            if duplicate is not None:
                errors.append({
                    "field": "effectiveFrom",
                    "message": "A price for this provider/model/component with this "
                               "effective date already exists.",
                })


def _validate_provider_model_payload(db: Session, payload: dict, effective, errors: list[dict],
                                     current) -> None:
    """Provider-model catalog validation: the capability/provider pair must be
    coherent and list/schema-shaped fields must be well-formed. Capability and
    provider are immutable after creation (models never move between
    providers — deactivate and recreate instead)."""
    from backend.core.provider_catalog import CAPABILITIES

    if current is not None:
        for field in ("capability", "provider_code"):
            if field in payload and payload[field] != getattr(current, field):
                errors.append({
                    "field": _camel(field),
                    "message": "Cannot be changed after creation — deactivate this model "
                               "and create a new one instead.",
                })

    capability = effective("capability")
    provider_code = effective("provider_code")
    if current is None:
        if capability not in CAPABILITIES:
            errors.append({
                "field": "capability",
                "message": f"Capability must be one of: {', '.join(CAPABILITIES)}.",
            })
        provider_row = None
        if provider_code:
            provider_row = db.scalar(
                select(ProviderDef).where(
                    ProviderDef.kind == capability,
                    ProviderDef.code == provider_code,
                    ProviderDef.is_deleted.is_(False),
                )
            )
        if provider_row is None:
            errors.append({
                "field": "providerCode",
                "message": f"'{provider_code or ''}' is not a configured {capability or ''} provider.",
            })

    for field, label in (("languages", "Languages"), ("codecs", "Codecs"),
                         ("sample_rates", "Sample rates")):
        if field in payload and payload[field] is not None:
            value = payload[field]
            if not isinstance(value, list):
                errors.append({"field": _camel(field), "message": f"{label} must be a list."})
    if "params_schema" in payload and payload["params_schema"] is not None:
        if not isinstance(payload["params_schema"], dict):
            errors.append({"field": "paramsSchema", "message": "Must be an object."})
    if "streaming" in payload and payload["streaming"] is not None:
        if not isinstance(payload["streaming"], bool):
            errors.append({"field": "streaming", "message": "Must be true or false."})


def _validate_voice_payload(db: Session, payload: dict, effective, errors: list[dict]) -> None:
    """Provider-aware voice validation: the selected provider decides which
    models, languages and settings are legal. Settings are checked against the
    model's params_schema — unknown parameters and out-of-range values are
    rejected, never stored."""
    from backend.core.provider_catalog import (
        get_model, list_models, model_platform_languages, validate_params,
    )

    provider = effective("provider")
    provider_row = None
    if provider:
        provider_row = db.scalar(
            select(ProviderDef).where(
                ProviderDef.code == provider,
                ProviderDef.kind.in_(("tts", "voice")),
                ProviderDef.status == "active",
                ProviderDef.is_deleted.is_(False),
            )
        )
        if provider_row is None:
            errors.append({
                "field": "provider",
                "message": f"'{provider}' is not a configured voice/TTS provider.",
            })
            return

    model_codes = effective("model_codes") or []
    if "model_codes" in payload and payload["model_codes"] is not None:
        if not isinstance(payload["model_codes"], list) or not all(
            isinstance(m, str) and m.strip() for m in payload["model_codes"]
        ):
            errors.append({"field": "modelCodes", "message": "Must be a list of model codes."})
            return

    catalog_models = list_models(db, "tts", provider) if provider else []
    model_row = None
    if model_codes:
        if not provider:
            errors.append({"field": "modelCodes", "message": "Select a provider before choosing a model."})
            return
        if not catalog_models:
            errors.append({
                "field": "modelCodes",
                "message": f"'{provider}' has no configured TTS models — models cannot be assigned.",
            })
            return
        for code in model_codes:
            if get_model(db, "tts", provider, code) is None:
                errors.append({
                    "field": "modelCodes",
                    "message": f"Model '{code}' does not belong to provider '{provider}'. "
                               f"Available: {', '.join(m.code for m in catalog_models)}.",
                })
                return
        model_row = get_model(db, "tts", provider, model_codes[0])

    settings = effective("provider_settings")
    if "provider_settings" in payload and payload["provider_settings"] is not None:
        if not isinstance(payload["provider_settings"], dict):
            errors.append({"field": "providerSettings", "message": "Must be an object."})
            return
    if settings:
        # Settings are validated against the voice's model schema; voices
        # without an explicit model fall back to the provider's default model.
        schema_row = model_row or next(
            (m for m in catalog_models if m.is_default), catalog_models[0] if catalog_models else None
        )
        if schema_row is None:
            errors.append({
                "field": "providerSettings",
                "message": f"'{provider or 'This provider'}' has no configured models — "
                           "provider settings cannot be validated or stored.",
            })
            return
        for message in validate_params(schema_row.params_schema, settings, prefix="Settings"):
            errors.append({"field": "providerSettings", "message": message})

    locale = effective("locale")
    if locale and model_row is not None and model_row.languages:
        supported = {lang.code for lang in model_platform_languages(db, model_row)}
        if supported and locale not in supported:
            errors.append({
                "field": "locale",
                "message": f"'{locale}' is not supported by {provider}/{model_row.code}. "
                           f"Supported: {', '.join(sorted(supported))}.",
            })


def _spec(mtype: str) -> dict:
    spec = _TYPES.get(mtype)
    if spec is None:
        raise NotFoundError("Master data type")
    return spec


def _guard(mtype: str, user: User) -> None:
    from backend.core.deps import has_permission

    spec = _spec(mtype)
    if not (has_permission(user, spec["perm"]) or has_permission(user, "manage_master_data")):
        from shared.errors import ForbiddenError

        raise ForbiddenError()


def _serialize(db: Session, mtype: str, row, names: dict | None = None) -> dict:
    spec = _spec(mtype)
    usage = spec["usage"](db, row)
    if mtype in ("languages", "voices"):
        return spec["serializer"](row, usage=usage)
    return spec["serializer"](row, usage=usage, names=names)


def _user_names(db: Session, rows: list) -> dict[str, str]:
    ids = {r.created_by for r in rows if getattr(r, "created_by", None)} | {
        r.updated_by for r in rows if getattr(r, "updated_by", None)
    }
    if not ids:
        return {}
    return dict(db.execute(select(User.id, User.name).where(User.id.in_(ids))).all())


# ── List ──────────────────────────────────────────────────────────────────────


@router.get("/master/{mtype}")
def list_master(
    mtype: str,
    kind: str | None = Query(None),  # providers only
    capability: str | None = Query(None),  # provider-models + provider-pricing
    provider: str | None = Query(None),  # voices + provider-models + provider-pricing
    gender: str | None = Query(None),  # voices only
    language: str | None = Query(None),  # voices only (locale prefix or languages[])
    status_filter: str | None = Query(None, alias="status", pattern="^(active|inactive|archived)$"),
    include_inactive: bool = Query(True, alias="includeInactive"),
    params: PageParams = Depends(page_params),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    model = spec["model"]

    stmt = select(model)
    if hasattr(model, "is_deleted"):
        stmt = stmt.where(model.is_deleted.is_(False))
    if mtype == "providers" and kind:
        stmt = stmt.where(ProviderDef.kind == kind)
    if mtype == "provider-models":
        if capability:
            stmt = stmt.where(ProviderModel.capability == capability)
        if provider:
            stmt = stmt.where(ProviderModel.provider_code == provider)
    if mtype == "provider-pricing":
        if capability:
            stmt = stmt.where(ProviderPricing.capability == capability)
        if provider:
            stmt = stmt.where(ProviderPricing.provider_code == provider)
    if mtype == "voices":
        if provider:
            stmt = stmt.where(VoiceProfile.provider == provider)
        if gender:
            stmt = stmt.where(VoiceProfile.gender == gender)
        if language:
            stmt = stmt.where(or_(
                VoiceProfile.locale.like(f"{language}%"),
                func.json_contains(VoiceProfile.languages, f'"{language}"'),
            ))
    if status_filter:
        if mtype in _STATUSLESS:
            stmt = stmt.where(model.enabled.is_(status_filter == "active"))
        else:
            stmt = stmt.where(model.status == status_filter)
    if not include_inactive:
        if mtype in _STATUSLESS:
            stmt = stmt.where(model.enabled.is_(True))
        else:
            stmt = stmt.where(model.status == "active")
    if params.search:
        like = f"%{params.search}%"
        search_clauses = [getattr(model, f).like(like) for f in spec["search"]]
        if mtype == "voices":
            search_clauses.append(VoiceProfile.id.like(like))  # search by voice ID too
        stmt = stmt.where(or_(*search_clauses))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    sortable = {
        "name": getattr(model, "name", None),
        "code": getattr(model, "code", None),
        "iso2": getattr(model, "iso2", None),
        "iso3": getattr(model, "iso3", None),
        "createdAt": model.created_at,
        "updatedAt": model.updated_at,
        "sortOrder": getattr(model, "sort_order", None),
    }
    default_col = getattr(model, "sort_order", model.created_at)
    if params.sort_by and sortable.get(params.sort_by) is not None:
        col = sortable[params.sort_by]
        descending = params.sort_dir == "desc"
    else:
        # Master lists default to ascending sort order: lower values first.
        col = default_col
        descending = False

    order = []
    # Every Platform Configuration master list surfaces available records
    # (active / enabled) before any unavailable one (inactive, deactivated,
    # disabled, archived), so deactivating an entry drops it to the bottom of
    # the list immediately and it stays there on reload. Ordering *within* a
    # status group follows the normal sort-order/name/id rules below. This runs
    # even when an explicit sortBy is supplied, so the active-first guarantee
    # holds for search, filtered and paginated responses alike.
    status_priority = _status_priority(model, mtype)
    if status_priority is not None:
        order.append(status_priority.asc())

    order.append(col.desc() if descending else col.asc())
    # Stable secondary sort so equal primary values keep a deterministic order:
    # by human-readable label (name / display_name) then the immutable id.
    label_col = getattr(model, "name", None)
    if label_col is None:
        label_col = getattr(model, "display_name", None)
    if label_col is not None and label_col is not col:
        order.append(label_col.asc())
    order.append(model.id.asc())
    stmt = stmt.order_by(*order)

    rows = db.scalars(stmt.offset(params.offset).limit(params.page_size)).all()
    names = _user_names(db, rows)
    return paginated(
        [_serialize(db, mtype, r, names) for r in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


# ── Create / update ───────────────────────────────────────────────────────────


class MasterPayload(BaseModel):
    """Free-shape payload — validated per type against the editable-field set."""

    model_config = {"extra": "allow"}

    code: str | None = Field(default=None, max_length=50)
    name: str | None = Field(default=None, max_length=150)
    kind: str | None = None  # providers only


def _camel_to_snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _apply_fields(row, payload: dict, allowed: set[str], clearable: set[str] = frozenset()) -> dict:
    """Apply snake_case payload values to the row. `None` values are ignored
    (partial updates), except fields in `clearable` where an explicit empty
    string / None clears the column."""
    changed = {}
    for snake, value in payload.items():
        if snake not in allowed:
            continue
        if isinstance(value, str) and value.strip() == "" and snake in clearable:
            value = None
        elif value is None:
            if snake not in clearable:
                continue
        if getattr(row, snake, None) != value:
            changed[snake] = value
            setattr(row, snake, value)
    return changed


_ID_PREFIX = {
    "industries": "ind", "data-regions": "dr", "plans": "pl",
    "ai-profiles": "aip", "providers": "prov", "provider-models": "pm",
    "languages": "lang", "voices": "vp",
    "currencies": "cur", "exchange-rates": "fxr", "provider-pricing": "ppr",
}


@router.post("/master/{mtype}", status_code=201)
def create_master(
    mtype: str,
    body: MasterPayload,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    model = spec["model"]
    payload = {_camel_to_snake(k): v for k, v in body.model_dump(exclude_unset=True).items()}

    name = (payload.get("name") or "").strip()
    if not name and mtype not in _NAMELESS:
        raise ApiError("Name is required.", 422, errors=[{"field": "name", "message": "Name is required."}])
    _validate_payload(db, mtype, payload)

    row = model() if mtype == "countries" else model(id=new_id(_ID_PREFIX[mtype]))
    if hasattr(model, "code"):
        code = (payload.get("code") or "").strip().replace(" ", "_")
        if mtype == "languages":
            # Locale codes keep their case (en-IN); column is 15 chars.
            if len(code) > 15:
                raise ApiError("Language code must be at most 15 characters.", 422)
        elif mtype == "currencies":
            code = code.upper()  # ISO 4217 — validated upstream
        elif mtype != "provider-models":
            # Provider model codes are provider wire codes — case preserved.
            code = code.lower()
        if mtype != "voices":
            if not code:
                raise ApiError("Code is required.", 422, errors=[{"field": "code", "message": "Code is required."}])
            dup_stmt = select(model).where(model.code == code)
            if mtype == "providers":
                p_kind = payload.get("kind")
                if p_kind not in ("voice", "stt", "tts", "llm", "embedding"):
                    raise ApiError("kind must be one of voice, stt, tts, llm, embedding.", 422)
                dup_stmt = dup_stmt.where(ProviderDef.kind == p_kind)
                row.kind = p_kind
            if mtype == "provider-models":
                # Uniqueness is scoped per provider+capability (validated above).
                dup_stmt = dup_stmt.where(
                    ProviderModel.provider_code == payload.get("provider_code"),
                    ProviderModel.capability == payload.get("capability"),
                )
                row.provider_code = payload.get("provider_code")
                row.capability = payload.get("capability")
            if db.scalar(dup_stmt) is not None:
                raise ApiError(
                    f"A {spec['label'].lower()} with code '{code}' already exists.", 409
                )
            row.code = code
    if mtype == "provider-models":
        row.display_name = name
    elif mtype not in _NAMELESS:
        row.name = name
    _apply_fields(row, payload, _EDITABLE[mtype], _CLEARABLE.get(mtype, frozenset()))
    if hasattr(row, "created_by"):
        row.created_by = user.id
    db.add(row)
    if mtype == "countries":
        db.flush()  # Numeric auto-increment ID is needed by the audit record.
    label = name or _row_label(mtype, row)
    record_audit(
        db, user=user, action=f"Created {spec['label'].lower()}",
        entity_type=f"master:{mtype}", entity_id=str(row.id), target_label=label,
        new_value={"name": label, "code": getattr(row, "code", None),
                   "iso2": getattr(row, "iso2", None), "iso3": getattr(row, "iso3", None)},
        request=request,
    )
    db.commit()
    return ok(_serialize(db, mtype, row))


def _get_row(db: Session, mtype: str, item_id: str):
    model = _spec(mtype)["model"]
    try:
        identity = int(item_id) if mtype == "countries" else item_id
    except ValueError:
        identity = -1
    row = db.get(model, identity)
    if row is None or (hasattr(row, "is_deleted") and row.is_deleted):
        raise NotFoundError(_spec(mtype)["label"])
    return row


@router.patch("/master/{mtype}/{item_id}")
def update_master(
    mtype: str,
    item_id: str,
    body: MasterPayload,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    row = _get_row(db, mtype, item_id)
    payload = {_camel_to_snake(k): v for k, v in body.model_dump(exclude_unset=True).items()}
    _validate_payload(db, mtype, payload, current=row)
    changed = _apply_fields(row, payload, _EDITABLE[mtype], _CLEARABLE.get(mtype, frozenset()))
    if mtype == "countries" and "name" in changed:
        # DataRegion.country is a compatibility/display snapshot. Keep it in
        # sync with the canonical country master when a name is corrected.
        for data_region in db.scalars(
            select(DataRegion).where(DataRegion.country_id == row.id)
        ).all():
            data_region.country = row.name
    if changed.get("is_default") and mtype in ("voices", "languages", "provider-models"):
        # Exactly one default at a time — per provider for voices (a default
        # Sarvam speaker and a default ElevenLabs voice coexist), per
        # provider+capability for provider models, platform-wide for languages.
        model = spec["model"]
        query = select(model).where(model.is_default.is_(True))
        if mtype == "voices":
            query = query.where(model.provider == row.provider)
        if mtype == "provider-models":
            query = query.where(
                model.provider_code == row.provider_code,
                model.capability == row.capability,
            )
        for other in db.scalars(query).all():
            if other.id != row.id:
                other.is_default = False
    if hasattr(row, "updated_by"):
        row.updated_by = user.id
    if changed:
        record_audit(
            db, user=user, action=f"Updated {spec['label'].lower()}",
            entity_type=f"master:{mtype}", entity_id=str(row.id),
            target_label=_row_label(mtype, row),
            new_value={
                k: (v if isinstance(v, (str, int, float, bool, type(None))) else str(v))
                for k, v in changed.items() if not isinstance(v, (dict, list))
            } or {"fields": list(changed)},
            request=request,
        )
    db.commit()
    return ok(_serialize(db, mtype, row))


# ── Lifecycle: activate / deactivate / archive ────────────────────────────────


class StatusRequest(BaseModel):
    status: str = Field(pattern="^(active|inactive|archived)$")


@router.post("/master/{mtype}/{item_id}/status")
def set_master_status(
    mtype: str,
    item_id: str,
    body: StatusRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    row = _get_row(db, mtype, item_id)
    if mtype == "currencies" and row.is_base and body.status != "active":
        raise ApiError(
            "The platform base currency cannot be deactivated.", 422,
            errors=[{"field": "status", "message": "The platform base currency cannot be deactivated."}],
        )
    if mtype in _STATUSLESS:
        before = "active" if row.enabled else "inactive"
        row.enabled = body.status == "active"
    else:
        before = row.status
        row.status = body.status
    if hasattr(row, "updated_by"):
        row.updated_by = user.id
    action = {"active": "Activated", "inactive": "Deactivated", "archived": "Archived"}[body.status]
    record_audit(
        db, user=user, action=f"{action} {spec['label'].lower()}",
        entity_type=f"master:{mtype}", entity_id=str(row.id),
        target_label=_row_label(mtype, row),
        previous_value={"status": before}, new_value={"status": body.status},
        request=request,
    )
    db.commit()
    if mtype in ("providers", "provider-models", "voices"):
        # Governance change: cached runtime snapshots must not keep serving a
        # provider/model that was just deactivated.
        from shared.bot_config import invalidate_all_bot_configs_sync

        invalidate_all_bot_configs_sync()
    return ok(_serialize(db, mtype, row))


@router.delete("/master/{mtype}/{item_id}")
def delete_master(
    mtype: str,
    item_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    spec = _spec(mtype)
    row = _get_row(db, mtype, item_id)
    if mtype == "currencies" and row.is_base:
        raise ApiError("The platform base currency cannot be deleted.", 409)
    usage = spec["usage"](db, row)
    if usage > 0:
        raise ApiError(
            f"This {spec['label'].lower()} is used by {usage} existing "
            f"record{'s' if usage != 1 else ''} and cannot be deleted. "
            "Deactivate or archive it instead.",
            409,
        )
    # Unreferenced → archive (soft delete). Hard removal stays disabled.
    if mtype in _STATUSLESS:
        row.enabled = False
    if hasattr(row, "is_deleted"):
        from backend.core.softdelete import soft_delete

        soft_delete(row, user)
    elif hasattr(row, "status"):
        row.status = "archived"
    record_audit(
        db, user=user, action=f"Archived {spec['label'].lower()}",
        entity_type=f"master:{mtype}", entity_id=str(row.id),
        target_label=_row_label(mtype, row),
        request=request,
    )
    db.commit()
    if mtype in ("providers", "provider-models", "voices"):
        from shared.bot_config import invalidate_all_bot_configs_sync

        invalidate_all_bot_configs_sync()
    return ok({"archived": True, "id": row.id})


# ── Audit trail per record ────────────────────────────────────────────────────


@router.get("/master/{mtype}/{item_id}/audit")
def master_audit(
    mtype: str,
    item_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _guard(mtype, user)
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.entity_type == f"master:{mtype}", AuditLog.entity_id == item_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
    ).all()
    return ok([
        {
            "id": a.id, "actor": a.actor_name or "System", "action": a.action,
            "previousValue": a.previous_value, "newValue": a.new_value,
            "time": a.created_at.isoformat() + "Z",
        }
        for a in rows
    ])


# ── Plan extras: duplicate + tenants using ────────────────────────────────────


@router.post("/master/plans/{item_id}/duplicate", status_code=201)
def duplicate_plan(
    item_id: str,
    request: Request,
    user: User = Depends(require_permission("manage_plans", "manage_master_data")),
    db: Session = Depends(get_db),
):
    src = _get_row(db, "plans", item_id)
    base_code = f"{src.code}_copy"
    code, n = base_code, 2
    while db.scalar(select(Plan).where(Plan.code == code)) is not None:
        code, n = f"{base_code}{n}", n + 1
    clone = Plan(id=new_id("pl"), code=code, name=f"{src.name} (copy)", status="inactive",
                 created_by=user.id)
    for field in _EDITABLE["plans"] - {"name"}:
        setattr(clone, field, getattr(src, field))
    clone.is_recommended = False
    db.add(clone)
    record_audit(
        db, user=user, action="Duplicated plan", entity_type="master:plans",
        entity_id=clone.id, target_label=clone.name,
        new_value={"from": src.code, "code": code}, request=request,
    )
    db.commit()
    return ok(_serialize(db, "plans", clone))


@router.get("/master/plans/{item_id}/tenants")
def plan_tenants(
    item_id: str,
    user: User = Depends(require_permission("manage_plans", "manage_master_data")),
    db: Session = Depends(get_db),
):
    plan = _get_row(db, "plans", item_id)
    rows = db.execute(
        select(Tenant.id, Tenant.name, Tenant.domain, Subscription.status, Subscription.mrr)
        .join(Subscription, Subscription.tenant_id == Tenant.id)
        .where(Subscription.plan_id == plan.id, Subscription.is_deleted.is_(False),
               Tenant.is_deleted.is_(False))
        .order_by(Tenant.name)
    ).all()
    return ok([
        {"id": r[0], "name": r[1], "domain": r[2], "subscriptionStatus": r[3], "mrr": float(r[4])}
        for r in rows
    ])


# ── Onboarding options (DB-driven, active values only) ────────────────────────


@router.get("/onboarding/options")
def onboarding_options(
    user: User = Depends(require_permission("tenants.manage", "manage_master_data")),
    db: Session = Depends(get_db),
):
    industries = db.scalars(
        select(Industry).where(Industry.is_deleted.is_(False), Industry.status == "active")
        .order_by(Industry.sort_order)
    ).all()
    regions = db.scalars(
        select(DataRegion).where(DataRegion.is_deleted.is_(False), DataRegion.status == "active")
        .order_by(DataRegion.sort_order)
    ).all()
    plans = db.scalars(
        select(Plan).where(Plan.is_deleted.is_(False), Plan.status == "active",
                           Plan.is_public.is_(True))
        .order_by(Plan.sort_order)
    ).all()
    profiles = db.scalars(
        select(AiConfigProfile).where(
            AiConfigProfile.is_deleted.is_(False), AiConfigProfile.status == "active"
        ).order_by(AiConfigProfile.sort_order)
    ).all()
    languages = db.scalars(
        select(SupportedLanguage).where(SupportedLanguage.enabled.is_(True))
        .order_by(SupportedLanguage.sort_order, SupportedLanguage.code)
    ).all()
    guardrail_profiles = db.scalars(
        select(GuardrailProfile).where(
            GuardrailProfile.is_deleted.is_(False),
            GuardrailProfile.status == "active",
        ).order_by(GuardrailProfile.created_at)
    ).all()
    return ok({
        "industries": [
            {"code": i.code, "name": i.name, "icon": i.icon or "",
             "defaultGuardrailProfileId": i.default_guardrail_profile_id or ""}
            for i in industries
        ],
        "dataRegions": [
            {"code": r.code, "name": r.name, "infrastructureReady": r.infrastructure_ready}
            for r in regions
        ],
        "plans": [
            {"code": p.code, "name": p.name, "description": p.description or "",
             "priceMonthly": float(p.price_monthly), "minutesIncluded": p.minutes_included,
             "botLimit": p.bot_limit, "seatsIncluded": p.seats_included,
             "isRecommended": p.is_recommended}
            for p in plans
        ],
        "aiProfiles": [
            {"code": a.code, "name": a.name, "description": a.description or "",
             "costCategory": a.cost_category}
            for a in profiles
        ],
        "languages": [
            {"code": l.code, "name": l.name, "nativeName": l.native_name or "",
             "direction": l.direction}
            for l in languages
        ],
        "guardrailProfiles": [
            {"id": p.id, "code": p.code, "name": p.name,
             "description": p.description or ""}
            for p in guardrail_profiles
        ],
    })
