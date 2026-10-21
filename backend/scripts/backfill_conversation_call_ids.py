"""Backfill `conversation_sessions.call_id` for historical telephony calls.

The telephony webhook's `callId` was never stored on the conversation row, but
the voice runtime has always written it on the call's `call_started` event in
Mongo (`voice_events`, keyed by the `vs_*` session id that the conversation row
also carries as `session_id`). That mapping is copied across — nothing is
invented, nothing is overwritten: rows that already have a `call_id` and rows
without a matching event are left untouched.

Usage:
    env/bin/python -m backend.scripts.backfill_conversation_call_ids --dry-run
    env/bin/python -m backend.scripts.backfill_conversation_call_ids --apply [--tenant tn_…]
"""

from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select


async def _call_id_map(tenant_id: str | None) -> dict[str, str]:
    """voice session_id -> telephony call id, from the call_started events."""
    from shared.db.mongo import Mongo

    await Mongo.connect()
    try:
        query: dict = {"kind": "call_started", "data.call_id": {"$type": "string", "$ne": ""}}
        if tenant_id:
            query["tenant_id"] = tenant_id
        out: dict[str, str] = {}
        cursor = Mongo.voice_events().find(query, {"session_id": 1, "data.call_id": 1})
        async for doc in cursor:
            session_id = doc.get("session_id")
            call_id = (doc.get("data") or {}).get("call_id")
            if session_id and call_id:
                out.setdefault(str(session_id), str(call_id)[:64])
        return out
    finally:
        await Mongo.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--tenant", default=None, help="limit to one tenant id")
    args = parser.parse_args()

    from shared.db.mysql import get_sessionmaker
    from shared.models import ConversationSession

    mapping = asyncio.run(_call_id_map(args.tenant))
    print(f"call_started events with a call id: {len(mapping)}")

    session = get_sessionmaker()()
    try:
        stmt = select(ConversationSession).where(
            ConversationSession.call_id.is_(None),
            ConversationSession.session_id.isnot(None),
            ConversationSession.is_deleted.is_(False),
        )
        if args.tenant:
            stmt = stmt.where(ConversationSession.tenant_id == args.tenant)
        candidates = session.scalars(stmt).all()
        matched = [(c, mapping[c.session_id]) for c in candidates if c.session_id in mapping]
        print(f"conversations without call_id: {len(candidates)}; with a matching event: {len(matched)}")
        for c, call_id in matched[:20]:
            print(f"  {c.id} ({c.tenant_id}) ← {call_id}")
        if len(matched) > 20:
            print(f"  … {len(matched) - 20} more")
        if not args.apply:
            print("dry run — nothing written")
            return
        for c, call_id in matched:
            c.call_id = call_id
        session.commit()
        print(f"updated {len(matched)} conversation(s)")
    finally:
        session.close()


if __name__ == "__main__":
    main()
