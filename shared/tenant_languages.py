"""Tenant language assignments and catalog visibility.

``TenantSetting.default_languages`` is the language entitlement selected by a
Super Admin during onboarding.  ``None`` is kept as a backwards-compatible
"not assigned yet" state for legacy tenants; an explicit list restricts new
bot/prompt choices to that list.
"""

from sqlalchemy import select

from shared.models import SupportedLanguage, TenantSetting


def normalize_language_codes(codes: list[str] | None) -> list[str]:
    """Trim and order-preserving de-duplicate locale codes."""
    return list(dict.fromkeys(
        str(code).strip() for code in (codes or []) if str(code).strip()
    ))


def tenant_language_assignment(session, tenant_id: str) -> list[str] | None:
    """Return the explicit assignment, or ``None`` for a legacy tenant."""
    value = session.scalar(
        select(TenantSetting.default_languages)
        .where(TenantSetting.tenant_id == tenant_id)
    )
    if value is None:
        return None
    return normalize_language_codes(value)


def tenant_allowed_language_codes(
    session, tenant_id: str, *, include_disabled: bool = False,
) -> set[str] | None:
    """Codes visible to a tenant; ``None`` means the legacy unrestricted set."""
    assigned = tenant_language_assignment(session, tenant_id)
    if assigned is None:
        return None
    if include_disabled or not assigned:
        return set(assigned)
    enabled = set(session.scalars(
        select(SupportedLanguage.code).where(
            SupportedLanguage.code.in_(assigned),
            SupportedLanguage.enabled.is_(True),
        )
    ).all())
    return enabled


def validate_language_assignment(
    session,
    codes: list[str] | None,
    *,
    existing: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Normalize an assignment and return codes that cannot be newly assigned.

    Existing languages may be retained after a platform administrator disables
    them; a disabled or unknown language can never be newly assigned.
    """
    normalized = normalize_language_codes(codes)
    rows = session.execute(
        select(SupportedLanguage.code, SupportedLanguage.enabled)
        .where(SupportedLanguage.code.in_(normalized))
    ).all() if normalized else []
    states = {row.code: row.enabled for row in rows}
    retained = set(normalize_language_codes(existing))
    invalid = [
        code for code in normalized
        if code not in states or (not states[code] and code not in retained)
    ]
    return normalized, invalid
