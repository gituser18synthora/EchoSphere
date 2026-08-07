"""Deepgram Flux conversational STT: provider activation, models, pricing.

Adds Deepgram Flux as a first-class realtime STT option:

- activates the ``deepgram`` STT provider (governance matrix now allows two
  STT vendors: Sarvam and Deepgram — mirrored in
  ``backend/seeds/provider_catalog_seed.ALLOWED_ACTIVE_PROVIDERS``);
- catalogues ``flux-general-multi`` (default, Hindi/Hinglish/English calling)
  and ``flux-general-en`` with the voice-agent parameter schema
  (eot_threshold / eager_eot_threshold / eot_timeout_ms / language_hints);
- demotes ``nova-3``'s is_default flag (Flux multi is the provider default);
- writes the official pay-as-you-go prices (deepgram.com/pricing, verified
  2026-08): flux-general-multi $0.0078/min, flux-general-en $0.0065/min.

Rollback removes the Flux models/prices it introduced and re-deactivates the
provider; operator-managed rows that predate this migration are untouched.

Revision ID: e9a1c3b5d7f9
Revises: d8f0b2c4e6a8
Create Date: 2026-08-07
"""

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "e9a1c3b5d7f9"
down_revision = "d8f0b2c4e6a8"
branch_labels = None
depends_on = None

# Schema constants are inlined on purpose: a migration must keep describing
# the world as it was when it ran, even after the seed constant moves on.
_FLUX_SCHEMA = {
    "eot_threshold": {
        "type": "number", "min": 0.5, "max": 0.9, "default": 0.7, "step": 0.05,
        "label": "End-of-turn threshold",
        "help": "Confidence Flux needs before ending the caller's turn. "
                "Lower answers sooner but may cut into natural pauses.",
    },
    "eager_eot_threshold": {
        "type": "number", "min": 0.3, "max": 0.9, "default": 0.6, "step": 0.05,
        "label": "Eager end-of-turn threshold", "advanced": True,
        "help": "Enables EagerEndOfTurn: orchestration starts speculatively "
                "before the turn is confirmed (lower = faster responses, more "
                "speculative decision calls).",
    },
    "eot_timeout_ms": {
        "type": "integer", "min": 500, "max": 60000, "default": 3000,
        "label": "End-of-turn timeout (ms)", "advanced": True,
        "help": "Silence after speech that force-ends the turn regardless of "
                "end-of-turn confidence.",
    },
    "language_hints": {
        "type": "string_list", "max_items": 8, "max_length": 8,
        "values": ["de", "en", "es", "fr", "hi", "it", "ja", "nl", "pt", "ru"],
        "label": "Language hints", "advanced": True,
        "help": "Bias multilingual detection toward these languages. Defaults "
                "to the bot's configured languages (e.g. hi, en).",
    },
}

# (code, display, languages, is_default, sort, description, price_per_minute)
_FLUX_MODELS = (
    (
        "flux-general-multi", "Flux (multilingual)", [], True, 0,
        "Deepgram Flux multilingual conversational STT (/v2/listen): "
        "model-integrated turn detection (EndOfTurn / EagerEndOfTurn / "
        "TurnResumed), per-turn language detection. Recommended for "
        "Hindi/Hinglish/English voice agents.",
        "0.0078",
    ),
    (
        "flux-general-en", "Flux (English)", ["en"], False, 1,
        "Deepgram Flux English-only conversational STT (/v2/listen) with "
        "model-integrated turn detection. Use flux-general-multi for "
        "Hindi/Hinglish callers.",
        "0.0065",
    ),
)

_SAMPLE_RATES = [8000, 16000, 24000, 44100, 48000]


def _utc_now() -> datetime:
    """Naive UTC — `effective_from` must never use the server-local NOW()
    default, or a server running ahead of UTC dates the row into the future
    and the costing engine silently excludes it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _usd_exists(bind) -> bool:
    """On a fresh DB `alembic upgrade head` runs before the bootstrap seed,
    so `currencies` is empty and the pricing FK would reject inserts; the
    seed writes these same official prices itself."""
    return bind.execute(
        sa.text("SELECT code FROM currencies WHERE code = 'USD'")
    ).first() is not None


def _upsert_price(bind, model: str, price: str) -> None:
    """Official per-minute price, preserving any existing row's identity."""
    existing = bind.execute(
        sa.text(
            "SELECT id, unit, unit_price, effective_from FROM provider_pricing "
            "WHERE provider_code = 'deepgram' AND capability = 'stt' "
            "AND model_code = :model AND component = 'audio_seconds' "
            "AND is_deleted = 0"
        ),
        {"model": model},
    ).first()
    now = _utc_now()
    if existing is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_pricing (id, provider_code, capability, "
                "model_code, component, unit, unit_price, currency_code, "
                "effective_from, status, sort_order, created_at, updated_at, "
                "is_deleted) VALUES (:id, 'deepgram', 'stt', :model, "
                "'audio_seconds', 'per_minute', :price, 'USD', :now, 'active', "
                "0, :now, :now, 0)"
            ),
            {"id": f"ppr_{uuid.uuid4().hex[:12]}", "model": model,
             "price": price, "now": now},
        )
        return
    row_id, current_unit, current_price, effective_from = existing
    unchanged = current_unit == "per_minute" and float(current_price) == float(price)
    if unchanged and effective_from is not None and effective_from <= now:
        return
    bind.execute(
        sa.text(
            "UPDATE provider_pricing SET unit = 'per_minute', "
            "unit_price = :price, status = 'active', effective_from = :now, "
            "updated_at = :now WHERE id = :id"
        ),
        {"price": price, "id": row_id, "now": now},
    )


def upgrade() -> None:
    bind = op.get_bind()
    now = _utc_now()

    # ── 1. Governance: activate the Deepgram STT provider ─────────────────
    bind.execute(sa.text(
        "UPDATE provider_defs SET status = 'active', updated_at = :now "
        "WHERE kind = 'stt' AND code = 'deepgram' AND is_deleted = 0 "
        "AND status != 'active'"
    ), {"now": now})

    # ── 2. Flux model catalog rows ────────────────────────────────────────
    for code, display, languages, is_default, sort, description, _ in _FLUX_MODELS:
        exists = bind.execute(
            sa.text(
                "SELECT id FROM provider_models WHERE provider_code = 'deepgram' "
                "AND capability = 'stt' AND code = :code"
            ),
            {"code": code},
        ).first()
        if exists is not None:
            # Operator-managed row: fill an empty description only.
            bind.execute(
                sa.text(
                    "UPDATE provider_models SET description = :description "
                    "WHERE id = :id AND (description IS NULL OR description = '')"
                ),
                {"description": description, "id": exists[0]},
            )
            continue
        bind.execute(
            sa.text(
                "INSERT INTO provider_models (id, provider_code, capability, "
                "code, display_name, description, languages, codecs, "
                "sample_rates, streaming, params_schema, is_default, status, "
                "sort_order, created_at, updated_at, is_deleted) VALUES "
                "(:id, 'deepgram', 'stt', :code, :display_name, :description, "
                ":languages, :codecs, :sample_rates, 1, :params_schema, "
                ":is_default, 'active', :sort_order, :now, :now, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "code": code, "display_name": display,
                "description": description,
                "languages": json.dumps(languages),
                "codecs": json.dumps(["linear16"]),
                "sample_rates": json.dumps(_SAMPLE_RATES),
                "params_schema": json.dumps(_FLUX_SCHEMA),
                "is_default": 1 if is_default else 0,
                "sort_order": sort, "now": now,
            },
        )

    # Flux multi is the Deepgram default; nova-3 loses the flag it held while
    # the provider was governance-inactive.
    bind.execute(sa.text(
        "UPDATE provider_models SET is_default = 0, updated_at = :now "
        "WHERE provider_code = 'deepgram' AND capability = 'stt' "
        "AND code = 'nova-3' AND is_default = 1"
    ), {"now": now})

    # ── 3. Official prices ────────────────────────────────────────────────
    if not _usd_exists(bind):
        return  # fresh database — the seed writes these prices
    for code, _, _, _, _, _, price in _FLUX_MODELS:
        _upsert_price(bind, code, price)


def downgrade() -> None:
    bind = op.get_bind()
    now = _utc_now()
    for code, *_ in _FLUX_MODELS:
        bind.execute(sa.text(
            "DELETE FROM provider_pricing WHERE provider_code = 'deepgram' "
            "AND capability = 'stt' AND model_code = :code"
        ), {"code": code})
        bind.execute(sa.text(
            "DELETE FROM provider_models WHERE provider_code = 'deepgram' "
            "AND capability = 'stt' AND code = :code"
        ), {"code": code})
    bind.execute(sa.text(
        "UPDATE provider_models SET is_default = 1, updated_at = :now "
        "WHERE provider_code = 'deepgram' AND capability = 'stt' "
        "AND code = 'nova-3'"
    ), {"now": now})
    bind.execute(sa.text(
        "UPDATE provider_defs SET status = 'inactive', updated_at = :now "
        "WHERE kind = 'stt' AND code = 'deepgram'"
    ), {"now": now})
