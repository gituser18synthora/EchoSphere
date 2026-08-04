"""Backfill `conversation_sessions.session_id` and recompute stored costs.

Two repairs on existing rows, both derived from data the platform already has —
nothing is invented and nothing is deleted:

1. **session_id** — the link to `usage_events` was never stored on the control
   plane row, but the Mongo transcript document has always carried both
   `control_plane_id` and `session_id`. That mapping is copied across.

2. **cost_usd** — the cached total is recomputed as the sum of the
   conversation's usage events. Costs are NOT re-priced: each event's stored
   `pricing_snapshot` already records the rate that was applied at the time, so
   the recompute restates the total from history rather than from today's rate
   table.

Rows whose stored total already matches are left untouched. A conversation with
no usage events keeps its existing value: absent events, the stored number is
the only record there is, and zeroing it would destroy information.

Usage:
    env/bin/python -m backend.scripts.backfill_conversation_costs --dry-run
    env/bin/python -m backend.scripts.backfill_conversation_costs --apply
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from sqlalchemy import select

TOLERANCE = Decimal("0.000001")


async def _session_id_map() -> dict[str, str]:
    """control_plane_id -> voice session_id, from the transcript documents."""
    from shared.db.mongo import Mongo

    await Mongo.connect()
    mapping: dict[str, str] = {}
    cursor = Mongo.transcripts().find(
        {"control_plane_id": {"$exists": True}},
        {"control_plane_id": 1, "session_id": 1},
    )
    async for doc in cursor:
        cp, sid = doc.get("control_plane_id"), doc.get("session_id")
        if cp and sid:
            mapping[cp] = sid
    return mapping


def run(apply: bool) -> int:
    from shared.billing.conversation_cost import conversation_cost
    from shared.db.mysql import get_sessionmaker
    from shared.models import ConversationSession

    mapping = asyncio.run(_session_id_map())
    print(f"transcript documents with a session link: {len(mapping)}")

    session = get_sessionmaker()()
    linked = recosted = unchanged = no_events = 0
    try:
        rows = list(
            session.execute(
                select(ConversationSession).where(
                    ConversationSession.is_deleted.is_(False)
                )
            ).scalars()
        )
        print(f"conversations: {len(rows)}")
        for row in rows:
            session_id = row.session_id or mapping.get(row.id)
            if session_id and not row.session_id:
                row.session_id = session_id
                linked += 1
            if not session_id:
                no_events += 1
                continue

            costing = conversation_cost(session, session_id)
            if costing.event_count == 0:
                # Nothing to recompute from — keep whatever is stored.
                no_events += 1
                continue
            stored = Decimal(str(row.cost_usd))
            if abs(stored - costing.total_usd) <= TOLERANCE:
                unchanged += 1
                continue
            print(
                f"  {row.id}: {stored} -> {costing.total_usd} "
                f"({costing.event_count} events"
                + (f", unpriced: {', '.join(costing.unpriced)}" if costing.unpriced else "")
                + ")"
            )
            row.cost_usd = costing.total_usd
            recosted += 1

        print(
            f"\nlinked={linked} recosted={recosted} unchanged={unchanged} "
            f"no_events={no_events}"
        )
        if apply:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run — nothing written (pass --apply to commit)")
    finally:
        session.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true", help="report only")
    group.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
