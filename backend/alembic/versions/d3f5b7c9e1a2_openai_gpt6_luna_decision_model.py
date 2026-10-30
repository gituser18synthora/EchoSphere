"""OpenAI gpt-6-luna in the LLM catalog (Goal Engine decision default).

Revision ID: d3f5b7c9e1a2
Revises: c2e4a6b8d0f1
Create Date: 2026-10-23

1. Inserts the ``gpt-6-luna`` OpenAI LLM model when missing (active, not
   default). It is the platform's default Goal Engine decision model
   (shared.config ``orchestration_model``, on the fast service tier); a bot
   that names another ``orchestration_model`` keeps it. Without this row
   governance refuses gpt-6-luna and the bot's conversation model decides.
2. Inserts its standard-tier prices when missing (USD per 1M tokens: input
   0.10, cached input 0.01, output 0.50 — developers.openai.com pricing,
   2026-10-23). OpenAI bills the fast tier at 2x; that premium is not
   modelled in provider_pricing.

Rollback removes the inserted row and prices. Bots that pinned gpt-6-luna as
their orchestration or conversation model fail validation afterwards.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5b7c9e1a2"
down_revision: Union[str, None] = "c2e4a6b8d0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MODEL = "gpt-6-luna"
_DESCRIPTION = (
    "Default Goal Engine decision model, run on OpenAI's fast tier with "
    "reasoning effort none (2026-10-23 bench: routing 69 % vs 54 % for "
    "gpt-4o-mini, harmful mislabels 5 vs 36, same ~1.3 s latency). "
    "OpenAI's replacement for gpt-4o-mini / gpt-4.1 / gpt-4.1-mini "
    "(shutdown 2027-01-14). As a reply model it runs at its default "
    "reasoning effort — measure latency before choosing it for replies."
)
_SCHEMA = {
    "temperature": {
        "type": "number", "min": 0.0, "max": 2.0, "default": 0.3, "step": 0.05,
        "label": "Temperature", "help": "Response randomness. Keep low for voice bots.",
    },
    "max_tokens": {
        "type": "integer", "min": 16, "max": 4096, "default": 256,
        "label": "Max output tokens",
        "help": "Upper bound per reply. Voice replies should stay short.",
    },
    "timeout_seconds": {
        "type": "number", "min": 5, "max": 120, "default": 30, "step": 1,
        "label": "Timeout (s)", "advanced": True,
        "help": "Per-request timeout before retry/fallback handling.",
    },
    "streaming": {
        "type": "boolean", "default": True, "fixed": True, "label": "Streaming",
        "help": "Token streaming into the sentence buffer. Always on for realtime voice.",
    },
    "max_retries": {
        "type": "integer", "min": 0, "max": 5, "default": 1,
        "label": "Max retries", "advanced": True,
        "help": "Automatic retries on transient failures.",
    },
}
_PRICES = (("input_tokens", "0.10"), ("cached_input_tokens", "0.01"), ("output_tokens", "0.50"))


def _usd_exists(bind) -> bool:
    return bind.execute(
        sa.text("SELECT code FROM currencies WHERE code = 'USD'")
    ).first() is not None


def upgrade() -> None:
    bind = op.get_bind()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    exists = bind.execute(
        sa.text(
            "SELECT id FROM provider_models WHERE provider_code = 'openai' "
            "AND capability = 'llm' AND code = :code"
        ),
        {"code": _MODEL},
    ).first()
    if exists is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_models (id, provider_code, capability, "
                "code, display_name, description, languages, codecs, "
                "sample_rates, streaming, params_schema, is_default, status, "
                "sort_order, is_deleted) VALUES (:id, 'openai', 'llm', :code, "
                ":display_name, :description, :languages, NULL, NULL, "
                ":streaming, :params_schema, :is_default, 'active', :sort_order, 0)"
            ),
            {
                "id": f"pm_{uuid.uuid4().hex[:20]}",
                "code": _MODEL,
                "display_name": "GPT-6 Luna",
                "description": _DESCRIPTION,
                "languages": json.dumps([]),
                "streaming": True,
                "params_schema": json.dumps(_SCHEMA),
                "is_default": False,
                "sort_order": 5,
            },
        )

    if not _usd_exists(bind):
        return  # fresh database — the seed writes the prices after currencies
    for component, price in _PRICES:
        found = bind.execute(
            sa.text(
                "SELECT id FROM provider_pricing WHERE provider_code = 'openai' "
                "AND capability = 'llm' AND model_code = :code "
                "AND component = :component AND is_deleted = 0"
            ),
            {"code": _MODEL, "component": component},
        ).first()
        if found is None:
            bind.execute(
                sa.text(
                    "INSERT INTO provider_pricing (id, provider_code, capability, "
                    "model_code, component, unit, unit_price, currency_code, "
                    "effective_from, status, sort_order, created_at, updated_at, "
                    "is_deleted) VALUES (:id, 'openai', 'llm', :code, :component, "
                    "'per_1m_tokens', :price, 'USD', :now, 'active', 0, :now, :now, 0)"
                ),
                {"id": f"ppr_{uuid.uuid4().hex[:12]}", "code": _MODEL,
                 "component": component, "price": price, "now": now},
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM provider_pricing WHERE provider_code = 'openai' "
        "AND capability = 'llm' AND model_code = :code"
    ), {"code": _MODEL})
    bind.execute(sa.text(
        "DELETE FROM provider_models WHERE provider_code = 'openai' "
        "AND capability = 'llm' AND code = :code"
    ), {"code": _MODEL})
