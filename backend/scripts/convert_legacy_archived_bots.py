"""Convert bots archived by the pre-September-2026 implementation to the
recoverable Archive state.

Before Archive and Delete were separate actions, the only lifecycle action on
the Bots page was "Archive", and it ran the soft delete: ``status='archived'``
AND ``is_deleted=1``. Under the current model that combination means
"deleted", so those bots would stay invisible and unrestorable.

This script finds legacy rows and converts the ones with positive evidence of
an archive to ``is_deleted=0`` (status stays ``archived``). Evidence gate — ALL
of these must hold, otherwise the row is left exactly as it is:

* the bot has an ``Archived VoiceBot`` audit row (the old action's trail);
* it has no ``Deleted VoiceBot`` audit row (written only by the new Delete);
* its tenant is not deleted.

For a converted bot, channel rows soft-deleted by the same legacy action
(``deleted_at`` within a few seconds of the bot's) are revived as *deactivated*
channels — exactly what a new-style archive leaves behind. Phone numbers are
NOT touched: the legacy action returned them to the platform pool without
recording which bot held them, and a number may have been claimed since; the
tenant re-attaches a number from the Channels tab after restoring the bot.
``live_version`` is cleared because nothing is live for an archived bot; the
previous value is kept in the conversion's audit row.

Dry-run by default; nothing is written without ``--apply``.

Usage:
    env/bin/python -m backend.scripts.convert_legacy_archived_bots
    env/bin/python -m backend.scripts.convert_legacy_archived_bots --tenant tn_x --apply
    env/bin/python -m backend.scripts.convert_legacy_archived_bots --bot bot_a --bot bot_b --apply
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

LEGACY_ARCHIVE_ACTION = "Archived VoiceBot"
DELETE_ACTION = "Deleted VoiceBot"
CONVERSION_ACTION = "Converted legacy archived VoiceBot"
CHANNEL_WINDOW = timedelta(seconds=5)


@dataclass
class Candidate:
    bot_id: str
    tenant_id: str
    name: str
    deleted_at: object
    live_version: str | None
    convert: bool
    reason: str
    channel_ids: list[str] = field(default_factory=list)


def plan(
    session: Session,
    *,
    tenant_id: str | None = None,
    bot_ids: list[str] | None = None,
) -> list[Candidate]:
    """Classify every legacy-shaped bot (status archived + is_deleted) in scope."""
    from shared.models import AuditLog, ChannelConfig, Tenant, VoiceBot

    stmt = select(VoiceBot).where(
        VoiceBot.status == "archived", VoiceBot.is_deleted.is_(True)
    )
    if tenant_id:
        stmt = stmt.where(VoiceBot.tenant_id == tenant_id)
    if bot_ids:
        stmt = stmt.where(VoiceBot.id.in_(bot_ids))
    bots = session.scalars(stmt.order_by(VoiceBot.tenant_id, VoiceBot.name)).all()

    out: list[Candidate] = []
    for bot in bots:
        actions = set(session.scalars(
            select(AuditLog.action).where(
                AuditLog.entity_type == "voice_bot", AuditLog.entity_id == bot.id,
                AuditLog.action.in_((LEGACY_ARCHIVE_ACTION, DELETE_ACTION, CONVERSION_ACTION)),
            )
        ).all())
        tenant = session.get(Tenant, bot.tenant_id)
        cand = Candidate(
            bot_id=bot.id, tenant_id=bot.tenant_id, name=bot.name,
            deleted_at=bot.deleted_at, live_version=bot.live_version,
            convert=False, reason="",
        )
        if DELETE_ACTION in actions:
            cand.reason = "skip: has a 'Deleted VoiceBot' audit row (permanent delete)"
        elif LEGACY_ARCHIVE_ACTION not in actions:
            cand.reason = "skip: no 'Archived VoiceBot' audit evidence"
        elif tenant is None or tenant.is_deleted:
            cand.reason = "skip: tenant is deleted"
        else:
            cand.convert = True
            cand.reason = "convert: legacy archive with audit evidence"
            if bot.deleted_at is not None:
                lo, hi = bot.deleted_at - CHANNEL_WINDOW, bot.deleted_at + CHANNEL_WINDOW
                cand.channel_ids = list(session.scalars(
                    select(ChannelConfig.id).where(
                        ChannelConfig.bot_id == bot.id,
                        ChannelConfig.is_deleted.is_(True),
                        ChannelConfig.deleted_at.is_not(None),
                        ChannelConfig.deleted_at >= lo,
                        ChannelConfig.deleted_at <= hi,
                    )
                ).all())
        out.append(cand)
    return out


def apply(session: Session, candidates: list[Candidate]) -> int:
    """Convert the candidates flagged ``convert``; returns how many changed.
    The caller commits."""
    from backend.core.audit import record_audit
    from shared.models import ChannelConfig, VoiceBot

    changed = 0
    for cand in candidates:
        if not cand.convert:
            continue
        bot = session.get(VoiceBot, cand.bot_id)
        previous = {
            "isDeleted": True,
            "deletedAt": bot.deleted_at.isoformat() if bot.deleted_at else None,
            "deletedBy": bot.deleted_by,
            "liveVersion": bot.live_version,
        }
        bot.is_deleted = False
        bot.deleted_at = None
        bot.deleted_by = None
        bot.live_version = None
        # status stays "archived"
        for channel_id in cand.channel_ids:
            channel = session.get(ChannelConfig, channel_id)
            channel.is_deleted = False
            channel.deleted_at = None
            channel.deleted_by = None
            channel.enabled = False
            if channel.status == "archived":
                channel.status = "configured"
        record_audit(
            session, user=None, action=CONVERSION_ACTION, entity_type="voice_bot",
            entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id,
            previous_value=previous,
            new_value={"isDeleted": False, "status": "archived",
                       "channelsRevived": len(cand.channel_ids)},
        )
        changed += 1
    return changed


def run(*, tenant_id: str | None, bot_ids: list[str] | None, do_apply: bool) -> int:
    from shared.db.mysql import get_sessionmaker

    session = get_sessionmaker()()
    try:
        candidates = plan(session, tenant_id=tenant_id, bot_ids=bot_ids)
        if not candidates:
            print("no legacy archived bots (status=archived AND is_deleted=1) in scope")
            return 0
        print(f"{'bot':18} {'tenant':16} {'deleted_at':20} {'channels':8} decision")
        for c in candidates:
            when = c.deleted_at.strftime("%Y-%m-%d %H:%M:%S") if c.deleted_at else "-"
            print(f"{c.bot_id:18} {c.tenant_id:16} {when:20} {len(c.channel_ids):<8} {c.reason}  ({c.name})")
        to_convert = [c for c in candidates if c.convert]
        print(f"\n{len(to_convert)} of {len(candidates)} would be converted")
        if not do_apply:
            print("dry run — re-run with --apply to write")
            return 0
        changed = apply(session, to_convert)
        session.commit()
        print(f"converted {changed} bot(s)")
        return changed
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tenant", help="limit to one tenant id")
    parser.add_argument("--bot", action="append", help="limit to these bot ids (repeatable)")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    run(tenant_id=args.tenant, bot_ids=args.bot, do_apply=args.apply)


if __name__ == "__main__":
    main()
