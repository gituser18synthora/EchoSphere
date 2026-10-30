"""Deepgram TTS (Aura / Aura-2): provider activation, models, voices, pricing.

Adds Deepgram as the platform's third active TTS vendor:

- activates (or creates) the ``deepgram`` TTS provider row pointing at the
  SAME credential the Deepgram STT row already uses (``env:DEEPGRAM_API_KEY``)
  — the platform never holds a second Deepgram key;
- catalogues ``aura-2`` (default) and ``aura`` with the synthesis parameter
  schema (speed / region / mip_opt_out);
- seeds 11 Aura-2 and 6 Aura v1 voices, scoped to the platform's English
  locales only;
- writes the official pay-as-you-go prices (deepgram.com/pricing, verified
  2026-09-21): Aura-2 $0.030 per 1k characters, Aura v1 $0.0150 per 1k;
- records Deepgram against ``en-IN`` in ``supported_languages.provider_support``
  for TTS.

LANGUAGE SCOPE — deliberate and verified against
developers.deepgram.com/docs/tts-models on 2026-09-21: Deepgram text-to-speech
speaks English, Spanish, German, Dutch, French, Italian and Japanese. It has
NO Hindi, Tamil, Telugu, Malayalam, Marathi, Gujarati, Punjabi or Urdu voice.
The India regional endpoint (api.in.deepgram.com) is a data-residency host
running the same models and adds no languages, so nothing here claims Indic
support. ``en-IN`` is mapped only because Aura genuinely speaks English —
with American/British/Australian/Irish/Filipino accents, never Indian.

Rollback removes the models, voices and prices it introduced and deactivates
the provider row; operator-managed rows that predate this migration are left
untouched.

Revision ID: c7e9a1b3d5f7
Revises: a1b2c3d4e5f6
Create Date: 2026-09-21
"""

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "c7e9a1b3d5f7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None

# Schema constants are inlined on purpose: a migration must keep describing
# the world as it was when it ran, even after the seed constant moves on.
_TTS_SCHEMA = {
    "speed": {
        "type": "number", "min": 0.7, "max": 1.5, "default": 1.0, "step": 0.05,
        "label": "Speed", "help": "Playback speed multiplier.",
    },
    "region": {
        "type": "enum", "values": ["default", "global", "in", "eu", "au"],
        "default": "default", "label": "Region", "advanced": True,
        "help": "Deepgram endpoint to synthesize on: 'in' keeps inference and "
                "storage inside India (api.in.deepgram.com), 'default' follows "
                "the platform's DEEPGRAM_REGION setting. This is a data-"
                "residency choice and does not change which languages the "
                "voices speak.",
    },
    "mip_opt_out": {
        "type": "boolean", "default": False,
        "label": "Opt out of model improvement", "advanced": True,
        "help": "Ask Deepgram not to retain this audio for model improvement.",
    },
}

_SAMPLE_RATES = [8000, 16000, 24000, 32000, 48000]
_CODECS = ["linear16", "mulaw", "alaw"]

# (code, display, languages, is_default, sort, description, price_per_1k_chars)
_TTS_MODELS = (
    (
        "aura-2", "Aura-2 (streaming)",
        ["en", "es", "de", "nl", "fr", "it", "ja"], True, 0,
        "Deepgram Aura-2 low-latency neural voices over /v1/speak (REST and "
        "realtime WebSocket). Speaks English, Spanish, German, Dutch, French, "
        "Italian and Japanese only — NO Indian language. Its English voices "
        "carry American, British, Australian, Irish and Filipino accents; "
        "there is no Indian-English voice.",
        "0.030",
    ),
    (
        "aura", "Aura v1 (English, legacy)", ["en"], False, 1,
        "Deepgram Aura (v1) voices — English only, cheaper than Aura-2 and "
        "superseded by it. Same /v1/speak endpoint and streaming support.",
        "0.0150",
    ),
)

_VOICE_LOCALES = ["en-IN", "en-US", "en-GB"]
_SAMPLE_TEXT = "Hello! I can help you book your next appointment in just a minute."

# (id, name, gender, wire voice id, accent, model family, sort)
_VOICES = (
    ("vp-dg-thalia", "Thalia", "female", "aura-2-thalia-en", "American", "aura-2", 0),
    ("vp-dg-andromeda", "Andromeda", "female", "aura-2-andromeda-en", "American", "aura-2", 1),
    ("vp-dg-asteria", "Asteria", "female", "aura-2-asteria-en", "American", "aura-2", 2),
    ("vp-dg-luna", "Luna", "female", "aura-2-luna-en", "American", "aura-2", 3),
    ("vp-dg-pandora", "Pandora", "female", "aura-2-pandora-en", "British", "aura-2", 4),
    ("vp-dg-theia", "Theia", "female", "aura-2-theia-en", "Australian", "aura-2", 5),
    ("vp-dg-apollo", "Apollo", "male", "aura-2-apollo-en", "American", "aura-2", 6),
    ("vp-dg-arcas", "Arcas", "male", "aura-2-arcas-en", "American", "aura-2", 7),
    ("vp-dg-zeus", "Zeus", "male", "aura-2-zeus-en", "American", "aura-2", 8),
    ("vp-dg-draco", "Draco", "male", "aura-2-draco-en", "British", "aura-2", 9),
    ("vp-dg-hyperion", "Hyperion", "male", "aura-2-hyperion-en", "Australian", "aura-2", 10),
    ("vp-dg-v1-asteria", "Asteria (v1)", "female", "aura-asteria-en", "American", "aura", 11),
    ("vp-dg-v1-luna", "Luna (v1)", "female", "aura-luna-en", "American", "aura", 12),
    ("vp-dg-v1-stella", "Stella (v1)", "female", "aura-stella-en", "American", "aura", 13),
    ("vp-dg-v1-orion", "Orion (v1)", "male", "aura-orion-en", "American", "aura", 14),
    ("vp-dg-v1-arcas", "Arcas (v1)", "male", "aura-arcas-en", "American", "aura", 15),
    ("vp-dg-v1-angus", "Angus (v1)", "male", "aura-angus-en", "Irish", "aura", 16),
)

_PROVIDER_DESCRIPTION = (
    "Low-latency Aura / Aura-2 voices (English, Spanish, German, Dutch, "
    "French, Italian, Japanese — no Indian language)."
)


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
    """Official per-1k-character price, preserving any existing row's identity."""
    existing = bind.execute(
        sa.text(
            "SELECT id, unit, unit_price, effective_from FROM provider_pricing "
            "WHERE provider_code = 'deepgram' AND capability = 'tts' "
            "AND model_code = :model AND component = 'characters' "
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
                "is_deleted) VALUES (:id, 'deepgram', 'tts', :model, "
                "'characters', 'per_1k_characters', :price, 'USD', :now, "
                "'active', 0, :now, :now, 0)"
            ),
            {"id": f"ppr_{uuid.uuid4().hex[:12]}", "model": model,
             "price": price, "now": now},
        )
        return
    row_id, current_unit, current_price, effective_from = existing
    unchanged = (
        current_unit == "per_1k_characters" and float(current_price) == float(price)
    )
    # Drivers differ on what a DATETIME comes back as (MySQL: datetime;
    # SQLite: str), and an un-comparable value must not crash the migration —
    # it just means "re-stamp the row", which is harmless.
    already_effective = isinstance(effective_from, datetime) and effective_from <= now
    if unchanged and already_effective:
        return
    bind.execute(
        sa.text(
            "UPDATE provider_pricing SET unit = 'per_1k_characters', "
            "unit_price = :price, status = 'active', effective_from = :now, "
            "updated_at = :now WHERE id = :id"
        ),
        {"price": price, "id": row_id, "now": now},
    )


def upgrade() -> None:
    bind = op.get_bind()
    now = _utc_now()

    # ── 1. Governance: the Deepgram TTS provider row ──────────────────────
    # It shares the STT row's credential reference: one Deepgram account,
    # one key, two capabilities.
    existing_provider = bind.execute(sa.text(
        "SELECT id FROM provider_defs WHERE kind = 'tts' AND code = 'deepgram' "
        "AND is_deleted = 0"
    )).first()
    if existing_provider is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_defs (id, kind, code, name, description, "
                "requires_api_key, secret_ref, status, sort_order, created_at, "
                "updated_at, is_deleted) VALUES (:id, 'tts', 'deepgram', "
                "'Deepgram', :description, 1, 'env:DEEPGRAM_API_KEY', 'active', "
                ":sort, :now, :now, 0)"
            ),
            {"id": f"pv_{uuid.uuid4().hex[:12]}", "description": _PROVIDER_DESCRIPTION,
             "sort": 3, "now": now},
        )
    else:
        bind.execute(sa.text(
            "UPDATE provider_defs SET status = 'active', updated_at = :now "
            "WHERE kind = 'tts' AND code = 'deepgram' AND is_deleted = 0 "
            "AND status != 'active'"
        ), {"now": now})
        # Only fill a missing credential reference; never repoint one an
        # operator has set to their own secret store.
        bind.execute(sa.text(
            "UPDATE provider_defs SET secret_ref = 'env:DEEPGRAM_API_KEY', "
            "updated_at = :now WHERE kind = 'tts' AND code = 'deepgram' "
            "AND is_deleted = 0 AND (secret_ref IS NULL OR secret_ref = '')"
        ), {"now": now})

    # ── 2. Model catalog rows ─────────────────────────────────────────────
    for code, display, languages, is_default, sort, description, _ in _TTS_MODELS:
        exists = bind.execute(
            sa.text(
                "SELECT id FROM provider_models WHERE provider_code = 'deepgram' "
                "AND capability = 'tts' AND code = :code"
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
                "(:id, 'deepgram', 'tts', :code, :display_name, :description, "
                ":languages, :codecs, :sample_rates, 1, :params_schema, "
                ":is_default, 'active', :sort_order, :now, :now, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "code": code, "display_name": display, "description": description,
                "languages": json.dumps(languages),
                "codecs": json.dumps(_CODECS),
                "sample_rates": json.dumps(_SAMPLE_RATES),
                "params_schema": json.dumps(_TTS_SCHEMA),
                "is_default": 1 if is_default else 0,
                "sort_order": sort, "now": now,
            },
        )

    # ── 3. Voices ─────────────────────────────────────────────────────────
    for vid, name, gender, wire_id, accent, family, sort in _VOICES:
        exists = bind.execute(
            sa.text("SELECT id FROM voice_profiles WHERE id = :id"), {"id": vid}
        ).first()
        if exists is not None:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO voice_profiles (id, source, name, gender, "
                "languages, accent, styles, latency_ms, premium, sample_text, "
                "provider, provider_voice_id, speaking_rate, pitch, "
                "model_codes, provider_settings, is_default, status, "
                "sort_order, created_at, updated_at, is_deleted) VALUES "
                "(:id, 'platform', :name, :gender, :languages, :accent, "
                ":styles, 150, 0, :sample_text, 'deepgram', :wire_id, 1.0, "
                "1.0, :model_codes, :provider_settings, :is_default, 'active', "
                ":sort, :now, :now, 0)"
            ),
            {
                "id": vid, "name": name, "gender": gender,
                "languages": json.dumps(_VOICE_LOCALES),
                "accent": accent, "styles": json.dumps(["Natural"]),
                "sample_text": _SAMPLE_TEXT, "wire_id": wire_id,
                "model_codes": json.dumps([family]),
                "provider_settings": json.dumps({}),
                "is_default": 1 if vid == "vp-dg-thalia" else 0,
                "sort": sort, "now": now,
            },
        )

    # ── 4. Language provider_support (en-IN only — see module docstring) ──
    row = bind.execute(sa.text(
        "SELECT provider_support FROM supported_languages WHERE code = 'en-IN'"
    )).first()
    if row is not None:
        support = row[0]
        if isinstance(support, str):
            try:
                support = json.loads(support)
            except (TypeError, ValueError):
                support = None
        if isinstance(support, dict):
            tts = list(support.get("tts") or [])
            if "deepgram" not in tts:
                support["tts"] = [*tts, "deepgram"]
                bind.execute(
                    sa.text(
                        "UPDATE supported_languages SET provider_support = "
                        ":support, updated_at = :now WHERE code = 'en-IN'"
                    ),
                    {"support": json.dumps(support), "now": now},
                )

    # ── 5. Official prices ────────────────────────────────────────────────
    if not _usd_exists(bind):
        return  # fresh database — the seed writes these prices
    for code, _, _, _, _, _, price in _TTS_MODELS:
        _upsert_price(bind, code, price)


def downgrade() -> None:
    bind = op.get_bind()
    now = _utc_now()
    for vid, *_ in _VOICES:
        bind.execute(
            sa.text("DELETE FROM voice_profiles WHERE id = :id AND provider = 'deepgram'"),
            {"id": vid},
        )
    for code, *_ in _TTS_MODELS:
        bind.execute(sa.text(
            "DELETE FROM provider_pricing WHERE provider_code = 'deepgram' "
            "AND capability = 'tts' AND model_code = :code"
        ), {"code": code})
        bind.execute(sa.text(
            "DELETE FROM provider_models WHERE provider_code = 'deepgram' "
            "AND capability = 'tts' AND code = :code"
        ), {"code": code})
    bind.execute(sa.text(
        "UPDATE provider_defs SET status = 'inactive', updated_at = :now "
        "WHERE kind = 'tts' AND code = 'deepgram'"
    ), {"now": now})
