"""STT/TTS provider costing: selling price and tenant charge columns.

Revision ID: c1d3e5f7a9b1
Revises: a9b1c3d5e7f9
Create Date: 2026-07-27

Additive only:
- provider_pricing.selling_price: optional platform selling price per unit
  (what the tenant is charged); NULL = provider cost only, no markup.
- usage_events.charge_usd: the tenant/platform charge frozen at recording
  time next to cost_usd, so selling-price changes never rewrite history.

New pricing units (per_1m_characters, per_hour) need no schema change —
`unit` is a plain varchar validated against PRICING_UNITS in code.

Rollback: `alembic downgrade a9b1c3d5e7f9`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d3e5f7a9b1"
down_revision: Union[str, None] = "a9b1c3d5e7f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    if not _has_column("provider_pricing", "selling_price"):
        op.add_column(
            "provider_pricing",
            sa.Column("selling_price", sa.Numeric(18, 10), nullable=True),
        )
    if not _has_column("usage_events", "charge_usd"):
        op.add_column(
            "usage_events",
            sa.Column(
                "charge_usd", sa.Numeric(14, 6), nullable=False, server_default=sa.text("0")
            ),
        )


def downgrade() -> None:
    if _has_column("usage_events", "charge_usd"):
        op.drop_column("usage_events", "charge_usd")
    if _has_column("provider_pricing", "selling_price"):
        op.drop_column("provider_pricing", "selling_price")
