"""Effective call disposition for the conversation list, detail and exports.

Two sources exist today:

* ``conversation_sessions.disposition`` — written by the voice runtime for
  bots that run a conversation policy (collections), the silence ladder
  (``no_response``) and the DNC fast path. Workflow-driven bots never fill it.
* the post-call structured summary — ``goal_policy.summaryFields`` lets a bot
  declare a field named ``disposition`` whose value is derived from the
  workflow's final slots (Haier: the client's official nine dispositions).
  It is persisted in ``conversation_memories.memory.structured_fields``.

The stored session value wins when present (it is the runtime's own verdict);
otherwise the structured summary's ``disposition`` field fills the same
column, so every surface that reads a disposition shows one source of truth.
Bots that declare no such field are untouched: the lookup returns nothing for
them and the column stays as it was.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import ConversationMemory

# The structured summary field name a bot uses for its official disposition.
STRUCTURED_DISPOSITION_FIELD = "disposition"
# Memory rows whose structured fields are final (a queued/processing row is
# not a verdict yet; a failed row keeps the deterministic workflow fallback).
_FINAL_STATUSES = ("completed", "failed")
_CHUNK = 500


def structured_disposition(memory: dict | None) -> str | None:
    """The ``disposition`` structured field of one persisted memory JSON."""
    if not isinstance(memory, dict):
        return None
    fields = memory.get("structured_fields")
    if not isinstance(fields, dict):
        return None
    value = fields.get(STRUCTURED_DISPOSITION_FIELD)
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def structured_dispositions(db: Session, conversation_ids: Iterable[str]) -> dict[str, str]:
    """conversation_id → structured-summary disposition, for the ids that have one.

    One query per 500 ids; a page of the conversations list or an export of a
    few thousand rows costs a handful of indexed lookups on the unique
    ``conversation_memories.conversation_id``.
    """
    ids = [cid for cid in dict.fromkeys(conversation_ids) if cid]
    out: dict[str, str] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        rows = db.execute(
            select(ConversationMemory.conversation_id, ConversationMemory.memory).where(
                ConversationMemory.conversation_id.in_(chunk),
                ConversationMemory.status.in_(_FINAL_STATUSES),
            )
        ).all()
        for conversation_id, memory in rows:
            value = structured_disposition(memory)
            if value:
                out[conversation_id] = value
    return out


def effective_disposition(stored: str | None, structured: str | None) -> str | None:
    """The runtime's stored disposition when present, else the structured one."""
    stored_text = " ".join(str(stored or "").split())
    return stored_text or structured or None


__all__ = [
    "STRUCTURED_DISPOSITION_FIELD",
    "effective_disposition",
    "structured_disposition",
    "structured_dispositions",
]
