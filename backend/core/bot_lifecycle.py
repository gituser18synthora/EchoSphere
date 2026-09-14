"""Bot lifecycle transitions: archive, restore and permanent delete.

Two distinct product actions share this module so their side effects live in
one place:

* **Archive** is a reversible management state. The bot keeps ``is_deleted=0``
  and moves to ``status="archived"``: it stays visible under the Archived
  filter and through the management APIs, its configuration (workflows,
  prompts and published prompt versions, intents, knowledge sources,
  guardrails, voice/language settings, test scenarios) is untouched, but it
  takes no runtime traffic. Channels are deactivated (not deleted) and the
  bot's phone numbers move from ``assigned`` to ``reserved`` so nothing else
  can claim them while the bot is parked.

* **Restore** returns an archived bot to ``draft`` and hands its numbers back
  (``reserved`` -> ``assigned``). Channels stay deactivated until they are
  re-tested, and the bot is not live until it is published again.

* **Delete** is permanent from the product's point of view. The bot row is
  kept as a tombstone (``is_deleted=1``; the workflow status is preserved so
  the tombstone still says what the bot was), channels are archived and
  disabled, phone numbers return to the platform pool and the bot's editable
  configuration is soft-deleted so no tenant-level listing can surface it.
  History (conversations, transcripts, usage/billing rows, audit rows,
  post-call memories) is never touched.

Callers own the transaction: every helper only mutates the session and returns
a summary for the audit row. Commit and runtime cache invalidation happen in
the router after the commit succeeds.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.softdelete import soft_delete
from shared.errors import ApiError
from shared.models import (
    ApiConnection,
    ChannelConfig,
    Intent,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    RuntimeContextSchema,
    TestScenario,
    User,
    VoiceBot,
    Workflow,
)

ARCHIVED = "archived"
RESTORED_STATUS = "draft"

# Phone-number states owned by this module. ``reserved`` keeps the tenant/bot
# link of an archived bot's number: inbound routing only trusts ``assigned``,
# and a reserved number is refused to every other bot until the owner is
# restored or deleted.
NUMBER_ASSIGNED = "assigned"
NUMBER_RESERVED = "reserved"
NUMBER_AVAILABLE = "available"


def is_archived(bot: VoiceBot) -> bool:
    return bot.status == ARCHIVED


def assert_bot_operational(bot: VoiceBot, *, action: str = "continue") -> None:
    """409 for runtime-shaped operations on an archived bot.

    Archived bots stay readable (list, detail, configuration, history) but
    cannot start sessions, run tests, take channel traffic or be published
    until they are restored to draft."""
    if is_archived(bot):
        raise ApiError(f"This bot is archived. Restore it to {action}.", 409)


def _live_channels(db: Session, bot: VoiceBot) -> list[ChannelConfig]:
    return list(db.scalars(
        select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id, ChannelConfig.is_deleted.is_(False)
        )
    ).all())


def _bot_numbers(db: Session, bot: VoiceBot) -> list[PhoneNumber]:
    return list(db.scalars(
        select(PhoneNumber).where(
            PhoneNumber.bot_id == bot.id, PhoneNumber.is_deleted.is_(False)
        )
    ).all())


def archive_bot(db: Session, bot: VoiceBot, user: User) -> dict:
    """Park the bot: status -> archived, channels deactivated, numbers reserved."""
    if is_archived(bot):
        raise ApiError("This bot is already archived.", 409)
    previous = {"status": bot.status, "liveVersion": bot.live_version}

    channels = _live_channels(db, bot)
    for channel in channels:
        channel.enabled = False
        if channel.status == "live":
            channel.status = "configured"  # same demotion as a manual deactivate
        channel.updated_by = user.id

    reserved = 0
    for number in _bot_numbers(db, bot):
        if number.status == NUMBER_ASSIGNED:
            number.status = NUMBER_RESERVED
            number.updated_by = user.id
            reserved += 1

    bot.status = ARCHIVED
    # Nothing is live while archived; the previous value travels in the audit
    # row and the release history keeps every published version.
    bot.live_version = None
    bot.updated_by = user.id
    return {
        "previousStatus": previous["status"],
        "previousLiveVersion": previous["liveVersion"],
        "channelsDisabled": len(channels),
        "phoneNumbersReserved": reserved,
    }


def restore_bot(db: Session, bot: VoiceBot, user: User) -> dict:
    """Archived -> draft. Numbers go back to assigned; channels stay off; not live."""
    if not is_archived(bot):
        raise ApiError("Only an archived bot can be restored.", 409)

    reassigned = 0
    for number in _bot_numbers(db, bot):
        if number.status == NUMBER_RESERVED:
            number.status = NUMBER_ASSIGNED
            number.updated_by = user.id
            reassigned += 1

    bot.status = RESTORED_STATUS
    bot.updated_by = user.id
    return {"status": RESTORED_STATUS, "phoneNumbersReassigned": reassigned}


def permanently_delete_bot(db: Session, bot: VoiceBot, user: User) -> dict:
    """Tombstone the bot and take every live surface out of service.

    Order matters for the reader, not the database (one transaction): the bot
    row is flagged first so the intent is unambiguous, then channels, numbers
    and editable configuration are torn down."""
    previous_status = bot.status
    soft_delete(bot, user, keep_status=True)

    channels = _live_channels(db, bot)
    for channel in channels:
        channel.enabled = False
        soft_delete(channel, user)

    released = 0
    for number in _bot_numbers(db, bot):
        # Same semantics as releasing a voice channel: a tenant owns a number
        # only while one of its bots does, so the number returns to the
        # platform pool for any tenant to claim next.
        number.bot_id = None
        number.tenant_id = None
        number.status = NUMBER_AVAILABLE
        number.updated_by = user.id
        released += 1

    # Editable / operational configuration becomes inaccessible everywhere,
    # including tenant-level listings (Knowledge Hub, Workflows page). Rows
    # are soft-deleted, never purged: conversation history references them.
    retired: dict[str, int] = {}
    scoped = (
        ("workflows", Workflow, Workflow.bot_id == bot.id),
        ("prompts", Prompt, Prompt.bot_id == bot.id),
        ("intents", Intent, Intent.bot_id == bot.id),
        ("knowledgeSources", KnowledgeSource,
         (KnowledgeSource.bot_id == bot.id) & (KnowledgeSource.scope == "bot")),
        ("testScenarios", TestScenario, TestScenario.bot_id == bot.id),
        ("runtimeContextSchemas", RuntimeContextSchema,
         RuntimeContextSchema.bot_id == bot.id),
        ("apiConnections", ApiConnection, ApiConnection.bot_id == bot.id),
    )
    for key, model, predicate in scoped:
        rows = db.scalars(
            select(model).where(predicate, model.is_deleted.is_(False))
        ).all()
        for row in rows:
            soft_delete(row, user)
        retired[key] = len(rows)

    return {
        "previousStatus": previous_status,
        "channelsArchived": len(channels),
        "phoneNumbersReleased": released,
        "configurationRetired": retired,
    }
