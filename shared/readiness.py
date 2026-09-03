"""Bot readiness derivation — the single source of truth for the checklist.

Each of the seven standard readiness items (r1–r7, created per bot at bot
creation) is DERIVED from real platform state, never hand-set:

  r1  Knowledge sources indexed   ≥1 indexed source usable by the bot
  r2  Voice selected & tuned      a voice is configured (profile or TTS voice)
  r3  Core prompts approved       system prompt exists; core prompts approved
  r4  Intents validated           ≥1 active intent, all with training phrases
  r5  Workflow published          latest workflow is approved with real nodes
  r6  Channel connected           ≥1 channel saved (configured) or live
  r7  Regression suite passing    scenarios exist and every last run passed

Routers call ``refresh_readiness`` after any mutation that can change one of
these facts, so the stored ``voice_bot_readiness.done`` flags stay in sync
with reality. Flags are recomputed only for the affected keys — they are
never rewritten on reads, so bots whose domains are untouched (e.g. seeded
demo data) keep their stored checklist.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import (
    ChannelConfig,
    Intent,
    KnowledgeSource,
    Prompt,
    TestScenario,
    VoiceBot,
    VoiceBotSetting,
    Workflow,
)

READINESS_KEYS = ("r1", "r2", "r3", "r4", "r5", "r6", "r7")

# The prompt types a bot cannot go live without; other types (fallback,
# closing, …) are optional extras and never block readiness.
CORE_PROMPT_TYPES = ("system", "greeting")

# A saved ("configured") channel counts as connected; "live" means its
# connection test also passed. Mirrors the release-checklist semantics.
CONNECTED_CHANNEL_STATUSES = ("live", "configured")

# Workflow lifecycle is draft → pending_approval → approved; "approved" is
# the terminal published state the runtime executes.
PUBLISHED_WORKFLOW_STATUS = "approved"

APPROVED_PROMPT_STATES = ("approved", "published")


def _knowledge_indexed(db: Session, bot: VoiceBot) -> bool:
    """≥1 indexed knowledge source the runtime would authorize for this bot:
    bot-scoped, tenant-scoped for the bot's tenant, or global."""
    return bool(
        db.scalar(
            select(KnowledgeSource.id)
            .where(
                KnowledgeSource.is_deleted.is_(False),
                KnowledgeSource.status == "indexed",
                (
                    (KnowledgeSource.bot_id == bot.id)
                    | (
                        (KnowledgeSource.tenant_id == bot.tenant_id)
                        & (KnowledgeSource.scope == "tenant")
                    )
                    | (KnowledgeSource.scope == "global")
                ),
            )
            .limit(1)
        )
    )


def _voice_selected(db: Session, bot: VoiceBot) -> bool:
    """A voice is configured: a voice profile on the bot or its settings, an
    explicit TTS voice, or a per-language voice map entry."""
    if bot.voice_id:
        return True
    s = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bot.id))
    if s is None:
        return False
    return bool(s.voice_id or s.tts_voice or (s.language_voice_map or {}))


def _prompts_approved(db: Session, bot: VoiceBot) -> bool:
    """The bot has a system prompt and every core prompt is approved or
    published (a draft optional prompt never blocks readiness)."""
    core = db.scalars(
        select(Prompt).where(
            Prompt.bot_id == bot.id,
            Prompt.is_deleted.is_(False),
            Prompt.type.in_(CORE_PROMPT_TYPES),
        )
    ).all()
    if not any(p.type == "system" for p in core):
        return False
    return all(p.state in APPROVED_PROMPT_STATES for p in core)


def _intents_validated(db: Session, bot: VoiceBot) -> bool:
    """≥1 active intent, every active intent has training phrases, and none
    is flagged needs_samples."""
    intents = db.scalars(
        select(Intent).where(Intent.bot_id == bot.id, Intent.is_deleted.is_(False))
    ).all()
    active = [i for i in intents if i.status == "active"]
    if not active:
        return False
    if any(i.status == "needs_samples" for i in intents):
        return False
    return all(bool(i.samples) for i in active)


def _workflow_published(db: Session, bot: VoiceBot) -> bool:
    """The bot's latest workflow is approved and actually has a graph."""
    w = db.scalar(
        select(Workflow)
        .where(Workflow.bot_id == bot.id, Workflow.is_deleted.is_(False))
        .order_by(Workflow.version.desc())
        .limit(1)
    )
    return w is not None and w.status == PUBLISHED_WORKFLOW_STATUS and bool(w.nodes)


def _channel_connected(db: Session, bot: VoiceBot) -> bool:
    return bool(
        db.scalar(
            select(ChannelConfig.id)
            .where(
                ChannelConfig.bot_id == bot.id,
                ChannelConfig.is_deleted.is_(False),
                ChannelConfig.status.in_(CONNECTED_CHANNEL_STATUSES),
            )
            .limit(1)
        )
    )


def _regression_passing(db: Session, bot: VoiceBot) -> bool:
    """Scenarios exist, every one has been run, and every run passed —
    the same bar the release checklist applies."""
    rows = db.scalars(
        select(TestScenario).where(
            TestScenario.bot_id == bot.id, TestScenario.is_deleted.is_(False)
        )
    ).all()
    if not rows:
        return False
    return all(bool((s.last_run or {}).get("pass")) for s in rows)


_EVALUATORS = {
    "r1": _knowledge_indexed,
    "r2": _voice_selected,
    "r3": _prompts_approved,
    "r4": _intents_validated,
    "r5": _workflow_published,
    "r6": _channel_connected,
    "r7": _regression_passing,
}


def evaluate_readiness(
    db: Session, bot: VoiceBot, keys: Iterable[str] | None = None
) -> dict[str, bool]:
    """Compute the derived value of each requested item from live state."""
    wanted = tuple(keys) if keys is not None else READINESS_KEYS
    return {key: _EVALUATORS[key](db, bot) for key in wanted if key in _EVALUATORS}


def refresh_readiness(
    db: Session, bot: VoiceBot, keys: Iterable[str] | None = None
) -> dict[str, bool]:
    """Recompute the requested items and persist them onto the bot's
    readiness rows. The caller owns the transaction (no commit here).

    Sessions run with ``autoflush=False``, so a row the caller just added,
    restored or re-statused is still only in memory here. Flush first so the
    evaluators' SELECTs see the mutation they were called to reflect —
    otherwise e.g. re-creating an archived channel leaves r6 stuck at False."""
    db.flush()
    derived = evaluate_readiness(db, bot, keys)
    for item in bot.readiness_items:
        if item.item_key in derived:
            item.done = derived[item.item_key]
    return derived


def refresh_readiness_for_source(db: Session, source: KnowledgeSource) -> None:
    """Refresh r1 for every bot a knowledge source's scope can serve.

    Global sources are excluded on purpose: they would fan out to every bot
    on the platform. Bots pick the change up on their next own refresh.
    """
    if source.scope == "global":
        return
    stmt = select(VoiceBot).where(VoiceBot.is_deleted.is_(False))
    if source.scope == "bot":
        if not source.bot_id:
            return
        stmt = stmt.where(VoiceBot.id == source.bot_id)
    else:  # tenant scope
        if not source.tenant_id:
            return
        stmt = stmt.where(VoiceBot.tenant_id == source.tenant_id)
    for bot in db.scalars(stmt).all():
        refresh_readiness(db, bot, keys=("r1",))
