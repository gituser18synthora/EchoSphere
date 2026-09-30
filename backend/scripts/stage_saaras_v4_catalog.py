"""Stage the Sarvam ``saaras:v4`` STT catalog rows without running Alembic.

Writes exactly what migration ``a9c1e3b5d7f9`` writes — the
``provider_models`` row (saaras:v3 schema + ``keyterms``) and the
``provider_pricing`` row — so a database can carry the catalog entry before
its code is upgraded (the migration is insert-if-missing and later activates
a row staged inactive, see its docstring). Self-contained on purpose: it only
needs ``shared.config`` and SQLAlchemy, so it runs unchanged on a checkout
that does not yet contain the saaras:v4 code.

Usage (dry-run prints the rows it would write; nothing is changed):

    env/bin/python backend/scripts/stage_saaras_v4_catalog.py
    env/bin/python backend/scripts/stage_saaras_v4_catalog.py --apply --status active
    env/bin/python backend/scripts/stage_saaras_v4_catalog.py --apply --status inactive

``--status inactive`` is the right choice for a server whose voice runtime
cannot construct saaras:v4 yet (Pipecat ≤ 1.6 raises "Unsupported model"):
an inactive row is invisible to the Voice tab and rejected by the API, so no
bot can be switched to a model that would break its calls. The migration (or
``--status active`` later) exposes it once the code is deployed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

from sqlalchemy import create_engine, text  # noqa: E402

from shared.config import get_settings  # noqa: E402

MODEL = "saaras:v4"
DISPLAY = "Saaras v4 (streaming)"
DESCRIPTION = (
    "Sarvam Saaras v4 realtime STT: v3 coverage plus global English, better "
    "noise/accent robustness, and key-term biasing (up to 50 "
    "names/brands/terms). Not the default — select it per bot."
)
V3_DESCRIPTION = (
    "Sarvam Saaras v3 realtime STT: 22 Indic languages + Indian English, "
    "auto-detect, output modes (transcribe/verbatim/translit/codemix/"
    "translate). Platform default."
)
PRICE_INR_PER_HOUR = "30"

LANGS = [
    "unknown", "hi-IN", "en-IN", "bn-IN", "mr-IN", "gu-IN", "ta-IN", "te-IN",
    "kn-IN", "ml-IN", "pa-IN", "od-IN", "as-IN", "ur-IN", "ne-IN", "kok-IN",
    "ks-IN", "sd-IN", "sa-IN", "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN",
]

SCHEMA = {
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
    return datetime.now(timezone.utc).replace(tzinfo=None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the rows (default: dry-run)")
    parser.add_argument(
        "--status", choices=("active", "inactive"), default="active",
        help="status for a NEW model row (an existing row is never changed)",
    )
    args = parser.parse_args()

    engine = create_engine(get_settings().mysql_url)
    now = _utc_now()
    plan: list[tuple[str, dict]] = []

    with engine.connect() as conn:
        model_row = conn.execute(
            text(
                "SELECT id, status, is_default, description FROM provider_models "
                "WHERE provider_code = 'sarvam' AND capability = 'stt' AND code = :code"
            ),
            {"code": MODEL},
        ).first()
        if model_row is None:
            plan.append((
                "INSERT INTO provider_models (id, provider_code, capability, code, "
                "display_name, description, languages, codecs, sample_rates, streaming, "
                "params_schema, is_default, status, sort_order, created_at, updated_at, "
                "is_deleted) VALUES (:id, 'sarvam', 'stt', :code, :display_name, "
                ":description, :languages, :codecs, :sample_rates, 1, :params_schema, 0, "
                ":status, 1, :now, :now, 0)",
                {
                    "id": f"pm_{uuid.uuid4().hex[:20]}", "code": MODEL,
                    "display_name": DISPLAY, "description": DESCRIPTION,
                    "languages": json.dumps(LANGS), "codecs": json.dumps(["linear16"]),
                    "sample_rates": json.dumps([8000, 16000]),
                    "params_schema": json.dumps(SCHEMA), "status": args.status, "now": now,
                },
            ))
        else:
            print(f"provider_models sarvam/stt/{MODEL} exists: id={model_row[0]} "
                  f"status={model_row[1]} is_default={model_row[2]} — left unchanged")

        v3 = conn.execute(
            text(
                "SELECT id, description FROM provider_models WHERE provider_code = 'sarvam' "
                "AND capability = 'stt' AND code = 'saaras:v3'"
            )
        ).first()
        if v3 is not None and not (v3[1] or "").strip():
            plan.append((
                "UPDATE provider_models SET description = :description WHERE id = :id "
                "AND (description IS NULL OR description = '')",
                {"description": V3_DESCRIPTION, "id": v3[0]},
            ))

        inr = conn.execute(text("SELECT code FROM currencies WHERE code = 'INR'")).first()
        price_row = conn.execute(
            text(
                "SELECT id, unit, unit_price, status FROM provider_pricing WHERE provider_code = "
                "'sarvam' AND capability = 'stt' AND model_code = :model AND component = "
                "'audio_seconds' AND is_deleted = 0"
            ),
            {"model": MODEL},
        ).first()
        if inr is None:
            print("currencies has no INR row — skipping the pricing row (seed writes it)")
        elif price_row is None:
            plan.append((
                "INSERT INTO provider_pricing (id, provider_code, capability, model_code, "
                "component, unit, unit_price, currency_code, effective_from, status, "
                "sort_order, created_at, updated_at, is_deleted) VALUES (:id, 'sarvam', "
                "'stt', :model, 'audio_seconds', 'per_hour', :price, 'INR', :now, 'active', "
                "0, :now, :now, 0)",
                {"id": f"ppr_{uuid.uuid4().hex[:12]}", "model": MODEL,
                 "price": PRICE_INR_PER_HOUR, "now": now},
            ))
        else:
            print(f"provider_pricing sarvam/stt/{MODEL} exists: id={price_row[0]} "
                  f"{price_row[2]} {price_row[1]} status={price_row[3]} — left unchanged")

        if not plan:
            print("nothing to do")
            return 0
        for sql, params in plan:
            shown = {k: (v if len(str(v)) < 120 else str(v)[:117] + "...") for k, v in params.items()}
            print(("APPLY " if args.apply else "DRY-RUN ") + sql)
            print("       params:", json.dumps(shown, ensure_ascii=False, default=str))
        if not args.apply:
            print("dry-run only — re-run with --apply to write")
            return 0
        # SQLAlchemy 2.0 autobegan a transaction on the first SELECT above;
        # the writes join it and one commit publishes them atomically.
        for sql, params in plan:
            conn.execute(text(sql), params)
        conn.commit()
        print(f"applied {len(plan)} statement(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
