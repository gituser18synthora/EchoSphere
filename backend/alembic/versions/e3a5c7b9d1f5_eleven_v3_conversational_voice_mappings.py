"""Eleven v3 Conversational: verified voice mappings + voice-metadata fixes.

Revision ID: e3a5c7b9d1f5
Revises: d1f3b5a7c9e1
Create Date: 2026-09-22

Follow-up to d1f3b5a7c9e1, which shipped the model with a single verified
voice. Every platform ElevenLabs voice has since been synthesized on the
Text-to-Dialogue endpoint (Hindi and Malayalam, 2026-09-22) and all returned
audio, so the compatibility mapping is widened on evidence rather than on the
assumption that "same provider = same models".

1. ``eleven_v3_conversational`` sample_rates gains 22050. All five PCM rates
   the streaming router may request (8000/16000/22050/24000, plus ulaw_8000)
   were probed; 22050 was previously withheld only because it was untested.

2. model_codes of the seeded platform voices gain the model (additive; rows
   with an empty list already mean "any model of the provider", and operator
   edits are never removed).

3. Two metadata corrections, from ElevenLabs' own voice labels
   (GET /v1/voices) rather than from the voice names:
   * ``vp-el-shivank`` gender female -> male. This is not cosmetic: gender
     selects the synthesized breath/filler clips, so the wrong value makes
     the bot breathe in the wrong voice.
   * ``vp-el-leo`` accent "Indian" -> "American". The old seed hardcoded an
     Indian accent for every ElevenLabs voice; Leo is an American English
     narrator.
   Both are applied ONLY where the stored value still equals the incorrect
   seeded one, so an operator who already fixed or deliberately changed a row
   is never overwritten.

No pricing row is added — the per-character rate for this model is still
unverified, and metering records such usage as pricing_status='missing_price'.

Rollback restores the previous sample_rates, removes the added model_codes
entries and reverts the two metadata values.
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a5c7b9d1f5"
down_revision: Union[str, None] = "d1f3b5a7c9e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL = "eleven_v3_conversational"
_RATES_NEW = [8000, 16000, 22050, 24000]
_RATES_OLD = [8000, 16000, 24000]

# Every seeded platform voice — all synthesis-verified on the endpoint.
_VOICES = (
    "vp-el-monika", "vp-el-raju", "vp-el-niraj", "vp-el-leo", "vp-el-viraj",
    "vp-el-shardul", "vp-el-anvi", "vp-el-shivank",
)


def _parse(raw):
    return raw if isinstance(raw, list) else json.loads(raw or "[]")


def _set_rates(bind, rates):
    row = bind.execute(
        sa.text("SELECT id, sample_rates FROM provider_models WHERE "
                "provider_code='elevenlabs' AND capability='tts' AND code=:c"),
        {"c": _MODEL},
    ).first()
    if row is None:
        return
    bind.execute(
        sa.text("UPDATE provider_models SET sample_rates=:r WHERE id=:id"),
        {"r": json.dumps(rates), "id": row.id},
    )


def upgrade() -> None:
    bind = op.get_bind()
    _set_rates(bind, _RATES_NEW)

    for vid in _VOICES:
        row = bind.execute(
            sa.text("SELECT id, model_codes FROM voice_profiles WHERE id=:id"),
            {"id": vid},
        ).first()
        if row is None:
            continue
        codes = _parse(row.model_codes)
        if not codes or _MODEL in codes:
            continue
        codes.append(_MODEL)
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes=:c WHERE id=:id"),
            {"c": json.dumps(codes), "id": vid},
        )

    # Guarded metadata corrections — only the stale seeded value is replaced.
    bind.execute(sa.text(
        "UPDATE voice_profiles SET gender='male' "
        "WHERE id='vp-el-shivank' AND gender='female'"))
    bind.execute(sa.text(
        "UPDATE voice_profiles SET accent='American' "
        "WHERE id='vp-el-leo' AND accent='Indian'"))


def downgrade() -> None:
    bind = op.get_bind()
    _set_rates(bind, _RATES_OLD)

    for vid in _VOICES:
        row = bind.execute(
            sa.text("SELECT id, model_codes FROM voice_profiles WHERE id=:id"),
            {"id": vid},
        ).first()
        if row is None:
            continue
        codes = _parse(row.model_codes)
        if _MODEL not in codes:
            continue
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes=:c WHERE id=:id"),
            {"c": json.dumps([c for c in codes if c != _MODEL]), "id": vid},
        )

    bind.execute(sa.text(
        "UPDATE voice_profiles SET gender='female' "
        "WHERE id='vp-el-shivank' AND gender='male'"))
    bind.execute(sa.text(
        "UPDATE voice_profiles SET accent='Indian' "
        "WHERE id='vp-el-leo' AND accent='American'"))
