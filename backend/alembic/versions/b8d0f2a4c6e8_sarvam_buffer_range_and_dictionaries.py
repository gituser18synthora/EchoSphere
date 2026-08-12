"""Sarvam buffer range fix + pronunciation dictionaries.

Revision ID: b8d0f2a4c6e8
Revises: d0f2a4b6c8e0
Create Date: 2026-08-12

1. Corrects the Sarvam WebSocket ``min_buffer_size`` bounds in the TTS
   catalog schemas. The seeded range (10–500, default 40) predates the
   documented contract; Sarvam rejects values outside 30–200 with a bare
   ``invalid_input``, so EchoSphere must enforce the real range itself.
   Verified against the official Sarvam streaming docs (2026-08):
   min 30, max 200, default 50. Stored bot settings outside the corrected
   range are clamped in the same transaction so existing configurations
   keep validating (values were provider-rejected at synthesis time anyway,
   never functional).

2. Restructures ``dict_id`` on bulbul:v3 from a raw advanced text field into
   a "Pronunciation" section entry rendered as a dictionary selector
   (``widget: "dictionary"``). bulbul:v2 does not support dictionaries and
   keeps no dict_id key.

3. Creates ``pronunciation_dictionaries`` — tenant-scoped metadata for
   dictionaries uploaded to the provider account (Sarvam stores only the
   pronunciations, not names, and its list API returns bare ids). The
   provider remains the source of truth for the mappings; this table owns
   the tenant-facing name and a cached summary.

Rollback restores the previous schema JSON for the two Sarvam models and
drops the table (uploaded provider dictionaries are NOT deleted from the
Sarvam account — they simply lose their local names).
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b8d0f2a4c6e8"
down_revision: Union[str, None] = "d0f2a4b6c8e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Documented Sarvam WebSocket streaming bounds (2026-08).
_MIN_BUFFER = {
    "type": "integer", "min": 30, "max": 200, "default": 50,
    "label": "Min buffer size",
    "help": "Minimum characters buffered before Sarvam starts synthesis. "
            "Lower values reduce initial buffering but may reduce sentence "
            "context. Sarvam accepts 30–200 (WebSocket streaming only).",
}
_MAX_CHUNK = {
    "type": "integer", "min": 50, "max": 500, "default": 150,
    "label": "Max chunk length",
    "help": "Maximum characters per synthesis chunk. 120–150 recommended for realtime.",
}
_SEND_COMPLETION = {
    "type": "boolean", "default": True, "label": "Completion event", "advanced": True,
    "help": "Ask Sarvam to signal when synthesis of flushed text has finished.",
}

_V3_SCHEMA = {
    "pace": {
        "type": "number", "min": 0.5, "max": 2.0, "default": 1.0, "step": 0.05,
        "label": "Pace", "help": "Speech speed multiplier (bulbul:v3 range 0.5–2.0).",
    },
    "temperature": {
        "type": "number", "min": 0.01, "max": 1.0, "default": 0.6, "step": 0.01,
        "label": "Temperature",
        "help": "Synthesis randomness — lower is more deterministic (bulbul:v3 only).",
    },
    "min_buffer_size": _MIN_BUFFER,
    "max_chunk_length": _MAX_CHUNK,
    "dict_id": {
        "type": "string", "default": None, "optional": True, "max_length": 100,
        "label": "Pronunciation dictionary",
        "widget": "dictionary", "section": "pronunciation",
        "help": "Fixes how specific words (brands, acronyms, names) are spoken. "
                "Supported by bulbul:v3 only.",
    },
    "send_completion_event": _SEND_COMPLETION,
    # v3 always preprocesses server-side; kept as a fixed (read-only) fact so
    # the UI can say so without offering a dead toggle.
    "enable_preprocessing": {
        "type": "boolean", "default": True, "fixed": True, "label": "Preprocessing",
        "help": "Text normalization (numbers, dates, currencies, mixed-language "
                "text) before synthesis. Always enabled for bulbul:v3.",
    },
}

_V2_SCHEMA = {
    "pace": {
        "type": "number", "min": 0.3, "max": 3.0, "default": 1.0, "step": 0.05,
        "label": "Pace", "help": "Speech speed multiplier (bulbul:v2 range 0.3–3.0).",
    },
    "pitch": {
        "type": "number", "min": -0.75, "max": 0.75, "default": 0.0, "step": 0.05,
        "label": "Pitch", "help": "Voice pitch adjustment (bulbul:v2 only).",
    },
    "loudness": {
        "type": "number", "min": 0.3, "max": 3.0, "default": 1.0, "step": 0.1,
        "label": "Loudness", "help": "Volume multiplier (bulbul:v2 only).",
    },
    "enable_preprocessing": {
        "type": "boolean", "default": False, "label": "Preprocessing",
        "help": "Normalizes numbers, dates, currencies and mixed-language text "
                "before synthesis.",
    },
    "min_buffer_size": _MIN_BUFFER,
    "max_chunk_length": _MAX_CHUNK,
    "send_completion_event": _SEND_COMPLETION,
}

# Pre-revision schemas, for downgrade.
_OLD_MIN_BUFFER = {
    "type": "integer", "min": 10, "max": 500, "default": 40,
    "label": "Min buffer size",
    "help": "Characters buffered before audio generation starts. 30–40 recommended for realtime.",
}
_OLD_V3_SCHEMA = {
    **{k: v for k, v in _V3_SCHEMA.items() if k not in ("min_buffer_size", "dict_id", "enable_preprocessing")},
    "min_buffer_size": _OLD_MIN_BUFFER,
    "dict_id": {
        "type": "string", "default": None, "optional": True, "max_length": 100,
        "label": "Pronunciation dictionary ID", "advanced": True,
        "help": "Optional Sarvam pronunciation dictionary applied during synthesis.",
    },
    "enable_preprocessing": {
        "type": "boolean", "default": True, "fixed": True, "label": "Preprocessing",
        "help": "Text normalization before synthesis. Always enabled for bulbul:v3.",
    },
}
_OLD_V2_SCHEMA = {
    **{k: v for k, v in _V2_SCHEMA.items() if k not in ("min_buffer_size", "enable_preprocessing")},
    "min_buffer_size": _OLD_MIN_BUFFER,
    "enable_preprocessing": {
        "type": "boolean", "default": False, "label": "Preprocessing",
        "help": "Enable text normalization before synthesis.",
    },
}


def _write_schema(conn, model_code: str, schema: dict) -> None:
    conn.execute(
        sa.text(
            "UPDATE provider_models SET params_schema = :schema, updated_at = NOW() "
            "WHERE capability = 'tts' AND provider_code = 'sarvam' AND code = :code"
        ),
        {"schema": json.dumps(schema), "code": model_code},
    )


def _clamp_stored_buffer_sizes(conn) -> None:
    """Clamp persisted Sarvam min_buffer_size values into 30–200.

    Out-of-range values were rejected by Sarvam at synthesis time (dead
    setting, never functional behavior), so clamping cannot change what a
    call actually sounded like — it only makes the stored row valid again
    under the corrected schema.
    """
    rows = conn.execute(sa.text(
        "SELECT bot_id, tts_settings FROM voice_bot_settings "
        "WHERE tts_provider = 'sarvam' AND tts_settings IS NOT NULL"
    )).fetchall()
    for bot_id, blob in rows:
        settings = json.loads(blob) if isinstance(blob, str) else (blob or {})
        value = settings.get("min_buffer_size")
        if not isinstance(value, int) or isinstance(value, bool):
            continue
        clamped = min(200, max(30, value))
        if clamped == value:
            continue
        settings["min_buffer_size"] = clamped
        conn.execute(
            sa.text(
                "UPDATE voice_bot_settings SET tts_settings = :settings, "
                "updated_at = NOW() WHERE bot_id = :bot_id"
            ),
            {"settings": json.dumps(settings), "bot_id": bot_id},
        )


def upgrade() -> None:
    conn = op.get_bind()
    _write_schema(conn, "bulbul:v3", _V3_SCHEMA)
    _write_schema(conn, "bulbul:v2", _V2_SCHEMA)
    _clamp_stored_buffer_sizes(conn)

    op.create_table(
        "pronunciation_dictionaries",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("tenant_id", sa.String(40), sa.ForeignKey("tenants.id"),
                  nullable=False, index=True),
        sa.Column("provider", sa.String(40), nullable=False, server_default="sarvam"),
        # Provider-assigned id (e.g. "p_5cb7faa6") passed to TTS as dict_id.
        sa.Column("provider_dict_id", sa.String(100), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=True),
        # Cached summary of the uploaded mappings (provider stays the source
        # of truth for the actual pronunciations): {"hi-IN": 12, "en-IN": 3}.
        sa.Column("language_word_counts", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("updated_by", sa.String(40), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(40), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    _write_schema(conn, "bulbul:v3", _OLD_V3_SCHEMA)
    _write_schema(conn, "bulbul:v2", _OLD_V2_SCHEMA)
    op.drop_table("pronunciation_dictionaries")
