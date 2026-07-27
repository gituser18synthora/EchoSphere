"""Tenant-wise usage metering, provider pricing and currency exchange.

Revision ID: a9b1c3d5e7f9
Revises: f8c0d2e4a6b8
Create Date: 2026-07-24

Additive only:
- currencies: ISO 4217 catalog (USD base) managed under Platform Configuration.
- exchange_rates: manual base->target rates with effective dating; history is
  preserved by superseding rows, never editing applied ones.
- provider_pricing: DB-driven unit prices per provider/model/component
  (input/output tokens, characters, seconds, minutes, requests).
- usage_events: raw tenant-attributed billable operations with a frozen
  pricing snapshot and USD cost; unique request_id makes recording idempotent.
- usage_records.cost_embedding: embedding spend joins the daily rollup.

Rollback: `alembic downgrade f8c0d2e4a6b8`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b1c3d5e7f9"
down_revision: Union[str, None] = "f8c0d2e4a6b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table)


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
        ),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("updated_by", sa.String(40), nullable=True),
        sa.Column("is_deleted", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("deleted_by", sa.String(40), nullable=True),
    ]


def upgrade() -> None:
    if not _has_table("currencies"):
        op.create_table(
            "currencies",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column("code", sa.String(3), nullable=False, unique=True),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("symbol", sa.String(8), nullable=False),
            sa.Column("decimal_places", sa.Integer, nullable=False, server_default=sa.text("2")),
            sa.Column("is_base", sa.Boolean, nullable=False, server_default=sa.text("0")),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default=sa.text("0")),
            *_audit_columns(),
        )
        op.create_index("ix_currencies_status", "currencies", ["status"])

    if not _has_table("exchange_rates"):
        op.create_table(
            "exchange_rates",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column(
                "base_code",
                sa.String(3),
                sa.ForeignKey("currencies.code", name="fk_fx_base_currency", onupdate="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "target_code",
                sa.String(3),
                sa.ForeignKey("currencies.code", name="fk_fx_target_currency", onupdate="CASCADE"),
                nullable=False,
            ),
            sa.Column("rate", sa.Numeric(18, 8), nullable=False),
            sa.Column("effective_from", sa.DateTime, nullable=False, server_default=sa.func.now()),
            sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default=sa.text("0")),
            *_audit_columns(),
            sa.UniqueConstraint(
                "base_code", "target_code", "effective_from", name="uq_fx_pair_effective"
            ),
        )
        op.create_index("ix_exchange_rates_status", "exchange_rates", ["status"])
        op.create_index(
            "ix_fx_pair_status", "exchange_rates", ["base_code", "target_code", "status"]
        )

    if not _has_table("provider_pricing"):
        op.create_table(
            "provider_pricing",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column("provider_code", sa.String(50), nullable=False),
            sa.Column("capability", sa.String(20), nullable=False),
            sa.Column("model_code", sa.String(80), nullable=False),
            sa.Column("component", sa.String(30), nullable=False),
            sa.Column("unit", sa.String(20), nullable=False),
            sa.Column("unit_price", sa.Numeric(18, 10), nullable=False),
            sa.Column(
                "currency_code",
                sa.String(3),
                sa.ForeignKey("currencies.code", name="fk_pricing_currency", onupdate="CASCADE"),
                nullable=False,
                server_default="USD",
            ),
            sa.Column("effective_from", sa.DateTime, nullable=False, server_default=sa.func.now()),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default=sa.text("0")),
            *_audit_columns(),
            sa.UniqueConstraint(
                "provider_code",
                "capability",
                "model_code",
                "component",
                "effective_from",
                name="uq_pricing_key_effective",
            ),
        )
        op.create_index("ix_provider_pricing_status", "provider_pricing", ["status"])
        op.create_index(
            "ix_pricing_lookup",
            "provider_pricing",
            ["provider_code", "capability", "model_code", "status"],
        )

    if not _has_table("usage_events"):
        op.create_table(
            "usage_events",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column(
                "tenant_id", sa.String(40), sa.ForeignKey("tenants.id"), nullable=False
            ),
            sa.Column(
                "bot_id", sa.String(40), sa.ForeignKey("voice_bots.id"), nullable=True
            ),
            sa.Column("session_id", sa.String(40), nullable=True),
            sa.Column("capability", sa.String(20), nullable=False),
            sa.Column("provider_code", sa.String(50), nullable=False),
            sa.Column("model_code", sa.String(80), nullable=True),
            sa.Column("voice_code", sa.String(80), nullable=True),
            sa.Column("request_id", sa.String(120), nullable=True),
            sa.Column("occurred_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
            sa.Column("requests", sa.Integer, nullable=False, server_default=sa.text("1")),
            sa.Column("input_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("output_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("cached_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("reasoning_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("total_tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column("characters", sa.Integer, nullable=False, server_default=sa.text("0")),
            sa.Column(
                "audio_seconds", sa.Numeric(12, 3), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("usage_source", sa.String(20), nullable=False, server_default="provider"),
            sa.Column("usage_metadata", sa.JSON, nullable=True),
            sa.Column("pricing_status", sa.String(20), nullable=False, server_default="priced"),
            sa.Column("pricing_snapshot", sa.JSON, nullable=True),
            sa.Column(
                "cost_usd", sa.Numeric(14, 6), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
            sa.Column(
                "updated_at",
                sa.DateTime,
                nullable=False,
                server_default=sa.func.now(),
                server_onupdate=sa.func.now(),
            ),
            sa.UniqueConstraint("request_id", name="uq_usage_event_request"),
        )
        op.create_index(
            "ix_usage_events_tenant_time", "usage_events", ["tenant_id", "occurred_at"]
        )
        op.create_index(
            "ix_usage_events_capability_time", "usage_events", ["capability", "occurred_at"]
        )
        op.create_index(
            "ix_usage_events_provider", "usage_events", ["provider_code", "model_code"]
        )

    if not _has_column("usage_records", "cost_embedding"):
        op.add_column(
            "usage_records",
            sa.Column(
                "cost_embedding", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")
            ),
        )


def downgrade() -> None:
    if _has_column("usage_records", "cost_embedding"):
        op.drop_column("usage_records", "cost_embedding")
    for table in ("usage_events", "provider_pricing", "exchange_rates", "currencies"):
        if _has_table(table):
            op.drop_table(table)
