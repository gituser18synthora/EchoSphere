"""Refresh OpenAI provider pricing to the official published rates.

Revision ID: d6f8a0b2c4e6
Revises: b3e5a7c9d1f3
Create Date: 2026-07-31

Data-only — no schema change. `provider_pricing` already carries the
input_tokens / cached_input_tokens / output_tokens components and the
per_1m_tokens unit this revision needs; only the rows were stale.

Verified against developers.openai.com/api/docs/pricing on 2026-07-31:

1. LLM prices move off the single blended per-1K-token rate onto OpenAI's
   own three-way split (input / cached input / output) quoted per 1M tokens.
   The blended `tokens` rows for gpt-4o, gpt-4o-mini and gpt-4.1-mini are
   deactivated, not deleted: usage events reference the price row they were
   costed with, and `shared.billing.pricing` only reads active rows.
2. Current-generation models missing from the catalog are inserted so their
   usage can be priced. The whole GPT-5 generation lands inactive — the
   OpenAI LLM provider sends `max_tokens`/temperature on chat.completions,
   which that generation rejects, so activating one would hand operators a
   model that fails on every turn. GPT-4.1 and GPT-4.1 nano are active: the
   existing call path drives them unchanged.
3. Embedding rows are re-expressed per 1M tokens (same price, OpenAI's own
   unit); transcription and tts-1/tts-1-hd prices are filled in.

Existing rows are updated in place, so ids, audit columns and any
operator-set `selling_price` markup survive. A price row is only rewritten
when its unit, unit price or (future-dated) effective_from differs, which
also makes the revision safe to re-run.

Rollback (`alembic downgrade b3e5a7c9d1f3`) reinstates the three blended LLM
rows and the previous per-1K embedding units, then removes every OpenAI
split-component LLM price and the models/prices introduced here. Note that
the delete is by component, so a split price an operator added by hand
outside this revision goes with it; the pre-existing gpt-5-mini `input_tokens`
row is one such row. Costs already recorded on usage_events are snapshots and
are never rewritten either way.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6f8a0b2c4e6"
down_revision: Union[str, None] = "b3e5a7c9d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Official prices ──────────────────────────────────────────────────────
# USD per 1M tokens: (model, input, cached input, output). None = the model
# has no discounted cached-input tier, so no cached row is created.
_LLM_PRICES = [
    ("gpt-5.6-sol", "5.00", "0.50", "30.00"),
    ("gpt-5.6-terra", "2.00", "0.20", "12.00"),
    ("gpt-5.6-luna", "0.20", "0.02", "1.20"),
    ("gpt-5.1", "1.25", "0.125", "10.00"),
    ("gpt-5", "1.25", "0.125", "10.00"),
    ("gpt-5-mini", "0.25", "0.025", "2.00"),
    ("gpt-5-nano", "0.05", "0.005", "0.40"),
    ("gpt-4.1", "2.00", "0.50", "8.00"),
    ("gpt-4.1-mini", "0.40", "0.10", "1.60"),
    ("gpt-4.1-nano", "0.10", "0.025", "0.40"),
    ("gpt-4o", "2.50", "1.25", "10.00"),
    ("gpt-4o-mini", "0.15", "0.075", "0.60"),
]

_EMBEDDING_PRICES = [
    ("text-embedding-3-small", "0.02"),
    ("text-embedding-3-large", "0.13"),
]

# USD per minute of audio.
_STT_PRICES = [
    ("whisper-1", "0.006"),
    ("gpt-transcribe", "0.0045"),
    ("gpt-4o-transcribe", "0.006"),
    ("gpt-4o-mini-transcribe", "0.003"),
]

# USD per 1M characters.
_TTS_PRICES = [
    ("tts-1", "15.00"),
    ("tts-1-hd", "30.00"),
]

# LLM models whose blended per-1K price this revision supersedes.
_BLENDED_LLM_MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"]

_BLENDED_LLM_RESTORE = [
    ("gpt-4o-mini", "0.0006"),
    ("gpt-4o", "0.005"),
    ("gpt-4.1-mini", "0.0007"),
]


# ── Catalog rows the new prices attach to ────────────────────────────────
# Mirrors _OPENAI_LLM_SCHEMA in backend/seeds/provider_catalog_seed.py.
# Inlined on purpose: a migration must keep describing the world as it was
# when it ran, even after the seed constant moves on.
_OPENAI_LLM_SCHEMA = {
    "temperature": {
        "type": "number", "min": 0.0, "max": 2.0, "default": 0.3, "step": 0.05,
        "label": "Temperature", "help": "Response randomness. Keep low for voice bots.",
    },
    "max_tokens": {
        "type": "integer", "min": 16, "max": 4096, "default": 256,
        "label": "Max output tokens",
        "help": "Upper bound per reply. Voice replies should stay short.",
    },
    "timeout_seconds": {
        "type": "number", "min": 5, "max": 120, "default": 30, "step": 1,
        "label": "Timeout (s)", "advanced": True,
        "help": "Per-request timeout before retry/fallback handling.",
    },
    "streaming": {
        "type": "boolean", "default": True, "fixed": True, "label": "Streaming",
        "help": "Token streaming into the sentence buffer. Always on for realtime voice.",
    },
    "max_retries": {
        "type": "integer", "min": 0, "max": 5, "default": 1,
        "label": "Max retries", "advanced": True,
        "help": "Automatic retries on transient failures.",
    },
}

_GPT5_NOTE = (
    "{summary}. Catalogued for pricing but inactive: the OpenAI LLM provider "
    "still sends `max_tokens`/temperature, which the GPT-5 family rejects. "
    "Activate only after the provider is updated."
)

# (capability, code, display_name, description, status, sort_order)
_NEW_MODELS = [
    ("llm", "gpt-4.1", "GPT-4.1",
     "Full GPT-4.1: strongest of the 4.1 family for instruction following and "
     "long context. Higher cost/latency than 4.1 mini — prefer it for complex "
     "reasoning turns rather than every realtime reply.", "active", 3),
    ("llm", "gpt-4.1-nano", "GPT-4.1 nano",
     "Cheapest GPT-4.1 variant and the lowest-latency OpenAI chat model. "
     "Suited to classification, routing and short scripted replies.",
     "active", 4),
    ("llm", "gpt-5.6-sol", "GPT-5.6 Sol", _GPT5_NOTE.format(
        summary="Flagship GPT-5.6 model: highest quality of the generation, "
                "priced accordingly"), "inactive", 5),
    ("llm", "gpt-5.6-terra", "GPT-5.6 Terra", _GPT5_NOTE.format(
        summary="Mid-tier GPT-5.6 model balancing quality and cost"), "inactive", 6),
    ("llm", "gpt-5.6-luna", "GPT-5.6 Luna", _GPT5_NOTE.format(
        summary="Smallest, cheapest GPT-5.6 model for high-volume turns"), "inactive", 7),
    ("llm", "gpt-5.1", "GPT-5.1", _GPT5_NOTE.format(
        summary="GPT-5.1 general-purpose model"), "inactive", 8),
    ("llm", "gpt-5", "GPT-5", _GPT5_NOTE.format(
        summary="GPT-5 general-purpose model"), "inactive", 9),
    ("llm", "gpt-5-mini", "GPT-5 mini", _GPT5_NOTE.format(
        summary="Cost-reduced GPT-5 variant"), "inactive", 10),
    ("llm", "gpt-5-nano", "GPT-5 nano", _GPT5_NOTE.format(
        summary="Smallest and cheapest GPT-5 variant"), "inactive", 11),
    ("stt", "gpt-transcribe", "GPT Transcribe",
     "Current OpenAI batch transcription model and the cheapest of the "
     "family. Inactive under platform governance: STT is Sarvam-only.",
     "inactive", 1),
    ("stt", "gpt-4o-transcribe", "GPT-4o Transcribe",
     "GPT-4o transcription (batch/REST). Inactive under platform governance: "
     "STT is Sarvam-only.", "inactive", 2),
    ("stt", "gpt-4o-mini-transcribe", "GPT-4o mini Transcribe",
     "Cheaper GPT-4o mini transcription (batch/REST). Inactive under platform "
     "governance: STT is Sarvam-only.", "inactive", 3),
]

_STT_AUDIO_DEFAULTS = {
    "languages": [], "codecs": ["linear16"], "sample_rates": [8000, 16000, 24000],
}


def _utc_now() -> datetime:
    """Naive UTC, the convention every datetime column in this schema uses.

    `effective_from` must never be written through the column's server-side
    NOW() default: MySQL evaluates that in the server's local timezone, and
    the costing engine compares it against `datetime.utcnow()`. On a server
    running ahead of UTC the row would be dated into the future and silently
    excluded from pricing.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _upsert_price(bind, capability: str, model: str, component: str,
                  unit: str, price: str) -> None:
    """Set the official price for one component, preserving the existing row.

    Updating in place keeps the row id (usage-event references), the audit
    columns and any operator-set selling price. Only unit and unit_price are
    rewritten, and only when they actually differ.
    """
    existing = bind.execute(
        sa.text(
            "SELECT id, unit, unit_price, effective_from FROM provider_pricing "
            "WHERE provider_code = 'openai' AND capability = :capability "
            "AND model_code = :model AND component = :component "
            "AND is_deleted = 0"
        ),
        {"capability": capability, "model": model, "component": component},
    ).first()

    now = _utc_now()
    if existing is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_pricing (id, provider_code, capability, "
                "model_code, component, unit, unit_price, currency_code, "
                "effective_from, status, sort_order, created_at, updated_at, "
                "is_deleted) VALUES (:id, 'openai', :capability, :model, "
                ":component, :unit, :price, 'USD', :now, 'active', 0, :now, "
                ":now, 0)"
            ),
            {
                "id": f"ppr_{uuid.uuid4().hex[:12]}",
                "capability": capability, "model": model,
                "component": component, "unit": unit, "price": price,
                "now": now,
            },
        )
        return

    row_id, current_unit, current_price, effective_from = existing
    # Compare numerically — Decimal("0.1500000000") != the string "0.15".
    unchanged = current_unit == unit and float(current_price) == float(price)
    # A row dated in the future never prices anything (see _utc_now); repair
    # it even when the price itself already matches.
    if unchanged and effective_from is not None and effective_from <= now:
        return
    bind.execute(
        sa.text(
            "UPDATE provider_pricing SET unit = :unit, unit_price = :price, "
            "status = 'active', effective_from = :now, updated_at = :now "
            "WHERE id = :id"
        ),
        {"unit": unit, "price": price, "id": row_id, "now": now},
    )


def _usd_exists(bind) -> bool:
    """Whether the USD currency row the price rows point at is present yet.

    On a fresh database `alembic upgrade head` runs before the bootstrap
    seed, so `currencies` is still empty and provider_pricing's currency
    foreign key would reject every insert. Nothing needs correcting there
    either — the seed writes these same official prices itself.
    """
    return bind.execute(
        sa.text("SELECT code FROM currencies WHERE code = 'USD'")
    ).first() is not None


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. Catalog rows for models that are about to be priced ───────────
    # Pricing rows are validated against the provider-model catalog, so a
    # price for a model that isn't catalogued would be uneditable in the UI.
    for capability, code, display_name, description, status, sort_order in _NEW_MODELS:
        exists = bind.execute(
            sa.text(
                "SELECT id FROM provider_models WHERE provider_code = 'openai' "
                "AND capability = :capability AND code = :code"
            ),
            {"capability": capability, "code": code},
        ).first()
        if exists is not None:
            # Operator-managed row (e.g. a hand-added gpt-5-mini): fill an
            # empty description, never touch status or any other field.
            bind.execute(
                sa.text(
                    "UPDATE provider_models SET description = :description "
                    "WHERE provider_code = 'openai' AND capability = :capability "
                    "AND code = :code AND (description IS NULL OR description = '')"
                ),
                {"description": description, "capability": capability, "code": code},
            )
            continue
        audio = _STT_AUDIO_DEFAULTS if capability == "stt" else {
            "languages": None, "codecs": None, "sample_rates": None,
        }
        bind.execute(
            sa.text(
                "INSERT INTO provider_models (id, provider_code, capability, "
                "code, display_name, description, languages, codecs, "
                "sample_rates, streaming, params_schema, is_default, status, "
                "sort_order, created_at, updated_at, is_deleted) VALUES "
                "(:id, 'openai', :capability, :code, :display_name, "
                ":description, :languages, :codecs, :sample_rates, :streaming, "
                ":params_schema, 0, :status, :sort_order, :now, :now, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "now": _utc_now(),
                "capability": capability, "code": code,
                "display_name": display_name, "description": description,
                "languages": json.dumps(audio["languages"]) if audio["languages"] is not None else None,
                "codecs": json.dumps(audio["codecs"]) if audio["codecs"] is not None else None,
                "sample_rates": json.dumps(audio["sample_rates"]) if audio["sample_rates"] is not None else None,
                # STT here is batch/REST only; LLM chat streaming is supported.
                "streaming": capability == "llm",
                "params_schema": json.dumps(_OPENAI_LLM_SCHEMA if capability == "llm" else {}),
                "status": status, "sort_order": sort_order,
            },
        )

    # ── 2. Official prices ───────────────────────────────────────────────
    if not _usd_exists(bind):
        return  # fresh database — the seed writes these prices (see _usd_exists)

    for model, price_in, price_cached, price_out in _LLM_PRICES:
        for component, price in (
            ("input_tokens", price_in),
            ("cached_input_tokens", price_cached),
            ("output_tokens", price_out),
        ):
            if price is None:
                continue
            _upsert_price(bind, "llm", model, component, "per_1m_tokens", price)

    for model, price in _EMBEDDING_PRICES:
        _upsert_price(bind, "embedding", model, "tokens", "per_1m_tokens", price)

    for model, price in _STT_PRICES:
        _upsert_price(bind, "stt", model, "audio_seconds", "per_minute", price)

    for model, price in _TTS_PRICES:
        _upsert_price(bind, "tts", model, "characters", "per_1m_characters", price)

    # ── 3. Retire the superseded blended LLM rates ───────────────────────
    # Deactivated rather than deleted: the split rows above now cost these
    # models, and the blended row stays readable next to the usage events
    # that were costed with it.
    for model in _BLENDED_LLM_MODELS:
        bind.execute(
            sa.text(
                "UPDATE provider_pricing SET status = 'inactive' "
                "WHERE provider_code = 'openai' AND capability = 'llm' "
                "AND model_code = :model AND component = 'tokens' "
                "AND is_deleted = 0"
            ),
            {"model": model},
        )


def downgrade() -> None:
    bind = op.get_bind()

    # Reactivate (or recreate) the blended per-1K LLM rates.
    for model, price in _BLENDED_LLM_RESTORE:
        updated = bind.execute(
            sa.text(
                "UPDATE provider_pricing SET status = 'active', "
                "unit = 'per_1k_tokens', unit_price = :price "
                "WHERE provider_code = 'openai' AND capability = 'llm' "
                "AND model_code = :model AND component = 'tokens' "
                "AND is_deleted = 0"
            ),
            {"model": model, "price": price},
        ).rowcount
        if not updated:
            bind.execute(
                sa.text(
                    "INSERT INTO provider_pricing (id, provider_code, "
                    "capability, model_code, component, unit, unit_price, "
                    "currency_code, effective_from, status, sort_order, "
                    "created_at, updated_at, is_deleted) VALUES (:id, "
                    "'openai', 'llm', :model, 'tokens', 'per_1k_tokens', "
                    ":price, 'USD', :now, 'active', 0, :now, :now, 0)"
                ),
                {
                    "id": f"ppr_{uuid.uuid4().hex[:12]}", "model": model,
                    "price": price, "now": _utc_now(),
                },
            )

    # Drop the split LLM rows this revision introduced.
    bind.execute(
        sa.text(
            "DELETE FROM provider_pricing WHERE provider_code = 'openai' "
            "AND capability = 'llm' AND component IN "
            "('input_tokens', 'cached_input_tokens', 'output_tokens')"
        )
    )

    # Embeddings return to their previous per-1K expression (same price).
    for model, per_million in _EMBEDDING_PRICES:
        bind.execute(
            sa.text(
                "UPDATE provider_pricing SET unit = 'per_1k_tokens', "
                "unit_price = :price WHERE provider_code = 'openai' "
                "AND capability = 'embedding' AND model_code = :model "
                "AND component = 'tokens' AND is_deleted = 0"
            ),
            {"model": model, "price": str(float(per_million) / 1000)},
        )

    # Prices for models that only existed as of this revision.
    bind.execute(
        sa.text(
            "DELETE FROM provider_pricing WHERE provider_code = 'openai' "
            "AND ((capability = 'stt' AND model_code IN "
            "('gpt-transcribe', 'gpt-4o-transcribe', 'gpt-4o-mini-transcribe')) "
            "OR (capability = 'tts' AND model_code IN ('tts-1', 'tts-1-hd')))"
        )
    )

    for capability, code, *_ in _NEW_MODELS:
        if code == "gpt-5-mini":
            # May pre-date this revision as an operator-created row; the
            # upgrade never inserted it, so the downgrade must not remove it.
            continue
        bind.execute(
            sa.text(
                "DELETE FROM provider_models WHERE provider_code = 'openai' "
                "AND capability = :capability AND code = :code"
            ),
            {"capability": capability, "code": code},
        )
