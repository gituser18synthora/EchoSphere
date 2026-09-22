"""ElevenLabs Eleven v3 Conversational as a selectable streaming TTS model.

Revision ID: d1f3b5a7c9e1
Revises: c7e9a1b3d5f7
Create Date: 2026-09-22

Eleven v3 streams in realtime after all — just not on the text-to-speech
WebSocket. ``eleven_v3_conversational`` runs over the Text-to-Dialogue
multi-context socket (shared/providers/tts/elevenlabs_v3_ws.py), so unlike
``eleven_v3`` (which stays REST-only, streaming=0) it is valid as a bot's
default engine, a per-language override AND a fallback engine.

1. Inserts the ``eleven_v3_conversational`` provider_models row when missing,
   with streaming=1 and is_default=0 — Flash v2.5 remains the default model
   and no existing bot configuration is touched. Languages are derived from
   the supported_languages table exactly like the eleven_v3 row (both models
   carry the same 74-language coverage per GET /v1/models, 2026-09-22).
   Sample rates are only the ones probed on that endpoint: 8000/16000/24000
   (ulaw_8000, pcm_8000, pcm_16000, pcm_24000 all returned audio; pcm_22050
   was NOT tested, so it is not advertised). params_schema carries
   ``stability`` alone — the only voice setting the dialogue models accept.

2. Additively appends the model to the model_codes of ``vp-el-monika`` ONLY:
   that is the voice synthesis was actually verified with. Other ElevenLabs
   voices keep their existing model lists until each is probed, so the UI
   never offers an untested voice/model pair. Rows with empty model_codes
   already mean "any model" and are left untouched.

3. Deliberately adds NO provider_pricing row. ElevenLabs' per-character rate
   for this model is unverified, and inventing one would put a fabricated
   number into conversation costing. Without a row, shared/billing/metering
   records the usage quantities and marks the event
   ``pricing_status='missing_price'``, which /usage surfaces to Super Admin
   as ``missingPriceEvents`` — usage is visibly unpriced, never silently
   free. Add the row once the rate is confirmed.

Rollback removes the model_codes entry and the inserted row. Bots configured
with eleven_v3_conversational fail validation afterwards — downgrade only
together with reverting those configurations.
"""
import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1f3b5a7c9e1"
down_revision: Union[str, None] = "c7e9a1b3d5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL_CODE = "eleven_v3_conversational"
_VERIFIED_VOICE_IDS = ("vp-el-monika",)

_DISPLAY_NAME = "Eleven v3 Conversational"
_DESCRIPTION = (
    "Expressive Eleven v3 tuned for realtime conversation (~280 ms claimed; "
    "250-625 ms measured to first audio). Streams over the Text-to-Dialogue "
    "WebSocket, so it works for live calls, per-language overrides and "
    "fallback. Speaks all nine platform languages, unlike Flash v2.5. "
    "Stability is the only voice setting; no speed control."
)

# stability presets identical to the eleven_v3 row (all three probed and
# accepted on the dialogue endpoint, 2026-09-22).
_SCHEMA = {
    "stability": {
        "type": "enum", "values": [0.0, 0.5, 1.0], "default": 0.5,
        "labels": {"0": "Creative", "0.5": "Natural", "1": "Robust"},
        "label": "Stability",
        "help": "Eleven v3 accepts three presets: Creative (expressive, may "
                "hallucinate), Natural (balanced) or Robust (very stable).",
    },
}

# Official Eleven v3 base ISO codes (same family coverage as eleven_v3).
_ELEVEN_V3_ISO_CODES = frozenset({
    "af", "ar", "hy", "as", "az", "be", "bn", "bs", "bg", "ca", "ceb", "ny",
    "hr", "cs", "da", "nl", "en", "et", "fil", "fi", "fr", "gl", "ka", "de",
    "el", "gu", "ha", "he", "hi", "hu", "is", "id", "ga", "it", "ja", "jv",
    "kn", "kk", "ky", "ko", "lv", "ln", "lt", "lb", "mk", "ms", "ml", "zh",
    "cmn", "mr", "ne", "no", "ps", "fa", "pl", "pt", "pa", "ro", "ru", "sr",
    "sd", "sk", "sl", "so", "es", "sw", "sv", "ta", "te", "th", "tr", "uk",
    "ur", "vi", "cy",
})


def _catalog_locales(bind) -> list[str]:
    """Platform locales this model speaks, from the languages master."""
    rows = bind.execute(
        sa.text("SELECT code, iso_code FROM supported_languages "
                "ORDER BY sort_order, code")
    ).all()
    locales: list[str] = []
    for code, iso in rows:
        code = (code or "").strip()
        if not code or code in locales:
            continue
        base = (iso or code.split("-")[0]).strip().lower()
        if base in _ELEVEN_V3_ISO_CODES:
            locales.append(code)
    return locales


def _eleven_v3_languages(bind) -> list[str]:
    """Fall back to the eleven_v3 row's list when the languages table is
    empty (fresh database: the bootstrap seed derives it instead)."""
    locales = _catalog_locales(bind)
    if locales:
        return locales
    row = bind.execute(
        sa.text("SELECT languages FROM provider_models WHERE "
                "provider_code = 'elevenlabs' AND capability = 'tts' "
                "AND code = 'eleven_v3'")
    ).first()
    if row is None:
        return []
    raw = row.languages
    return raw if isinstance(raw, list) else json.loads(raw or "[]")


def upgrade() -> None:
    bind = op.get_bind()

    # ── 1. provider model (insert-if-missing) ────────────────────────────
    exists = bind.execute(
        sa.text("SELECT id FROM provider_models WHERE provider_code = 'elevenlabs' "
                "AND capability = 'tts' AND code = :code"),
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
                "display_name": _DISPLAY_NAME,
                "description": _DESCRIPTION,
                "languages": json.dumps(_eleven_v3_languages(bind)),
                "codecs": json.dumps(["pcm", "ulaw", "alaw"]),
                "sample_rates": json.dumps([8000, 16000, 24000]),
                "streaming": True,
                "params_schema": json.dumps(_SCHEMA),
                "is_default": False,
                "sort_order": 2,
            },
        )

    # ── 2. verified voice model_codes (additive, probed voices only) ─────
    for voice_id in _VERIFIED_VOICE_IDS:
        row = bind.execute(
            sa.text("SELECT id, model_codes FROM voice_profiles WHERE id = :id"),
            {"id": voice_id},
        ).first()
        if row is None:
            continue
        codes = row.model_codes if isinstance(row.model_codes, list) else json.loads(
            row.model_codes or "[]")
        if not codes or _MODEL_CODE in codes:
            # Empty list already means "any model of the provider".
            continue
        codes.append(_MODEL_CODE)
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes = :codes WHERE id = :id"),
            {"codes": json.dumps(codes), "id": voice_id},
        )

    # ── 3. no provider_pricing row on purpose — see the module docstring ──


def downgrade() -> None:
    bind = op.get_bind()

    rows = bind.execute(
        sa.text("SELECT id, model_codes FROM voice_profiles "
                "WHERE provider = 'elevenlabs' AND model_codes IS NOT NULL")
    ).all()
    for voice_id, raw in rows:
        codes = raw if isinstance(raw, list) else json.loads(raw or "[]")
        if _MODEL_CODE not in codes:
            continue
        bind.execute(
            sa.text("UPDATE voice_profiles SET model_codes = :codes WHERE id = :id"),
            {"codes": json.dumps([c for c in codes if c != _MODEL_CODE]),
             "id": voice_id},
        )

    bind.execute(
        sa.text("DELETE FROM provider_models WHERE provider_code = 'elevenlabs' "
                "AND capability = 'tts' AND code = :code"),
        {"code": _MODEL_CODE},
    )
