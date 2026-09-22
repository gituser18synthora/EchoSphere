"""Eleven v3 Conversational: operator setting for ElevenLabs native breathing.

Revision ID: f5b7d9c1e3a7
Revises: e3a5c7b9d1f5
Create Date: 2026-09-22

Adds ``native_breathing`` (boolean, default False) to the
eleven_v3_conversational params schema.

It is a MODEL setting rather than a Natural Conversation one on purpose:
only this model can parse the audio tag, and routing it through the model's
catalog schema means the existing machinery already persists it
(voice_bot_settings.tts_settings), validates it, renders it in the UI and
hands it to both previews and live calls.

It is deliberately INDEPENDENT of the "Breathing" control. That control owns
EchoSphere's own pre-rendered breath clips and keeps behaving exactly as
configured whether or not this is enabled; the runtime only prevents the two
from breathing into the same moment of the same reply.

Guarded: the schema is rewritten only while it still matches the previously
shipped stability-only shape, so an operator-edited schema is never replaced.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9c1e3a7"
down_revision: Union[str, None] = "e3a5c7b9d1f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL = "eleven_v3_conversational"

_STABILITY = {
    "type": "enum", "values": [0.0, 0.5, 1.0], "default": 0.5,
    "labels": {"0": "Creative", "0.5": "Natural", "1": "Robust"},
    "label": "Stability",
    "help": "Eleven v3 accepts three presets: Creative (expressive, may "
            "hallucinate), Natural (balanced) or Robust (very stable).",
}
_NATIVE_BREATHING = {
    "type": "boolean", "default": False, "advanced": True,
    "label": "ElevenLabs native breathing",
    "help": "Let ElevenLabs generate an occasional soft breath inside its "
            "own speech (Eleven v3 audio tag). Independent of the Breathing "
            "setting, which controls EchoSphere's own breath clips. Off by "
            "default.",
}

_OLD = {"stability": _STABILITY}
_NEW = {"stability": _STABILITY, "native_breathing": _NATIVE_BREATHING}


def _row(bind):
    return bind.execute(
        sa.text("SELECT id, params_schema FROM provider_models WHERE "
                "provider_code='elevenlabs' AND capability='tts' AND code=:c"),
        {"c": _MODEL},
    ).first()


def _swap(bind, expect: dict, want: dict) -> None:
    row = _row(bind)
    if row is None:
        return
    current = row.params_schema
    if not isinstance(current, dict):
        current = json.loads(current or "{}")
    if current != expect:
        return  # operator-managed schema — left untouched
    bind.execute(
        sa.text("UPDATE provider_models SET params_schema=:s WHERE id=:id"),
        {"s": json.dumps(want), "id": row.id},
    )


def upgrade() -> None:
    _swap(op.get_bind(), _OLD, _NEW)


def downgrade() -> None:
    _swap(op.get_bind(), _NEW, _OLD)
