"""ElevenLabs Eleven v3 catalog support.

Revision ID: a7c9e1b3d5f7
Revises: f4b6d8e0a2c4
Create Date: 2026-07-30

1. provider_models.description — concise operator-facing model summary shown
   next to model dropdowns (nullable; backfilled for ElevenLabs TTS rows).
2. Inserts the ``eleven_v3`` ElevenLabs TTS model when missing, so databases
   that run migrations without the bootstrap seed still get the row. Verified
   against the official ElevenLabs docs (2026-07): 70+ languages, 5k chars per
   request, expressive alpha model, NOT supported on the realtime WebSocket
   endpoint (streaming=0) and no language_code enforcement; discrete stability
   0.0/0.5/1.0 (Creative/Natural/Robust).
3. One-time additive backfill: appends ``eleven_v3`` to model_codes of every
   ElevenLabs voice profile (platform voices and tenant clones). Before this
   revision the model did not exist, so no operator can have deliberately
   excluded it; rows with empty model_codes already mean "any model" and are
   left untouched.

Rollback removes the column, the inserted eleven_v3 row and the model_codes
entries. Voice bots configured with eleven_v3 fail validation afterwards —
downgrade only together with reverting those configurations.
"""
import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1b3d5f7"
down_revision: Union[str, None] = "f4b6d8e0a2c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL_CODE = "eleven_v3"

# Official supported-language list (ISO 639-1 where one exists; ElevenLabs
# uses bare short codes on the wire — "fil"/"ceb" have no two-letter form).
_ELEVEN_V3_LANGS = [
    "af", "ar", "hy", "as", "az", "be", "bn", "bs", "bg", "ca", "ceb", "ny",
    "hr", "cs", "da", "nl", "en", "et", "fil", "fi", "fr", "gl", "ka", "de",
    "el", "gu", "ha", "he", "hi", "hu", "is", "id", "ga", "it", "ja", "jv",
    "kn", "kk", "ky", "ko", "lv", "ln", "lt", "lb", "mk", "ms", "ml", "zh",
    "mr", "ne", "no", "ps", "fa", "pl", "pt", "pa", "ro", "ru", "sr", "sd",
    "sk", "sl", "so", "es", "sw", "sv", "ta", "te", "th", "tr", "uk", "ur",
    "vi", "cy",
]

_ELEVEN_V3_SCHEMA = {
    "stability": {
        "type": "enum", "values": [0.0, 0.5, 1.0], "default": 0.5,
        "labels": {"0": "Creative", "0.5": "Natural", "1": "Robust"},
        "label": "Stability",
        "help": "Eleven v3 accepts three presets: Creative (expressive, may "
                "hallucinate), Natural (balanced) or Robust (very stable).",
    },
    "similarity_boost": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 1.0, "step": 0.05,
        "label": "Similarity boost",
        "help": "How closely synthesis adheres to the original voice timbre.",
    },
    "style": {
        "type": "number", "min": 0.0, "max": 1.0, "default": 0.0, "step": 0.05,
        "label": "Style", "advanced": True,
        "help": "Style exaggeration. 0 is fastest and most stable.",
    },
}

_DESCRIPTIONS = {
    "eleven_v3": (
        "Most expressive ElevenLabs model (alpha): emotional range, audio tags, "
        "70+ languages. High latency, 5,000-character limit, no realtime "
        "streaming — synthesized per reply over REST; previews and non-realtime "
        "use recommended."
    ),
    "eleven_flash_v2_5": (
        "Ultra-low-latency model (~75 ms) for realtime conversation over the "
        "streaming WebSocket. 32 languages, 40,000-character limit. "
        "Recommended for live calls."
    ),
    "eleven_turbo_v2_5": (
        "Deprecated quality/latency-balance model (32 languages). Superseded "
        "by Eleven Flash v2.5."
    ),
}


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. description column ────────────────────────────────────────────
    if not _has_column("provider_models", "description"):
        op.add_column(
            "provider_models",
            sa.Column("description", sa.String(500), nullable=True),
        )

    for code, description in _DESCRIPTIONS.items():
        # Backfill only where empty — operator-entered text is never replaced.
        bind.execute(
            sa.text(
                "UPDATE provider_models SET description = :description "
                "WHERE provider_code = 'elevenlabs' AND capability = 'tts' "
                "AND code = :code "
                "AND (description IS NULL OR description = '')"
            ),
            {"description": description, "code": code},
        )

    # ── 2. eleven_v3 provider model (insert-if-missing) ──────────────────
    exists = bind.execute(
        sa.text(
            "SELECT id FROM provider_models WHERE provider_code = 'elevenlabs' "
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
                "sort_order, is_deleted) VALUES (:id, 'elevenlabs', 'tts', "
                ":code, :display_name, :description, :languages, :codecs, "
                ":sample_rates, :streaming, :params_schema, :is_default, "
                "'active', :sort_order, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "code": _MODEL_CODE,
                "display_name": "Eleven v3 (expressive)",
                "description": _DESCRIPTIONS[_MODEL_CODE],
                "languages": json.dumps(_ELEVEN_V3_LANGS),
                "codecs": json.dumps(["pcm", "ulaw", "alaw"]),
                "sample_rates": json.dumps([8000, 16000, 22050, 24000]),
                "streaming": False,
                "params_schema": json.dumps(_ELEVEN_V3_SCHEMA),
                "is_default": False,
                "sort_order": 1,
            },
        )

    # ── 3. voice model_codes backfill (additive only) ────────────────────
    rows = bind.execute(
        sa.text(
            "SELECT id, model_codes FROM voice_profiles "
            "WHERE provider = 'elevenlabs' AND model_codes IS NOT NULL"
        )
    ).all()
    for voice_id, raw in rows:
        codes = raw if isinstance(raw, list) else json.loads(raw or "[]")
        if not codes or _MODEL_CODE in codes:
            # Empty list already means "any model of the provider".
            continue
        codes.append(_MODEL_CODE)
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes = :codes WHERE id = :id"),
            {"codes": json.dumps(codes), "id": voice_id},
        )


def downgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(
        sa.text(
            "SELECT id, model_codes FROM voice_profiles "
            "WHERE provider = 'elevenlabs' AND model_codes IS NOT NULL"
        )
    ).all()
    for voice_id, raw in rows:
        codes = raw if isinstance(raw, list) else json.loads(raw or "[]")
        if _MODEL_CODE not in codes:
            continue
        codes = [c for c in codes if c != _MODEL_CODE]
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes = :codes WHERE id = :id"),
            {"codes": json.dumps(codes), "id": voice_id},
        )

    bind.execute(
        sa.text(
            "DELETE FROM provider_models WHERE provider_code = 'elevenlabs' "
            "AND capability = 'tts' AND code = :code"
        ),
        {"code": _MODEL_CODE},
    )

    if _has_column("provider_models", "description"):
        op.drop_column("provider_models", "description")
