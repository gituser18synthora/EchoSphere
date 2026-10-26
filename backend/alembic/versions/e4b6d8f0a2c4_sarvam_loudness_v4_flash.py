"""Sarvam loudness on bulbul:v4-flash.

Revision ID: e4b6d8f0a2c4
Revises: d3f5b7c9e1a2
Create Date: 2026-10-26

1. Adds ``loudness`` (0.1–2.5, default 1.0, step 0.1) to the catalog schema
   of the Sarvam TTS model bulbul:v4-flash, right after ``pace``, so the
   Studio offers the slider and saving validates the range. 0.1–2.5 is the
   Sarvam playground's range for v4-flash; the API rejects anything outside
   it with 400 (measured 2026-10-26, REST and WebSocket alike: 0.1 renders
   about 20 dB quieter, 2.5 about 6 dB louder). bulbul:v2 keeps 0.3–3.0;
   bulbul:v3 does not offer it.
2. The bulbul:v2 Loudness help text names its range (0.3–3.0) instead of
   "(bulbul:v2 only)".

Only catalog schemas change. No bot stores a loudness on v4-flash (checked
on the local and demo databases 2026-10-26) and the adapters never send the
1.0 default, so every existing bot renders exactly as before. A schema that
already has the key is left alone (fresh installs get it from the catalog
seed).

Rollback removes the key and restores the v2 help text. A bot that saved a
loudness on v4-flash then fails validation on its next save until the value
is removed.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4b6d8f0a2c4"
down_revision: Union[str, None] = "d3f5b7c9e1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL_CODE = "bulbul:v4-flash"
_LOUDNESS = {
    "type": "number", "min": 0.1, "max": 2.5, "default": 1.0, "step": 0.1,
    "label": "Loudness", "help": "Volume multiplier (bulbul:v4-flash range 0.1–2.5).",
}
_V2_HELP_OLD = "Volume multiplier (bulbul:v2 only)."
_V2_HELP_NEW = "Volume multiplier (bulbul:v2 range 0.3–3.0)."


def _schemas(conn, code: str) -> list[tuple[str, dict]]:
    rows = conn.execute(
        sa.text(
            "SELECT id, params_schema FROM provider_models "
            "WHERE capability = 'tts' AND provider_code = 'sarvam' AND code = :code"
        ),
        {"code": code},
    ).fetchall()
    return [
        (row_id, json.loads(blob) if isinstance(blob, (str, bytes)) else (blob or {}))
        for row_id, blob in rows
    ]


def _write(conn, row_id: str, schema: dict) -> None:
    conn.execute(
        sa.text(
            "UPDATE provider_models SET params_schema = :schema, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
        ),
        {"schema": json.dumps(schema), "id": row_id},
    )


def _v2_help(conn, old: str, new: str) -> None:
    for row_id, schema in _schemas(conn, "bulbul:v2"):
        spec = schema.get("loudness")
        if isinstance(spec, dict) and spec.get("help") == old:
            schema["loudness"] = {**spec, "help": new}
            _write(conn, row_id, schema)


def upgrade() -> None:
    conn = op.get_bind()
    for row_id, schema in _schemas(conn, _MODEL_CODE):
        if "loudness" in schema:
            continue
        updated: dict = {}
        for key, value in schema.items():
            updated[key] = value
            if key == "pace":
                updated["loudness"] = _LOUDNESS
        updated.setdefault("loudness", _LOUDNESS)
        _write(conn, row_id, updated)
    _v2_help(conn, _V2_HELP_OLD, _V2_HELP_NEW)


def downgrade() -> None:
    conn = op.get_bind()
    for row_id, schema in _schemas(conn, _MODEL_CODE):
        if "loudness" in schema:
            _write(conn, row_id, {k: v for k, v in schema.items() if k != "loudness"})
    _v2_help(conn, _V2_HELP_NEW, _V2_HELP_OLD)
