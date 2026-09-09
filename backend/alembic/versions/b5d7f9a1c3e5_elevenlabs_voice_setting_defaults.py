"""ElevenLabs voice-setting defaults aligned with the ElevenLabs API.

Revision ID: b5d7f9a1c3e5
Revises: f3a5c7e9b1d4
Create Date: 2026-09-04

The platform seeded every ElevenLabs voice (and the Flash/Turbo parameter
schema defaults) with ``stability 0.0`` and ``similarity_boost 1.0``. The
ElevenLabs API defaults are ``0.5`` / ``0.75``: stability 0 gives erratic,
wobbly delivery (worst on Hindi and other non-English text) and similarity
1.0 reproduces artifacts of the voice's source recording — which is how the
Voice tab preview of any ElevenLabs voice sounded wrong out of the box.

1. ``provider_models.params_schema`` — ElevenLabs TTS models: ``stability``
   default 0.0 → 0.5 (number-typed schemas only; Eleven v3's enum already
   defaults to Natural/0.5) and ``similarity_boost`` default 1.0 → 0.75.
2. ``voice_profiles.provider_settings`` — ElevenLabs profiles still carrying
   the untouched seed pair (stability 0.0 AND similarity_boost 1.0) move to
   0.5 / 0.75. A profile an operator tuned away from that exact pair is left
   alone. Tenant clones are included: they were created with the same seed
   values and nobody chose them.

Bot-level ``tts_settings`` are NOT touched — those are explicit per-bot
choices; operators re-save from the Voice tab if they want the new defaults.

Rollback restores the seed values on the same rows (schema defaults back to
0.0 / 1.0, and profiles carrying exactly 0.5 / 0.75 back to 0.0 / 1.0).
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5d7f9a1c3e5"
down_revision: Union[str, None] = "f3a5c7e9b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_STABILITY, NEW_STABILITY = 0.0, 0.5
OLD_SIMILARITY, NEW_SIMILARITY = 1.0, 0.75


def _load(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fixed_schema(schema: dict | None, *, stability: float, similarity: float,
                 from_stability: float, from_similarity: float) -> dict | None:
    """Move the stability/similarity defaults; None when nothing changes.

    Only number-typed stability specs are touched (Eleven v3 uses an enum
    with its own Natural default).
    """
    if not schema:
        return None
    out = json.loads(json.dumps(schema))
    changed = False
    spec = out.get("stability")
    if (isinstance(spec, dict) and spec.get("type") == "number"
            and _num(spec.get("default")) == from_stability):
        spec["default"] = stability
        changed = True
    spec = out.get("similarity_boost")
    if isinstance(spec, dict) and _num(spec.get("default")) == from_similarity:
        spec["default"] = similarity
        changed = True
    return out if changed else None


def fixed_settings(settings: dict | None, *, stability: float, similarity: float,
                   from_stability: float, from_similarity: float) -> dict | None:
    """Move a profile's seed pair to the new pair; None when it is not the
    exact seed pair (an operator tuned it) or a value is missing."""
    if not settings:
        return None
    if (_num(settings.get("stability")) == from_stability
            and _num(settings.get("similarity_boost")) == from_similarity):
        out = dict(settings)
        out["stability"] = stability
        out["similarity_boost"] = similarity
        return out
    return None


def _apply(*, stability: float, similarity: float,
           from_stability: float, from_similarity: float) -> None:
    conn = op.get_bind()
    models = conn.execute(sa.text(
        "SELECT id, params_schema FROM provider_models "
        "WHERE provider_code = 'elevenlabs' AND capability = 'tts'"
    )).mappings().all()
    for row in models:
        schema = fixed_schema(
            _load(row["params_schema"]), stability=stability, similarity=similarity,
            from_stability=from_stability, from_similarity=from_similarity,
        )
        if schema is not None:
            conn.execute(
                sa.text("UPDATE provider_models SET params_schema = :schema WHERE id = :id"),
                {"schema": json.dumps(schema), "id": row["id"]},
            )
    profiles = conn.execute(sa.text(
        "SELECT id, provider_settings FROM voice_profiles WHERE provider = 'elevenlabs'"
    )).mappings().all()
    for row in profiles:
        settings = fixed_settings(
            _load(row["provider_settings"]), stability=stability, similarity=similarity,
            from_stability=from_stability, from_similarity=from_similarity,
        )
        if settings is not None:
            conn.execute(
                sa.text(
                    "UPDATE voice_profiles SET provider_settings = :settings WHERE id = :id"
                ),
                {"settings": json.dumps(settings), "id": row["id"]},
            )


def upgrade() -> None:
    _apply(stability=NEW_STABILITY, similarity=NEW_SIMILARITY,
           from_stability=OLD_STABILITY, from_similarity=OLD_SIMILARITY)


def downgrade() -> None:
    _apply(stability=OLD_STABILITY, similarity=OLD_SIMILARITY,
           from_stability=NEW_STABILITY, from_similarity=NEW_SIMILARITY)
