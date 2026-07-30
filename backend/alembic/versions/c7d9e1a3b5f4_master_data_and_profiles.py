"""Master data (industries, data regions, AI profiles, providers), plan limits,
tenant profile, user profile + password rotation, language metadata, voice
profile tuning, structured prompts, intent/entity/API-connection extensions.

Revision ID: c7d9e1a3b5f4
Revises: b2e4f6a8c0d2
Create Date: 2026-07-17

Additive only — no table drops, no data rewrites. Rollback: `alembic downgrade
b2e4f6a8c0d2` removes the new tables/columns without touching existing data.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7d9e1a3b5f4"
down_revision: Union[str, None] = "b2e4f6a8c0d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ID_LEN = 40


def _audit_columns():
    return [
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("created_by", sa.String(ID_LEN), nullable=True),
        sa.Column("updated_by", sa.String(ID_LEN), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(ID_LEN), nullable=True),
    ]


# (table, column-name, column) — every additive column in one place.
_NEW_COLUMNS: list[tuple[str, sa.Column]] = [
    # tenants — profile + master-data references
    ("tenants", sa.Column("code", sa.String(50), nullable=True)),
    ("tenants", sa.Column("ai_profile_code", sa.String(50), nullable=True)),
    ("tenants", sa.Column("website", sa.String(300), nullable=True)),
    ("tenants", sa.Column("contact_name", sa.String(150), nullable=True)),
    ("tenants", sa.Column("contact_email", sa.String(255), nullable=True)),
    ("tenants", sa.Column("contact_phone", sa.String(30), nullable=True)),
    ("tenants", sa.Column("address", sa.String(500), nullable=True)),
    ("tenants", sa.Column("country", sa.String(100), nullable=True)),
    # plans — full limit set + lifecycle
    ("plans", sa.Column("description", sa.Text, nullable=True)),
    ("plans", sa.Column("price_annual", sa.Numeric(10, 2), nullable=False, server_default="0")),
    ("plans", sa.Column("currency", sa.String(3), nullable=False, server_default="USD")),
    ("plans", sa.Column("kb_limit", sa.Integer, nullable=False, server_default="5")),
    ("plans", sa.Column("storage_gb_included", sa.Integer, nullable=False, server_default="5")),
    ("plans", sa.Column("languages_included", sa.Integer, nullable=False, server_default="2")),
    ("plans", sa.Column("concurrent_call_limit", sa.Integer, nullable=False, server_default="10")),
    ("plans", sa.Column("monthly_call_limit", sa.Integer, nullable=False, server_default="0")),
    ("plans", sa.Column("monthly_token_limit", sa.Integer, nullable=False, server_default="0")),
    ("plans", sa.Column("monthly_embedding_limit", sa.Integer, nullable=False, server_default="0")),
    ("plans", sa.Column("recording_retention_days", sa.Integer, nullable=False, server_default="90")),
    ("plans", sa.Column("transcript_retention_days", sa.Integer, nullable=False, server_default="90")),
    ("plans", sa.Column("analytics_retention_days", sa.Integer, nullable=False, server_default="365")),
    ("plans", sa.Column("overage_rates", sa.JSON, nullable=True)),
    ("plans", sa.Column("is_public", sa.Boolean, nullable=False, server_default=sa.text("1"))),
    ("plans", sa.Column("is_recommended", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("plans", sa.Column("sort_order", sa.Integer, nullable=False, server_default="0")),
    ("plans", sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("plans", sa.Column("deleted_at", sa.DateTime, nullable=True)),
    ("plans", sa.Column("deleted_by", sa.String(ID_LEN), nullable=True)),
    # users — profile + password rotation
    ("users", sa.Column("first_name", sa.String(80), nullable=True)),
    ("users", sa.Column("last_name", sa.String(80), nullable=True)),
    ("users", sa.Column("phone", sa.String(30), nullable=True)),
    ("users", sa.Column("avatar_url", sa.String(500), nullable=True)),
    ("users", sa.Column("locale", sa.String(15), nullable=True)),
    ("users", sa.Column("timezone", sa.String(64), nullable=True)),
    ("users", sa.Column("password_changed_at", sa.DateTime, nullable=True)),
    # supported_languages — locale metadata + provider support
    ("supported_languages", sa.Column("iso_code", sa.String(8), nullable=True)),
    ("supported_languages", sa.Column("script", sa.String(50), nullable=True)),
    ("supported_languages", sa.Column("direction", sa.String(3), nullable=False, server_default="ltr")),
    ("supported_languages", sa.Column("provider_support", sa.JSON, nullable=True)),
    ("supported_languages", sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("supported_languages", sa.Column("created_by", sa.String(ID_LEN), nullable=True)),
    ("supported_languages", sa.Column("updated_by", sa.String(ID_LEN), nullable=True)),
    # voice_profiles — tuning + provider linkage
    ("voice_profiles", sa.Column("locale", sa.String(15), nullable=True)),
    ("voice_profiles", sa.Column("description", sa.Text, nullable=True)),
    ("voice_profiles", sa.Column("provider_voice_id", sa.String(100), nullable=True)),
    ("voice_profiles", sa.Column("speaking_rate", sa.Float, nullable=False, server_default="1")),
    ("voice_profiles", sa.Column("pitch", sa.Float, nullable=False, server_default="1")),
    ("voice_profiles", sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("voice_profiles", sa.Column("sort_order", sa.Integer, nullable=False, server_default="0")),
    # prompts — approval / publish lifecycle
    ("prompts", sa.Column("description", sa.String(500), nullable=True)),
    ("prompts", sa.Column("published_version", sa.Integer, nullable=True)),
    ("prompts", sa.Column("approved_by", sa.String(150), nullable=True)),
    ("prompts", sa.Column("approved_at", sa.DateTime, nullable=True)),
    ("prompts", sa.Column("published_at", sa.DateTime, nullable=True)),
    # prompt_versions — structured config + compiled prompt
    ("prompt_versions", sa.Column("structured_config", sa.JSON, nullable=True)),
    ("prompt_versions", sa.Column("compiled_prompt", sa.Text, nullable=True)),
    ("prompt_versions", sa.Column("model_compatibility", sa.JSON, nullable=True)),
    # intents — routing associations
    ("intents", sa.Column("code", sa.String(80), nullable=True)),
    ("intents", sa.Column("category", sa.String(80), nullable=True)),
    ("intents", sa.Column("languages", sa.JSON, nullable=True)),
    ("intents", sa.Column("optional_entities", sa.JSON, nullable=True)),
    ("intents", sa.Column("workflow_id", sa.String(ID_LEN), nullable=True)),
    ("intents", sa.Column("api_connection_id", sa.String(ID_LEN), nullable=True)),
    ("intents", sa.Column("kb_ids", sa.JSON, nullable=True)),
    ("intents", sa.Column("priority", sa.Integer, nullable=False, server_default="100")),
    ("intents", sa.Column("fallback_behavior", sa.String(30), nullable=True)),
    ("intents", sa.Column("handoff_enabled", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    # entity_defs — full definition
    ("entity_defs", sa.Column("code", sa.String(80), nullable=True)),
    ("entity_defs", sa.Column("description", sa.String(500), nullable=True)),
    ("entity_defs", sa.Column("data_type", sa.String(30), nullable=False, server_default="text")),
    ("entity_defs", sa.Column("languages", sa.JSON, nullable=True)),
    ("entity_defs", sa.Column("synonyms", sa.JSON, nullable=True)),
    ("entity_defs", sa.Column("allowed_values", sa.JSON, nullable=True)),
    ("entity_defs", sa.Column("regex_pattern", sa.String(500), nullable=True)),
    ("entity_defs", sa.Column("validation_rules", sa.JSON, nullable=True)),
    ("entity_defs", sa.Column("normalization_rules", sa.JSON, nullable=True)),
    ("entity_defs", sa.Column("masking_enabled", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("entity_defs", sa.Column("require_confirmation", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("entity_defs", sa.Column("retention_days", sa.Integer, nullable=True)),
    ("entity_defs", sa.Column("status", sa.String(20), nullable=False, server_default="active")),
    # api_connections — request builder
    ("api_connections", sa.Column("description", sa.String(500), nullable=True)),
    ("api_connections", sa.Column("headers", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("query_params", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("path_params", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("body_template", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("request_schema", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("response_schema", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("success_condition", sa.String(200), nullable=True)),
    ("api_connections", sa.Column("success_message", sa.String(500), nullable=True)),
    ("api_connections", sa.Column("failure_message", sa.String(500), nullable=True)),
    ("api_connections", sa.Column("error_mapping", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("sensitive_masks", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("allowed_intents", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("allowed_workflows", sa.JSON, nullable=True)),
    ("api_connections", sa.Column("is_state_changing", sa.Boolean, nullable=False, server_default=sa.text("0"))),
    ("api_connections", sa.Column("require_confirmation", sa.Boolean, nullable=False, server_default=sa.text("0"))),
]


def upgrade() -> None:
    op.create_table(
        "industries",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("icon", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("default_prompt_template_id", sa.String(ID_LEN), nullable=True),
        sa.Column("default_guardrail_profile_id", sa.String(ID_LEN), nullable=True),
        sa.Column("default_workflow_template_id", sa.String(ID_LEN), nullable=True),
        *_audit_columns(),
    )
    op.create_index("ix_industries_status", "industries", ["status"])

    op.create_table(
        "data_regions",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("country", sa.String(100), nullable=True),
        sa.Column("region", sa.String(100), nullable=True),
        sa.Column("cloud_provider", sa.String(100), nullable=True),
        sa.Column("storage_region", sa.String(100), nullable=True),
        sa.Column("database_region", sa.String(100), nullable=True),
        sa.Column("recording_region", sa.String(100), nullable=True),
        sa.Column("transcript_region", sa.String(100), nullable=True),
        sa.Column("infrastructure_ready", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        *_audit_columns(),
    )
    op.create_index("ix_data_regions_status", "data_regions", ["status"])

    op.create_table(
        "ai_config_profiles",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("stt_provider", sa.String(40), nullable=True),
        sa.Column("stt_model", sa.String(80), nullable=True),
        sa.Column("llm_provider", sa.String(40), nullable=True),
        sa.Column("llm_model", sa.String(80), nullable=True),
        sa.Column("tts_provider", sa.String(40), nullable=True),
        sa.Column("tts_model", sa.String(80), nullable=True),
        sa.Column("default_voice", sa.String(80), nullable=True),
        sa.Column("embedding_provider", sa.String(40), nullable=True),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("embedding_dimension", sa.Integer, nullable=True),
        sa.Column("reranking_model", sa.String(80), nullable=True),
        sa.Column("retrieval_top_k", sa.Integer, nullable=False, server_default="6"),
        sa.Column("retrieval_threshold", sa.Float, nullable=False, server_default="0.35"),
        sa.Column("temperature", sa.Float, nullable=False, server_default="0.4"),
        sa.Column("max_output_tokens", sa.Integer, nullable=False, server_default="600"),
        sa.Column("response_timeout_ms", sa.Integer, nullable=False, server_default="8000"),
        sa.Column("fallback_providers", sa.JSON, nullable=True),
        sa.Column("cost_category", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        *_audit_columns(),
    )
    op.create_index("ix_ai_config_profiles_status", "ai_config_profiles", ["status"])

    op.create_table(
        "provider_defs",
        sa.Column("id", sa.String(ID_LEN), primary_key=True),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("website", sa.String(300), nullable=True),
        sa.Column("requires_api_key", sa.Boolean, nullable=False, server_default=sa.text("1")),
        sa.Column("secret_ref", sa.String(300), nullable=True),
        sa.Column("config", sa.JSON, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        *_audit_columns(),
        sa.UniqueConstraint("kind", "code", name="uq_provider_kind_code"),
    )
    op.create_index("ix_provider_defs_kind", "provider_defs", ["kind"])
    op.create_index("ix_provider_defs_status", "provider_defs", ["status"])

    for table, column in _NEW_COLUMNS:
        op.add_column(table, column)

    op.create_unique_constraint("uq_tenants_code", "tenants", ["code"])


def downgrade() -> None:
    op.drop_constraint("uq_tenants_code", "tenants", type_="unique")
    for table, column in reversed(_NEW_COLUMNS):
        op.drop_column(table, column.name)
    op.drop_table("provider_defs")
    op.drop_table("ai_config_profiles")
    op.drop_table("data_regions")
    op.drop_table("industries")
