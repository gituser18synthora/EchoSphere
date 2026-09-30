"""Sarvam saaras:v4 STT: catalog model (with key-term biasing) + pricing.

Revision ID: a9c1e3b5d7f9
Revises: f5b7d9c1e3a7
Create Date: 2026-10-01

Adds Sarvam's ``saaras:v4`` realtime STT model as a SELECTABLE option:

1. ``provider_models`` row ``sarvam/stt/saaras:v4`` (insert-if-missing): the
   saaras:v3 schema plus ``keyterms`` — a ``string_list`` of up to 50 terms
   × 64 characters (Sarvam's documented contract, docs.sarvam.ai +
   sarvamai 0.1.35, verified 2026-10-01). ``is_default`` stays 0: saaras:v3
   remains the provider and platform default; nothing existing changes.
2. A row that was PRE-STAGED inactive by ``backend/scripts/
   stage_saaras_v4_catalog.py`` (the recipe for a database whose code cannot
   run v4 yet) is activated — only when it still has no operator edit
   (``updated_by IS NULL``). An operator-managed row keeps its status.
3. ``provider_pricing`` ``sarvam/stt/saaras:v4`` ₹30/hour (insert-if-missing;
   skipped on a fresh database where the seed writes it after currencies).
4. Description backfill for the saaras:v3 row when empty.

Rollback removes the saaras:v4 price and model rows this migration owns
(bots configured with saaras:v4 fail validation afterwards — same policy as
the Flux/Eleven v3 catalog migrations).
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3b5d7f9"
down_revision: Union[str, None] = "f5b7d9c1e3a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL = "saaras:v4"
_DISPLAY = "Saaras v4 (streaming)"
_DESCRIPTION = (
    "Sarvam Saaras v4 realtime STT: v3 coverage plus global English, better "
    "noise/accent robustness, and key-term biasing (up to 50 "
    "names/brands/terms). Not the default — select it per bot."
)
_V3_DESCRIPTION = (
    "Sarvam Saaras v3 realtime STT: 22 Indic languages + Indian English, "
    "auto-detect, output modes (transcribe/verbatim/translit/codemix/"
    "translate). Platform default."
)
_PRICE_INR_PER_HOUR = "30"

# Inlined on purpose: a migration keeps describing the world as it was when
# it ran, even after the seed constants move on.
_LANGS = [
    "unknown", "hi-IN", "en-IN", "bn-IN", "mr-IN", "gu-IN", "ta-IN", "te-IN",
    "kn-IN", "ml-IN", "pa-IN", "od-IN", "as-IN", "ur-IN", "ne-IN", "kok-IN",
    "ks-IN", "sd-IN", "sa-IN", "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN",
]

_SCHEMA = {
    "mode": {
        "type": "enum", "values": ["transcribe", "verbatim", "translit", "codemix", "translate"],
        "default": "transcribe", "label": "Mode",
        "help": "Output mode. 'transcribe' is standard; 'translate' returns English.",
    },
    "vad_signals": {
        "type": "boolean", "default": True, "label": "VAD signals",
        "help": "Emit start/end-of-speech events from Sarvam's server-side VAD.",
    },
    "high_vad_sensitivity": {
        "type": "boolean", "default": False, "label": "High VAD sensitivity",
        "help": "Finalize segments after ~0.5s of silence instead of ~1s (faster endpointing).",
    },
    "input_encoding": {
        "type": "enum", "values": ["pcm_s16le"], "default": "pcm_s16le",
        "label": "Input encoding", "advanced": True,
        "help": "Wire encoding for microphone/telephony audio sent to Sarvam.",
    },
    "auto_detect_language": {
        "type": "boolean", "widget": "auto_detect_language",
        "label": "Auto-detect language",
        "help": "Let the recognizer detect the spoken language on every utterance "
                "so the bot can follow the caller between its configured languages. "
                "When off, recognition is pinned to the bot's default language "
                "(more reliable for short phone replies).",
    },
    "timeout_seconds": {
        "type": "number", "min": 5, "max": 120, "default": 30, "step": 1,
        "label": "Timeout (s)", "advanced": True,
        "help": "Connection/response timeout before the turn is failed.",
    },
    "positive_speech_threshold": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.7, "step": 0.05,
        "label": "Speech threshold", "advanced": True,
        "help": "VAD probability above which a frame counts as speech.",
    },
    "negative_speech_threshold": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.45, "step": 0.05,
        "label": "Silence threshold", "advanced": True,
        "help": "VAD probability below which a frame counts as silence.",
    },
    "min_speech_frames": {
        "type": "integer", "min": 1, "max": 50, "default": 2,
        "label": "Min speech frames", "advanced": True,
        "help": "Consecutive speech frames required to open a segment.",
    },
    "interrupt_min_speech_frames": {
        "type": "integer", "min": 1, "max": 50, "default": 2,
        "label": "Barge-in min frames", "advanced": True,
        "help": "Speech frames required to register a barge-in.",
    },
    "keyterms": {
        "type": "string_list", "max_items": 50, "max_length": 64,
        "optional": True, "multiline": True,
        "label": "Key terms (saaras:v4)",
        "help": "Up to 50 names, places, brands or technical terms (64 characters "
                "each) that recognition is biased toward — one term or phrase per "
                "line, e.g. Zepto / New Delhi. Biasing only: a term is not "
                "guaranteed to appear in the transcript.",
    },
}


def _utc_now() -> datetime:
    """Naive UTC — `effective_from` must never use the server-local NOW()
    default, or a server running ahead of UTC dates the row into the future
    and the costing engine silently excludes it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _inr_exists(bind) -> bool:
    return bind.execute(
        sa.text("SELECT code FROM currencies WHERE code = 'INR'")
    ).first() is not None


def upgrade() -> None:
    bind = op.get_bind()
    now = _utc_now()

    # ── 1./2. Model row ───────────────────────────────────────────────────
    existing = bind.execute(
        sa.text(
            "SELECT id, status, updated_by FROM provider_models "
            "WHERE provider_code = 'sarvam' AND capability = 'stt' AND code = :code"
        ),
        {"code": _MODEL},
    ).first()
    if existing is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_models (id, provider_code, capability, "
                "code, display_name, description, languages, codecs, "
                "sample_rates, streaming, params_schema, is_default, status, "
                "sort_order, created_at, updated_at, is_deleted) VALUES "
                "(:id, 'sarvam', 'stt', :code, :display_name, :description, "
                ":languages, :codecs, :sample_rates, 1, :params_schema, "
                "0, 'active', 1, :now, :now, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "code": _MODEL, "display_name": _DISPLAY,
                "description": _DESCRIPTION,
                "languages": json.dumps(_LANGS),
                "codecs": json.dumps(["linear16"]),
                "sample_rates": json.dumps([8000, 16000]),
                "params_schema": json.dumps(_SCHEMA),
                "now": now,
            },
        )
    else:
        row_id, status, updated_by = existing
        bind.execute(
            sa.text(
                "UPDATE provider_models SET description = :description "
                "WHERE id = :id AND (description IS NULL OR description = '')"
            ),
            {"description": _DESCRIPTION, "id": row_id},
        )
        if status != "active" and updated_by is None:
            # Pre-staged inactive row (see module docstring): the code that
            # can run saaras:v4 is now deployed, so expose it.
            bind.execute(
                sa.text(
                    "UPDATE provider_models SET status = 'active', updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": row_id, "now": now},
            )

    # ── 4. v3 description backfill (empty only) ───────────────────────────
    bind.execute(
        sa.text(
            "UPDATE provider_models SET description = :description "
            "WHERE provider_code = 'sarvam' AND capability = 'stt' "
            "AND code = 'saaras:v3' AND (description IS NULL OR description = '')"
        ),
        {"description": _V3_DESCRIPTION},
    )

    # ── 3. Price ──────────────────────────────────────────────────────────
    if not _inr_exists(bind):
        return  # fresh database — the seed writes the price after currencies
    price_exists = bind.execute(
        sa.text(
            "SELECT id FROM provider_pricing WHERE provider_code = 'sarvam' "
            "AND capability = 'stt' AND model_code = :model "
            "AND component = 'audio_seconds' AND is_deleted = 0"
        ),
        {"model": _MODEL},
    ).first()
    if price_exists is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_pricing (id, provider_code, capability, "
                "model_code, component, unit, unit_price, currency_code, "
                "effective_from, status, sort_order, created_at, updated_at, "
                "is_deleted) VALUES (:id, 'sarvam', 'stt', :model, "
                "'audio_seconds', 'per_hour', :price, 'INR', :now, 'active', "
                "0, :now, :now, 0)"
            ),
            {"id": f"ppr_{uuid.uuid4().hex[:12]}", "model": _MODEL,
             "price": _PRICE_INR_PER_HOUR, "now": now},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM provider_pricing WHERE provider_code = 'sarvam' "
        "AND capability = 'stt' AND model_code = :code"
    ), {"code": _MODEL})
    bind.execute(sa.text(
        "DELETE FROM provider_models WHERE provider_code = 'sarvam' "
        "AND capability = 'stt' AND code = :code"
    ), {"code": _MODEL})
