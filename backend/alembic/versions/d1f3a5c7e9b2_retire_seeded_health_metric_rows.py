"""Retire the seeded (never-updated) platform health rows.

Platform Health used to render whatever sat in ``health_metrics``: eight rows
written once by the bootstrap seed and never touched again, so the admin
dashboard reported "API gateway — 100% uptime" regardless of what was
actually running, under service names that no longer match any process.

The card is now probed live from the hosts/ports in ``.env``
(``backend/core/service_health.py``), and the seed no longer writes these
rows. This deletes the stale ones so nothing can read a stored status back.
The table and model are kept for historical snapshots; only the placeholder
rows introduced by the seed are removed, and only when they are still
untouched placeholders.

Revision ID: d1f3a5c7e9b2
Revises: c9e1a3b5d7f2
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "d1f3a5c7e9b2"
down_revision = "c9e1a3b5d7f2"
branch_labels = None
depends_on = None

# The exact placeholder set the seed used to write (name, status, value, target).
_SEEDED_ROWS = (
    ("API gateway", "good", "100% uptime", "≥99.95%"),
    ("Call orchestration", "good", "—", "<250ms"),
    ("SIP trunks", "neutral", "—", "<0.5%"),
    ("STT latency", "neutral", "—", "<400ms"),
    ("LLM latency", "neutral", "—", "<800ms"),
    ("TTS latency", "neutral", "—", "<300ms"),
    ("Embedding queue", "neutral", "—", "<5 min"),
    ("Recording storage", "neutral", "—", "<80%"),
)


def upgrade() -> None:
    bind = op.get_bind()
    # Matching on the full placeholder tuple keeps any row an operator or a
    # future collector has actually written.
    for name, status, value, target in _SEEDED_ROWS:
        bind.execute(
            sa.text(
                "DELETE FROM health_metrics WHERE name = :name AND status = :status "
                "AND value = :value AND target = :target"
            ),
            {"name": name, "status": status, "value": value, "target": target},
        )


def downgrade() -> None:
    import uuid

    bind = op.get_bind()
    for order, (name, status, value, target) in enumerate(_SEEDED_ROWS):
        exists = bind.execute(
            sa.text("SELECT id FROM health_metrics WHERE name = :name"),
            {"name": name},
        ).first()
        if exists is not None:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO health_metrics (id, name, status, value, target, "
                "spark, sort_order) VALUES (:id, :name, :status, :value, "
                ":target, :spark, :sort_order)"
            ),
            {"id": f"hm_{uuid.uuid4().hex[:12]}", "name": name, "status": status,
             "value": value, "target": target, "spark": "[]", "sort_order": order},
        )
