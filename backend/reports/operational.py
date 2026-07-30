"""Definitions and database queries for operational table exports.

These exports intentionally query the database directly instead of serializing
the rows currently rendered by the frontend.  Each query applies its filters
before fetching rows and has a stable ordering, so pagination in the UI cannot
truncate or reorder the downloaded dataset.
"""

from datetime import date

from sqlalchemy import String, cast, extract, func, or_, select
from sqlalchemy.orm import Session

from backend.reports.registry import ReportColumn, ReportData, ReportDefinition
from shared.models import (
    ConversationSession,
    Invoice,
    Plan,
    Subscription,
    Tenant,
    UsageRecord,
    VoiceBot,
)


SUBSCRIPTIONS_EXPORT = ReportDefinition(
    code="subscriptions",
    name="Subscriptions",
    worksheet_name="Subscriptions",
    columns=(
        ReportColumn("subscription_id", "Subscription ID", "text", 24),
        ReportColumn("tenant", "Tenant", "text", 28),
        ReportColumn("plan_code", "Plan Code", "text", 16),
        ReportColumn("plan_name", "Plan Name", "text", 22),
        ReportColumn("status", "Status", "text", 14),
        ReportColumn("seats", "Seats", "integer", 10),
        ReportColumn("bot_limit", "Bot Limit", "integer", 12),
        ReportColumn("minutes_included", "Minutes Included", "integer", 18),
        ReportColumn("minutes_used", "Minutes Used This Month", "decimal", 24),
        ReportColumn("usage_percent", "Minutes Used %", "percentage", 17),
        ReportColumn("mrr", "MRR", "currency", 14),
        ReportColumn("currency", "Currency", "text", 11),
        ReportColumn("renewal_date", "Renewal Date", "date", 15),
        ReportColumn("created_at", "Created At", "datetime", 21),
    ),
    platform_only=True,
    supports_bot_filter=False,
)

INVOICES_EXPORT = ReportDefinition(
    code="invoices",
    name="Invoices",
    worksheet_name="Invoices",
    columns=(
        ReportColumn("invoice_id", "Invoice ID", "text", 24),
        ReportColumn("tenant", "Tenant", "text", 28),
        ReportColumn("period", "Period", "text", 16),
        ReportColumn("amount", "Amount", "currency", 14),
        ReportColumn("status", "Status", "text", 14),
        ReportColumn("issued_at", "Issued Date", "date", 15),
        ReportColumn("created_at", "Created At", "datetime", 21),
    ),
    platform_only=True,
    supports_bot_filter=False,
)

CONVERSATIONS_EXPORT = ReportDefinition(
    code="conversations",
    name="Conversations",
    worksheet_name="Conversations",
    columns=(
        ReportColumn("conversation_id", "Call ID", "text", 24),
        ReportColumn("bot", "Voice Bot", "text", 24),
        ReportColumn("channel", "Channel", "text", 12),
        ReportColumn("caller", "Caller", "text", 18),
        ReportColumn("started_at", "Started At", "datetime", 21),
        ReportColumn("duration_seconds", "Duration (seconds)", "integer", 19),
        ReportColumn("sentiment", "Sentiment", "text", 13),
        ReportColumn("intents", "Intents", "text", 34, wrap=True),
        ReportColumn("outcome", "Outcome", "text", 14),
        ReportColumn("escalation_reason", "Escalation Reason", "text", 36, wrap=True),
        ReportColumn("csat", "CSAT", "integer", 9),
        ReportColumn("cost_usd", "Cost (USD)", "currency", 14),
        ReportColumn("language", "Language", "text", 12),
        ReportColumn("qa_score", "QA Score", "integer", 11),
        ReportColumn("flagged", "Flagged", "text", 10),
    ),
)

TRANSCRIPT_EXPORT = ReportDefinition(
    code="conversation_transcript",
    name="Conversation Transcript",
    worksheet_name="Transcript",
    columns=(
        ReportColumn("turn", "Turn", "integer", 9),
        ReportColumn("speaker", "Speaker", "text", 12),
        ReportColumn("text", "Text", "text", 45, wrap=True),
        ReportColumn("intent", "Intent", "text", 22),
        ReportColumn("confidence", "Confidence", "percentage", 14),
        ReportColumn("chunks_used", "Knowledge Chunks", "text", 36, wrap=True),
        ReportColumn("api_calls", "API Calls", "text", 36, wrap=True),
        ReportColumn("prompt_version", "Prompt Version", "text", 18),
        ReportColumn("latency_ms", "Latency (ms)", "integer", 14),
        ReportColumn("cost_usd", "Cost (USD)", "currency", 14),
    ),
)

OPERATIONAL_EXPORT_REGISTRY: dict[str, ReportDefinition] = {
    definition.code: definition
    for definition in (
        SUBSCRIPTIONS_EXPORT,
        INVOICES_EXPORT,
        CONVERSATIONS_EXPORT,
    )
}

SUBSCRIPTION_STATUSES = frozenset({"active", "past_due", "cancelled", "trial"})
INVOICE_STATUSES = frozenset({"paid", "open", "past_due", "void"})
CONVERSATION_SENTIMENTS = frozenset({"positive", "neutral", "negative"})


def _search_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def build_subscriptions_export(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
    plan: str | None = None,
) -> ReportData:
    stmt = (
        select(
            Subscription,
            Tenant.name,
            Plan.code,
            Plan.name,
            Plan.currency,
        )
        .join(Tenant, Tenant.id == Subscription.tenant_id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.is_deleted.is_(False),
            Tenant.is_deleted.is_(False),
            Plan.is_deleted.is_(False),
        )
    )
    if search:
        pattern = _search_pattern(search)
        stmt = stmt.where(
            or_(
                Subscription.id.like(pattern, escape="\\"),
                Tenant.name.like(pattern, escape="\\"),
                Plan.code.like(pattern, escape="\\"),
            )
        )
    if status:
        stmt = stmt.where(Subscription.status == status)
    if plan:
        stmt = stmt.where(Plan.code == plan)

    records = db.execute(
        stmt.order_by(Tenant.name.asc(), Subscription.id.asc())
    ).all()

    tenant_ids = tuple({subscription.tenant_id for subscription, *_ in records})
    minutes_by_tenant: dict[str, float] = {}
    if tenant_ids:
        today = date.today()
        minutes_by_tenant = {
            tenant_id: float(minutes or 0)
            for tenant_id, minutes in db.execute(
                select(
                    UsageRecord.tenant_id,
                    func.coalesce(func.sum(UsageRecord.minutes), 0),
                )
                .where(
                    UsageRecord.tenant_id.in_(tenant_ids),
                    UsageRecord.bot_id.is_(None),
                    extract("year", UsageRecord.date) == today.year,
                    extract("month", UsageRecord.date) == today.month,
                )
                .group_by(UsageRecord.tenant_id)
            ).all()
        }

    rows = []
    for subscription, tenant_name, plan_code, plan_name, currency in records:
        minutes_used = minutes_by_tenant.get(subscription.tenant_id, 0.0)
        rows.append(
            {
                "subscription_id": subscription.id,
                "tenant": tenant_name,
                "plan_code": plan_code,
                "plan_name": plan_name,
                "status": subscription.status,
                "seats": subscription.seats,
                "bot_limit": subscription.bot_limit,
                "minutes_included": subscription.minutes_included,
                "minutes_used": round(minutes_used, 2),
                "usage_percent": (
                    minutes_used / subscription.minutes_included
                    if subscription.minutes_included
                    else 0.0
                ),
                "mrr": subscription.mrr,
                "currency": currency,
                "renewal_date": subscription.renews_at,
                "created_at": subscription.created_at,
            }
        )
    return ReportData(SUBSCRIPTIONS_EXPORT, rows)


def build_invoices_export(
    db: Session,
    *,
    search: str | None = None,
    status: str | None = None,
) -> ReportData:
    stmt = (
        select(Invoice, Tenant.name)
        .join(Tenant, Tenant.id == Invoice.tenant_id)
        .where(
            Invoice.is_deleted.is_(False),
            Tenant.is_deleted.is_(False),
        )
    )
    if search:
        pattern = _search_pattern(search)
        stmt = stmt.where(
            or_(
                Invoice.id.like(pattern, escape="\\"),
                Tenant.name.like(pattern, escape="\\"),
                Invoice.period.like(pattern, escape="\\"),
            )
        )
    if status:
        stmt = stmt.where(Invoice.status == status)

    records = db.execute(
        stmt.order_by(Invoice.issued_at.desc(), Invoice.id.asc())
    ).all()
    return ReportData(
        INVOICES_EXPORT,
        [
            {
                "invoice_id": invoice.id,
                "tenant": tenant_name,
                "period": invoice.period,
                "amount": invoice.amount,
                "status": invoice.status,
                "issued_at": invoice.issued_at,
                "created_at": invoice.created_at,
            }
            for invoice, tenant_name in records
        ],
    )


def build_conversations_export(
    db: Session,
    *,
    tenant_id: str,
    search: str | None = None,
    bot_id: str | None = None,
    sentiment: str | None = None,
    contained: bool | None = None,
    flagged: bool | None = None,
) -> ReportData:
    stmt = (
        select(ConversationSession, VoiceBot.name)
        .join(VoiceBot, VoiceBot.id == ConversationSession.bot_id)
        .where(
            ConversationSession.tenant_id == tenant_id,
            ConversationSession.is_deleted.is_(False),
            VoiceBot.is_deleted.is_(False),
        )
    )
    if bot_id:
        stmt = stmt.where(ConversationSession.bot_id == bot_id)
    if sentiment:
        stmt = stmt.where(ConversationSession.sentiment == sentiment)
    if contained is not None:
        stmt = stmt.where(ConversationSession.contained.is_(contained))
    if flagged is not None:
        stmt = stmt.where(ConversationSession.flagged.is_(flagged))
    if search:
        pattern = _search_pattern(search)
        stmt = stmt.where(
            or_(
                ConversationSession.id.like(pattern, escape="\\"),
                VoiceBot.name.like(pattern, escape="\\"),
                cast(ConversationSession.intents, String).like(pattern, escape="\\"),
            )
        )

    records = db.execute(
        stmt.order_by(
            ConversationSession.started_at.desc(),
            ConversationSession.id.asc(),
        )
    ).all()
    return ReportData(
        CONVERSATIONS_EXPORT,
        [
            {
                "conversation_id": conversation.id,
                "bot": bot_name,
                "channel": conversation.channel,
                "caller": conversation.caller_masked or "•••",
                "started_at": conversation.started_at,
                "duration_seconds": conversation.duration_sec,
                "sentiment": conversation.sentiment,
                "intents": "; ".join(conversation.intents or []),
                "outcome": "Contained" if conversation.contained else "Escalated",
                "escalation_reason": conversation.escalation_reason or "",
                "csat": conversation.csat,
                "cost_usd": conversation.cost_usd,
                "language": conversation.language or "en-US",
                "qa_score": conversation.qa_score,
                "flagged": "Yes" if conversation.flagged else "No",
            }
            for conversation, bot_name in records
        ],
    )


def build_transcript_export(turns: list[dict]) -> ReportData:
    rows = []
    for turn in sorted(turns, key=lambda item: int(item.get("turn") or 0)):
        api_calls = []
        for call in turn.get("apiCalls") or turn.get("api_calls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "API")
            outcome = "ok" if call.get("ok") else "failed"
            elapsed = f", {call.get('ms')}ms" if call.get("ms") is not None else ""
            api_calls.append(f"{name} ({outcome}{elapsed})")
        rows.append(
            {
                "turn": int(turn.get("turn") or 0),
                "speaker": str(turn.get("speaker") or ""),
                "text": str(turn.get("text") or ""),
                "intent": str(turn.get("intent") or ""),
                "confidence": turn.get("confidence"),
                "chunks_used": "; ".join(
                    str(value)
                    for value in (turn.get("chunksUsed") or turn.get("chunks_used") or [])
                ),
                "api_calls": "; ".join(api_calls),
                "prompt_version": str(
                    turn.get("promptVersion") or turn.get("prompt_version") or ""
                ),
                "latency_ms": turn.get("latencyMs") or turn.get("latency_ms"),
                "cost_usd": turn.get("costUsd") or turn.get("cost_usd"),
            }
        )
    return ReportData(TRANSCRIPT_EXPORT, rows)
