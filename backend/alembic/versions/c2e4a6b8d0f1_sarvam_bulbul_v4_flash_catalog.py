"""Sarvam bulbul:v4-flash in the TTS catalog.

Revision ID: c2e4a6b8d0f1
Revises: b2d4f6a8c0e2
Create Date: 2026-10-23

1. Inserts the ``bulbul:v4-flash`` Sarvam TTS model when missing (active,
   NOT default — making it the default for new bots is an operator decision,
   see the release notes). Same streaming contract and parameter schema as
   bulbul:v3; nine languages (ml-IN/od-IN have no v4 personas and stay on v3).
2. Inserts its price row when missing: Sarvam's public list price, ₹2.00 per
   1K characters (sarvam.ai/api-pricing, 2026-10-23).
3. Additive voice backfill: appends ``bulbul:v4-flash`` to model_codes of
   Sarvam voice profiles (platform voices and tenant rows alike) whose wire
   speaker has a reviewed Hindi or English persona
   (shared.providers.tts.sarvam_voices, snapshot below). Rows with empty
   model_codes already mean "any model" and are left untouched; the model
   did not exist before, so no operator can have excluded it deliberately.

Rollback removes the inserted rows and the appended model codes. Bots moved
to bulbul:v4-flash fail validation afterwards — downgrade only together with
moving them back to bulbul:v3.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: Union[str, None] = "b2d4f6a8c0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL_CODE = "bulbul:v4-flash"
_LANGS = ["hi-IN", "en-IN", "bn-IN", "mr-IN", "gu-IN", "ta-IN", "te-IN", "kn-IN", "pa-IN"]
_DESCRIPTION = (
    "Sarvam's low-latency Bulbul generation: same streaming contract as "
    "Bulbul v3 with persona voices (a v3 voice maps to its own persona). "
    "Nine languages — Malayalam and Odia stay on Bulbul v3."
)
_SCHEMA = {
    "pace": {
        "type": "number", "min": 0.5, "max": 2.0, "default": 1.0, "step": 0.05,
        "label": "Pace", "help": "Speech speed multiplier (range 0.5–2.0).",
    },
    "temperature": {
        "type": "number", "min": 0.01, "max": 1.0, "default": 0.6, "step": 0.01,
        "label": "Temperature",
        "help": "Synthesis randomness. Sarvam documents bulbul:v4-flash as "
                "ignoring it; kept so bulbul:v3 settings carry over.",
    },
    "min_buffer_size": {
        "type": "integer", "min": 30, "max": 200, "default": 50,
        "label": "Min buffer size",
        "help": "Minimum characters buffered before Sarvam starts synthesis. "
                "Lower values reduce initial buffering but may reduce sentence "
                "context. Sarvam accepts 30–200 (WebSocket streaming only).",
    },
    "max_chunk_length": {
        "type": "integer", "min": 50, "max": 500, "default": 150,
        "label": "Max chunk length",
        "help": "Maximum characters per synthesis chunk. 120–150 recommended for realtime.",
    },
    "dict_id": {
        "type": "string", "default": None, "optional": True, "max_length": 100,
        "label": "Pronunciation dictionary",
        "widget": "dictionary", "section": "pronunciation",
        "help": "Fixes how specific words (brands, acronyms, names) are spoken.",
    },
    "send_completion_event": {
        "type": "boolean", "default": True, "label": "Completion event", "advanced": True,
        "help": "Ask Sarvam to signal when synthesis of flushed text has finished.",
    },
    "enable_preprocessing": {
        "type": "boolean", "default": True, "fixed": True, "label": "Preprocessing",
        "help": "Text normalization (numbers, dates, currencies, mixed-language "
                "text) before synthesis. Always enabled.",
    },
}
# Speakers with a reviewed hi/en persona (snapshot of
# shared.providers.tts.sarvam_voices.V3_TO_V4_PERSONA, 2026-10-23).
_V4_SPEAKERS = (
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "rohan", "simran",
    "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun", "manan",
    "sumit", "roopa", "kabir", "aayan", "ashutosh", "advait", "anand", "tanya",
    "tarun", "sunny", "mani", "shruti", "suhani", "mohit", "rehan",
)


def _parse(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _inr_exists(bind) -> bool:
    return bind.execute(
        sa.text("SELECT code FROM currencies WHERE code = 'INR'")
    ).first() is not None


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    exists = bind.execute(
        sa.text(
            "SELECT id FROM provider_models WHERE provider_code = 'sarvam' "
            "AND capability = 'tts' AND code = :code"
        ),
        {"code": _MODEL_CODE},
    ).first()
    if exists is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_models (id, provider_code, capability, "
                "code, display_name, description, languages, codecs, "
                "sample_rates, streaming, params_schema, is_default, status, "
                "sort_order, is_deleted) VALUES (:id, 'sarvam', 'tts', :code, "
                ":display_name, :description, :languages, :codecs, "
                ":sample_rates, :streaming, :params_schema, :is_default, "
                "'active', :sort_order, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "code": _MODEL_CODE,
                "display_name": "Bulbul v4 Flash (streaming)",
                "description": _DESCRIPTION,
                "languages": json.dumps(_LANGS),
                "codecs": json.dumps(["linear16"]),
                "sample_rates": json.dumps([8000, 16000, 22050, 24000]),
                "streaming": True,
                "params_schema": json.dumps(_SCHEMA),
                "is_default": False,
                "sort_order": 0,
            },
        )

    # Fresh database: currencies are not seeded yet, and the bootstrap seed
    # writes this price after them (provider_pricing references currencies).
    price = None if not _inr_exists(bind) else bind.execute(
        sa.text(
            "SELECT id FROM provider_pricing WHERE provider_code = 'sarvam' "
            "AND capability = 'tts' AND model_code = :code "
            "AND component = 'characters' AND is_deleted = 0"
        ),
        {"code": _MODEL_CODE},
    ).first()
    if price is None and _inr_exists(bind):
        bind.execute(
            sa.text(
                "INSERT INTO provider_pricing (id, provider_code, capability, "
                "model_code, component, unit, unit_price, currency_code, "
                "effective_from, status, sort_order, created_at, updated_at, "
                "is_deleted) VALUES (:id, 'sarvam', 'tts', :code, 'characters', "
                "'per_1k_characters', :price, 'INR', :now, 'active', 0, :now, :now, 0)"
            ),
            {"id": f"ppr_{uuid.uuid4().hex[:12]}", "code": _MODEL_CODE,
             "price": "2.0", "now": now},
        )

    rows = bind.execute(
        sa.text(
            "SELECT id, provider_voice_id, model_codes FROM voice_profiles "
            "WHERE provider = 'sarvam' AND model_codes IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        codes = _parse(row.model_codes)
        speaker = str(row.provider_voice_id or "").strip().lower()
        if not codes or _MODEL_CODE in codes or speaker not in _V4_SPEAKERS:
            continue
        if "bulbul:v3" not in codes:
            continue
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes = :codes WHERE id = :id"),
            {"codes": json.dumps([*codes, _MODEL_CODE]), "id": row.id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, model_codes FROM voice_profiles "
            "WHERE provider = 'sarvam' AND model_codes IS NOT NULL"
        )
    ).fetchall()
    for row in rows:
        codes = _parse(row.model_codes)
        if _MODEL_CODE in codes:
            bind.execute(
                sa.text("UPDATE voice_profiles SET model_codes = :codes WHERE id = :id"),
                {"codes": json.dumps([c for c in codes if c != _MODEL_CODE]), "id": row.id},
            )
    bind.execute(
        sa.text(
            "DELETE FROM provider_pricing WHERE provider_code = 'sarvam' "
            "AND capability = 'tts' AND model_code = :code"
        ),
        {"code": _MODEL_CODE},
    )
    bind.execute(
        sa.text(
            "DELETE FROM provider_models WHERE provider_code = 'sarvam' "
            "AND capability = 'tts' AND code = :code"
        ),
        {"code": _MODEL_CODE},
    )
