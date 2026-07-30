"""Platform Configuration: filter/sort indexes + catalog data hygiene.

Revision ID: a3c5e7f9b1d3
Revises: f0a1b2c3d4e5
Create Date: 2026-07-22

Additive + data hygiene only (no schema-shape changes):
- Indexes for the fields the Platform Configuration lists filter and sort on:
  voice_profiles(provider / status / sort_order), supported_languages(sort_order),
  plans(status / sort_order), ai_config_profiles(sort_order).
- Data fixes:
  - ai_config_profiles / voice_bot_settings: legacy LLM model alias
    'mock-1' → 'mock' so every stored provider+model pair exists in the
    provider_models catalog (strict validation now enforces membership).
  - plans.currency normalized to uppercase ISO codes.
  - Defensive clamp of negative limit/sort values to 0 (none exist today;
    the API now rejects new negatives).

Rollback: `alembic downgrade f0a1b2c3d4e5` (drops the indexes only — the data
fixes are intentionally not reverted).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3c5e7f9b1d3"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = [
    ("ix_voice_profiles_provider", "voice_profiles", ["provider"]),
    ("ix_voice_profiles_status", "voice_profiles", ["status"]),
    ("ix_voice_profiles_sort_order", "voice_profiles", ["sort_order"]),
    ("ix_supported_languages_sort_order", "supported_languages", ["sort_order"]),
    ("ix_plans_status", "plans", ["status"]),
    ("ix_plans_sort_order", "plans", ["sort_order"]),
    ("ix_ai_config_profiles_sort_order", "ai_config_profiles", ["sort_order"]),
]

_PLAN_NON_NEGATIVE = [
    "price_monthly", "price_annual", "bot_limit", "minutes_included",
    "seats_included", "kb_limit", "storage_gb_included", "languages_included",
    "concurrent_call_limit", "monthly_call_limit", "monthly_token_limit",
    "monthly_embedding_limit", "recording_retention_days",
    "transcript_retention_days", "analytics_retention_days", "sort_order",
]


def _existing_indexes(table: str) -> set[str]:
    bind = op.get_bind()
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        if name not in _existing_indexes(table):
            op.create_index(name, table, cols)

    # Legacy alias: profiles/bots referenced 'mock-1', catalog model is 'mock'.
    op.execute("UPDATE ai_config_profiles SET llm_model = 'mock' WHERE llm_model = 'mock-1'")
    op.execute("UPDATE voice_bot_settings SET llm_model = 'mock' WHERE llm_model = 'mock-1'")

    op.execute("UPDATE plans SET currency = UPPER(currency)")

    for col in _PLAN_NON_NEGATIVE:
        op.execute(f"UPDATE plans SET {col} = 0 WHERE {col} < 0")
    op.execute("UPDATE voice_profiles SET sort_order = 0 WHERE sort_order < 0")
    op.execute("UPDATE voice_profiles SET latency_ms = 0 WHERE latency_ms < 0")
    op.execute("UPDATE supported_languages SET sort_order = 0 WHERE sort_order < 0")


def downgrade() -> None:
    for name, table, _cols in _INDEXES:
        if name in _existing_indexes(table):
            op.drop_index(name, table_name=table)
