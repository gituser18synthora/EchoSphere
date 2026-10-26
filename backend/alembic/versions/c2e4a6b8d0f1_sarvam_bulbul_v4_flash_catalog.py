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
3. Inserts the bulbul:v4-flash persona voices when missing: the 36 Hindi
   and English personas of the reviewed v3 -> v4 mapping
   (shared.providers.tts.sarvam_voices.v4_catalog_personas; snapshot below,
   identical to backend.seeds.provider_catalog_seed.sarvam_v4_persona_voices)
   as voice profiles ``vp-sv4-<persona>`` for that model only, offered for
   the nine v4-flash languages (never Malayalam / Odia). The bulbul:v3
   voices are not touched: the Studio lists persona voices for v4-flash.
4. Repairs a second TTS default: the API start-up seed of 683187650 created
   bulbul:v4-flash as default next to bulbul:v3. bulbul:v3 stays the only
   default; the flag is cleared only while another Sarvam TTS row is default.

Rollback removes the inserted model row, price and persona voices (step 4
is not undone). Bots moved to bulbul:v4-flash or a persona voice fail
validation afterwards — downgrade only together with moving them back.
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
# (id, name, gender, native locale, style, wire persona) — snapshot of
# backend.seeds.provider_catalog_seed.sarvam_v4_persona_voices(), 2026-10-26.
_PERSONA_VOICES = (
    ("vp-sv4-shubh_hi_customer", "Shubh · Hindi · Customer", "male", "hi-IN", "Customer", "shubh_hi_customer"),
    ("vp-sv4-aditya_hi_conversational", "Aditya · Hindi · Conversational", "male", "hi-IN", "Conversational", "aditya_hi_conversational"),
    ("vp-sv4-ritu_hi_customer", "Ritu · Hindi · Customer", "female", "hi-IN", "Customer", "ritu_hi_customer"),
    ("vp-sv4-priya_hi_recovery", "Priya · Hindi · Recovery", "female", "hi-IN", "Recovery", "priya_hi_recovery"),
    ("vp-sv4-rahul_hi_conversational", "Rahul · Hindi · Conversational", "male", "hi-IN", "Conversational", "rahul_hi_conversational"),
    ("vp-sv4-simran_enhi_customer", "Simran · Hinglish · Customer", "female", "hi-IN", "Customer", "simran_enhi_customer"),
    ("vp-sv4-kavya_hi_conversational", "Kavya · Hindi · Conversational", "female", "hi-IN", "Conversational", "kavya_hi_conversational"),
    ("vp-sv4-amit_hi_conversational", "Amit · Hindi · Conversational", "male", "hi-IN", "Conversational", "amit_hi_conversational"),
    ("vp-sv4-ishita_enhi_customer", "Ishita · Hinglish · Customer", "female", "hi-IN", "Customer", "ishita_enhi_customer"),
    ("vp-sv4-shreya_hi_conversational", "Shreya · Hindi · Conversational", "female", "hi-IN", "Conversational", "shreya_hi_conversational"),
    ("vp-sv4-ratan_hi_customer_expressive", "Ratan · Hindi · Customer expressive", "male", "hi-IN", "Customer expressive", "ratan_hi_customer_expressive"),
    ("vp-sv4-manan_hi_conversational", "Manan · Hindi · Conversational", "male", "hi-IN", "Conversational", "manan_hi_conversational"),
    ("vp-sv4-sumit_hi_conversational", "Sumit · Hindi · Conversational", "male", "hi-IN", "Conversational", "sumit_hi_conversational"),
    ("vp-sv4-roopa_hi_conversational", "Roopa · Hindi · Conversational", "female", "hi-IN", "Conversational", "roopa_hi_conversational"),
    ("vp-sv4-kabir_hi_conversational", "Kabir · Hindi · Conversational", "male", "hi-IN", "Conversational", "kabir_hi_conversational"),
    ("vp-sv4-aayan_hi_conversational", "Aayan · Hindi · Conversational", "male", "hi-IN", "Conversational", "aayan_hi_conversational"),
    ("vp-sv4-ashutosh_hi_conversational", "Ashutosh · Hindi · Conversational", "male", "hi-IN", "Conversational", "ashutosh_hi_conversational"),
    ("vp-sv4-advait_hi_character", "Advait · Hindi · Character", "male", "hi-IN", "Character", "advait_hi_character"),
    ("vp-sv4-anand_hi_news", "Anand · Hindi · News", "male", "hi-IN", "News", "anand_hi_news"),
    ("vp-sv4-tanya_hi_narration", "Tanya · Hindi · Narration", "female", "hi-IN", "Narration", "tanya_hi_narration"),
    ("vp-sv4-tarun_hi_conversational", "Tarun · Hindi · Conversational", "male", "hi-IN", "Conversational", "tarun_hi_conversational"),
    ("vp-sv4-sunny_enhi_customer", "Sunny · Hinglish · Customer", "male", "hi-IN", "Customer", "sunny_enhi_customer"),
    ("vp-sv4-mani_hi_conversational", "Mani · Hindi · Conversational", "male", "hi-IN", "Conversational", "mani_hi_conversational"),
    ("vp-sv4-shruti_hi_edtech", "Shruti · Hindi · Edtech", "female", "hi-IN", "Edtech", "shruti_hi_edtech"),
    ("vp-sv4-suhani_hi_social", "Suhani · Hindi · Social", "female", "hi-IN", "Social", "suhani_hi_social"),
    ("vp-sv4-mohit_hi_conversational", "Mohit · Hindi · Conversational", "male", "hi-IN", "Conversational", "mohit_hi_conversational"),
    ("vp-sv4-rehan_hi_social", "Rehan · Hindi · Social", "male", "hi-IN", "Social", "rehan_hi_social"),
    ("vp-sv4-shubh_enhi_companion", "Shubh · Hinglish · Companion", "male", "hi-IN", "Companion", "shubh_enhi_companion"),
    ("vp-sv4-ritu_en_edtech", "Ritu · English · Edtech", "female", "en-IN", "Edtech", "ritu_en_edtech"),
    ("vp-sv4-neha_en_customer", "Neha · English · Customer", "female", "en-IN", "Customer", "neha_en_customer"),
    ("vp-sv4-rohan_en_recovery", "Rohan · English · Recovery", "male", "en-IN", "Recovery", "rohan_en_recovery"),
    ("vp-sv4-simran_en_customer", "Simran · English · Customer", "female", "en-IN", "Customer", "simran_en_customer"),
    ("vp-sv4-dev_en_conversational", "Dev · English · Conversational", "male", "en-IN", "Conversational", "dev_en_conversational"),
    ("vp-sv4-ishita_en_customer", "Ishita · English · Customer", "female", "en-IN", "Customer", "ishita_en_customer"),
    ("vp-sv4-varun_en_ads", "Varun · English · Ads", "male", "en-IN", "Ads", "varun_en_ads"),
    ("vp-sv4-roopa_en_conversational", "Roopa · English · Conversational", "female", "en-IN", "Conversational", "roopa_en_conversational"),
)
# The nine v4-flash languages (platform codes); Malayalam/Odia have no personas.
_PERSONA_LOCALES = ["hi-IN", "en-IN", "bn-IN", "mr-IN", "gu-IN", "ta-IN", "te-IN", "kn-IN", "pa-IN"]
_SAMPLE_TEXT = "Hello! I can help you book your next appointment in just a minute."


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
                "sort_order": 1,
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

    for sort, (vid, name, gender, locale, style, persona) in enumerate(_PERSONA_VOICES, start=37):
        found = bind.execute(
            sa.text("SELECT id FROM voice_profiles WHERE id = :id"), {"id": vid},
        ).first()
        if found is not None:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO voice_profiles (id, source, name, gender, languages, "
                "locale, accent, styles, latency_ms, premium, sample_text, provider, "
                "provider_voice_id, speaking_rate, pitch, model_codes, "
                "provider_settings, is_default, status, sort_order, created_at, "
                "updated_at, is_deleted) VALUES (:id, 'platform', :name, :gender, "
                ":languages, :locale, 'Indian', :styles, 150, 0, :sample_text, "
                "'sarvam', :persona, 1.0, 1.0, :model_codes, :provider_settings, 0, "
                "'active', :sort, :now, :now, 0)"
            ),
            {
                "id": vid, "name": name, "gender": gender,
                "languages": json.dumps(_PERSONA_LOCALES), "locale": locale,
                "styles": json.dumps([style]), "sample_text": _SAMPLE_TEXT,
                "persona": persona, "model_codes": json.dumps([_MODEL_CODE]),
                "provider_settings": json.dumps({"pace": 1.0}),
                "sort": sort, "now": now,
            },
        )

    # Second default left by the start-up seed of 683187650: clear v4-flash's
    # flag while another Sarvam TTS model is the default.
    defaults = [
        row[0] for row in bind.execute(
            sa.text(
                "SELECT code FROM provider_models WHERE provider_code = 'sarvam' "
                "AND capability = 'tts' AND is_default = 1 AND is_deleted = 0"
            )
        ).fetchall()
    ]
    if _MODEL_CODE in defaults and len(defaults) > 1:
        bind.execute(
            sa.text(
                "UPDATE provider_models SET is_default = 0, updated_at = :now "
                "WHERE provider_code = 'sarvam' AND capability = 'tts' AND code = :code"
            ),
            {"code": _MODEL_CODE, "now": now},
        )


def downgrade() -> None:
    bind = op.get_bind()
    for vid, *_rest in _PERSONA_VOICES:
        bind.execute(sa.text("DELETE FROM voice_profiles WHERE id = :id"), {"id": vid})
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
