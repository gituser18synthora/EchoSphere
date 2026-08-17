"""Travel and Hospitality guardrail profile.

Adds the industry profile alongside the seeded Standard / Healthcare /
Finance ones (``backend/seeds/base_seed.GUARDRAIL_PROFILES``), plus the
travel-specific ``booking_commitment_restriction`` rule it carries: the bot
may state a tool-confirmed booking, but may never guarantee a booking,
promise a refund, waive a cancellation fee or offer a free upgrade on its own
(enforced in ``shared/guardrails/engine._BOOKING_COMMITMENT_RES``).

The bootstrap seed creates these rows on a fresh database; this migration is
for long-lived ones, where the seed's create-only contract would leave the
``travel_hospitality`` industry pointing at Standard forever. The industry
default is re-pointed only when it is unset or still on the seeded Standard
fallback — a Super Admin's explicit choice of any other profile is left
alone, matching the seed's "never overwrite an operator decision" rule.

Idempotent: every insert is guarded by an existence check.

Revision ID: c9e1a3b5d7f2
Revises: b8d0f2a4c6e8
Create Date: 2026-08-14
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "c9e1a3b5d7f2"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None

_PROFILE_CODE = "travel_hospitality"
_PROFILE_NAME = "Travel and Hospitality"
_PROFILE_DESC = (
    "Standard plus booking & payment restrictions — no guaranteed bookings, "
    "refunds or free upgrades by voice, and card numbers are never collected."
)

# The new travel-specific rule: (code, name, category, description, enforcement).
_NEW_GUARDRAIL = (
    "booking_commitment_restriction",
    "Booking & fare commitment restriction",
    "Compliance",
    "Blocks guaranteed bookings, refunds, fee waivers or free upgrades the "
    "bot cannot verify; tool-confirmed facts may still be stated.",
    "block",
)

# Profile membership. Mandatory platform rules are implied everywhere and are
# deliberately not listed (see shared/guardrails/loader.MANDATORY_FLOOR).
_PROFILE_RULE_CODES = (
    "profanity_deescalation",
    "payment_collection_restriction",
    "booking_commitment_restriction",
)

_INDUSTRY_CODE = "travel_hospitality"


def _scalar(bind, sql: str, **params):
    return bind.execute(sa.text(sql), params).scalar()


def upgrade() -> None:
    bind = op.get_bind()

    code, name, category, description, enforcement = _NEW_GUARDRAIL
    guardrail_id = _scalar(
        bind, "SELECT id FROM guardrails WHERE code = :code OR name = :name",
        code=code, name=name,
    )
    if guardrail_id is None:
        guardrail_id = f"gr_{uuid.uuid4().hex[:12]}"
        bind.execute(
            sa.text(
                "INSERT INTO guardrails (id, code, name, category, description, "
                "enforcement, enabled, is_mandatory, triggers_30d, is_deleted) "
                "VALUES (:id, :code, :name, :category, :description, "
                ":enforcement, 1, 0, 0, 0)"
            ),
            {"id": guardrail_id, "code": code, "name": name, "category": category,
             "description": description, "enforcement": enforcement},
        )

    profile_id = _scalar(
        bind, "SELECT id FROM guardrail_profiles WHERE code = :code",
        code=_PROFILE_CODE,
    )
    if profile_id is None:
        profile_id = f"gp_{uuid.uuid4().hex[:12]}"
        bind.execute(
            sa.text(
                "INSERT INTO guardrail_profiles (id, code, name, description, "
                "status, version, is_deleted) "
                "VALUES (:id, :code, :name, :description, 'active', 1, 0)"
            ),
            {"id": profile_id, "code": _PROFILE_CODE, "name": _PROFILE_NAME,
             "description": _PROFILE_DESC},
        )

    for rule_code in _PROFILE_RULE_CODES:
        rule_id = _scalar(
            bind,
            "SELECT id FROM guardrails WHERE code = :code AND is_deleted = 0",
            code=rule_code,
        )
        if rule_id is None:
            continue  # pre-code database; the bootstrap seed links it later
        linked = _scalar(
            bind,
            "SELECT id FROM guardrail_profile_rules "
            "WHERE profile_id = :profile_id AND guardrail_id = :guardrail_id",
            profile_id=profile_id, guardrail_id=rule_id,
        )
        if linked is None:
            bind.execute(
                sa.text(
                    "INSERT INTO guardrail_profile_rules "
                    "(id, profile_id, guardrail_id) "
                    "VALUES (:id, :profile_id, :guardrail_id)"
                ),
                {"id": f"gpr_{uuid.uuid4().hex[:12]}", "profile_id": profile_id,
                 "guardrail_id": rule_id},
            )

    standard_id = _scalar(
        bind, "SELECT id FROM guardrail_profiles WHERE code = 'standard'",
    )
    bind.execute(
        sa.text(
            "UPDATE industries SET default_guardrail_profile_id = :profile_id "
            "WHERE code = :industry AND is_deleted = 0 AND ("
            "  default_guardrail_profile_id IS NULL"
            "  OR default_guardrail_profile_id = :standard_id)"
        ),
        {"profile_id": profile_id, "industry": _INDUSTRY_CODE,
         "standard_id": standard_id},
    )


def downgrade() -> None:
    bind = op.get_bind()

    profile_id = _scalar(
        bind, "SELECT id FROM guardrail_profiles WHERE code = :code",
        code=_PROFILE_CODE,
    )
    if profile_id is not None:
        standard_id = _scalar(
            bind, "SELECT id FROM guardrail_profiles WHERE code = 'standard'",
        )
        bind.execute(
            sa.text(
                "UPDATE industries SET default_guardrail_profile_id = :standard_id "
                "WHERE default_guardrail_profile_id = :profile_id"
            ),
            {"standard_id": standard_id, "profile_id": profile_id},
        )
        # Tenants/bots still assigned to the profile keep it: dropping a live
        # assignment would silently weaken their enforcement.
        in_use = _scalar(
            bind,
            "SELECT COUNT(*) FROM tenants WHERE guardrail_profile_id = :id",
            id=profile_id,
        ) or 0
        in_use += _scalar(
            bind,
            "SELECT COUNT(*) FROM voice_bots WHERE guardrail_profile_id = :id",
            id=profile_id,
        ) or 0
        if not in_use:
            bind.execute(
                sa.text("DELETE FROM guardrail_profile_rules WHERE profile_id = :id"),
                {"id": profile_id},
            )
            bind.execute(
                sa.text("DELETE FROM guardrail_profiles WHERE id = :id"),
                {"id": profile_id},
            )

    code = _NEW_GUARDRAIL[0]
    guardrail_id = _scalar(
        bind, "SELECT id FROM guardrails WHERE code = :code", code=code,
    )
    if guardrail_id is not None:
        still_linked = _scalar(
            bind,
            "SELECT COUNT(*) FROM guardrail_profile_rules WHERE guardrail_id = :id",
            id=guardrail_id,
        ) or 0
        if not still_linked:
            bind.execute(
                sa.text("DELETE FROM guardrails WHERE id = :id"), {"id": guardrail_id},
            )
