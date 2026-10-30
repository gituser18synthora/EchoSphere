"""Report registry and database-side report builders.

Reports are deliberately aggregate and date-bounded (7–90 days at the API
boundary).  The database performs filtering and grouping, so exports never
depend on frontend pagination and cannot be silently truncated.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from shared.billing import convert_from_usd, effective_rate
from shared.models import Plan, Subscription, Tenant, UsageRecord

CellKind = Literal["text", "date", "datetime", "integer", "decimal", "percentage", "currency"]


@dataclass(frozen=True)
class ReportColumn:
    key: str
    header: str
    kind: CellKind = "text"
    width: int = 16
    wrap: bool = False


@dataclass(frozen=True)
class ReportDefinition:
    code: str
    name: str
    worksheet_name: str
    columns: tuple[ReportColumn, ...]
    platform_only: bool = False
    supports_bot_filter: bool = True
    # Entirely financial reports require the costs.view permission — roles
    # without it (tenant_user) must not be able to export cost data at all.
    requires_costs_view: bool = False


@dataclass(frozen=True)
class ReportData:
    definition: ReportDefinition
    rows: list[dict]


USAGE_REPORT = ReportDefinition(
    code="usage",
    name="Usage",
    worksheet_name="Usage",
    columns=(
        ReportColumn("date", "Date", "date", 13),
        ReportColumn("calls", "Calls", "integer", 12),
        ReportColumn("contained_calls", "Contained Calls", "integer", 18),
        ReportColumn("escalations", "Escalations", "integer", 14),
        ReportColumn("minutes", "Minutes", "decimal", 13),
        ReportColumn("containment_rate", "Containment Rate", "percentage", 19),
        ReportColumn("average_csat", "Average CSAT", "decimal", 15),
    ),
)

REVENUE_REPORT = ReportDefinition(
    code="revenue",
    name="Revenue",
    worksheet_name="Revenue by Plan",
    columns=(
        ReportColumn("date", "Date", "date", 13),
        ReportColumn("plan_code", "Plan Code", "text", 15),
        ReportColumn("plan_name", "Plan Name", "text", 22),
        ReportColumn("currency", "Currency", "text", 12),
        ReportColumn("active_subscriptions", "Active Subscriptions", "integer", 22),
        ReportColumn("mrr", "MRR", "currency", 16),
        ReportColumn("daily_revenue", "Daily Revenue", "currency", 18),
        # INR display conversion for USD-priced plans; blank for other plan
        # currencies (no chained conversions) or when no rate is configured.
        ReportColumn("mrr_inr", "MRR (INR)", "currency", 16),
        ReportColumn("exchange_rate_inr", "USD→INR Rate", "decimal", 15),
    ),
    platform_only=True,
    supports_bot_filter=False,
)

AI_COST_REPORT = ReportDefinition(
    code="ai_cost",
    name="AI Cost",
    worksheet_name="AI Cost",
    columns=(
        ReportColumn("date", "Date", "date", 13),
        ReportColumn("llm_cost", "LLM Cost (USD)", "currency", 17),
        ReportColumn("tts_cost", "TTS Cost (USD)", "currency", 17),
        ReportColumn("stt_cost", "STT Cost (USD)", "currency", 17),
        ReportColumn("embedding_cost", "Embedding Cost (USD)", "currency", 22),
        ReportColumn("telephony_cost", "Telephony Cost (USD)", "currency", 22),
        ReportColumn("ai_cost", "AI Cost (USD)", "currency", 17),
        ReportColumn("total_cost", "Total Cost (USD)", "currency", 19),
        # Display conversion with the exchange rate in force when the report
        # is generated; blank when no USD→INR rate is configured.
        ReportColumn("ai_cost_inr", "AI Cost (INR)", "currency", 17),
        ReportColumn("total_cost_inr", "Total Cost (INR)", "currency", 19),
        ReportColumn("exchange_rate_inr", "USD→INR Rate", "decimal", 15),
    ),
    requires_costs_view=True,
)

REPORT_REGISTRY: dict[str, ReportDefinition] = {
    report.code: report for report in (USAGE_REPORT, REVENUE_REPORT, AI_COST_REPORT)
}


def _date_sequence(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _daily_usage(
    db: Session,
    *,
    start: date,
    end: date,
    tenant_id: str | None,
    bot_id: str | None,
) -> dict[date, dict]:
    """Aggregate the same tenant-level rollups used by the analytics previews.

    NULL ``bot_id`` rows are tenant rollups.  A concrete bot filter switches
    to per-bot rows.  Joining Tenant excludes soft-deleted organizations from
    platform-wide reports.
    """

    stmt = (
        select(
            UsageRecord.date,
            func.coalesce(func.sum(UsageRecord.calls), 0),
            func.coalesce(func.sum(UsageRecord.contained_calls), 0),
            func.coalesce(func.sum(UsageRecord.escalations), 0),
            func.coalesce(func.sum(UsageRecord.minutes), 0),
            func.avg(UsageRecord.csat_avg),
            func.coalesce(func.sum(UsageRecord.cost_llm), 0),
            func.coalesce(func.sum(UsageRecord.cost_tts), 0),
            func.coalesce(func.sum(UsageRecord.cost_stt), 0),
            func.coalesce(func.sum(UsageRecord.cost_telephony), 0),
            func.coalesce(func.sum(UsageRecord.cost_embedding), 0),
        )
        .join(Tenant, Tenant.id == UsageRecord.tenant_id)
        .where(
            UsageRecord.date >= start,
            UsageRecord.date <= end,
            Tenant.is_deleted.is_(False),
        )
        .group_by(UsageRecord.date)
        .order_by(UsageRecord.date.asc())
    )
    if tenant_id is not None:
        stmt = stmt.where(UsageRecord.tenant_id == tenant_id)
    if bot_id is not None:
        stmt = stmt.where(UsageRecord.bot_id == bot_id)
    else:
        stmt = stmt.where(UsageRecord.bot_id.is_(None))

    return {
        row[0]: {
            "calls": int(row[1]),
            "contained_calls": int(row[2]),
            "escalations": int(row[3]),
            "minutes": float(row[4]),
            "average_csat": float(row[5]) if row[5] is not None else None,
            "llm_cost": float(row[6]),
            "tts_cost": float(row[7]),
            "stt_cost": float(row[8]),
            "telephony_cost": float(row[9]),
            "embedding_cost": float(row[10]),
        }
        for row in db.execute(stmt).all()
    }


def _usage_rows(
    db: Session, *, start: date, end: date, tenant_id: str | None, bot_id: str | None
) -> list[dict]:
    daily = _daily_usage(
        db, start=start, end=end, tenant_id=tenant_id, bot_id=bot_id
    )
    rows: list[dict] = []
    for day in _date_sequence(start, end):
        values = daily.get(day, {})
        calls = int(values.get("calls", 0))
        contained = int(values.get("contained_calls", 0))
        rows.append(
            {
                "date": day,
                "calls": calls,
                "contained_calls": contained,
                "escalations": int(values.get("escalations", 0)),
                "minutes": round(float(values.get("minutes", 0)), 2),
                "containment_rate": contained / calls if calls else 0.0,
                "average_csat": (
                    round(float(values["average_csat"]), 2)
                    if values.get("average_csat") is not None
                    else None
                ),
            }
        )
    return rows


def _ai_cost_rows(
    db: Session, *, start: date, end: date, tenant_id: str | None, bot_id: str | None
) -> list[dict]:
    daily = _daily_usage(
        db, start=start, end=end, tenant_id=tenant_id, bot_id=bot_id
    )
    # Live display conversion (never a finalized billing figure): the INR
    # columns use the rate in force at generation time and stay blank when
    # no rate is configured — never silently zero.
    inr_rate = effective_rate(db, "INR")
    rows: list[dict] = []
    for day in _date_sequence(start, end):
        values = daily.get(day, {})
        llm = float(values.get("llm_cost", 0))
        tts = float(values.get("tts_cost", 0))
        stt = float(values.get("stt_cost", 0))
        embedding = float(values.get("embedding_cost", 0))
        telephony = float(values.get("telephony_cost", 0))
        ai_cost = llm + tts + stt + embedding
        total_cost = ai_cost + telephony
        rows.append(
            {
                "date": day,
                "llm_cost": round(llm, 4),
                "tts_cost": round(tts, 4),
                "stt_cost": round(stt, 4),
                "embedding_cost": round(embedding, 4),
                "telephony_cost": round(telephony, 4),
                "ai_cost": round(ai_cost, 4),
                "total_cost": round(total_cost, 4),
                "ai_cost_inr": (
                    float(convert_from_usd(ai_cost, inr_rate)) if inr_rate else None
                ),
                "total_cost_inr": (
                    float(convert_from_usd(total_cost, inr_rate)) if inr_rate else None
                ),
                "exchange_rate_inr": float(inr_rate) if inr_rate else None,
            }
        )
    return rows


def _revenue_rows(
    db: Session, *, start: date, end: date, tenant_id: str | None
) -> list[dict]:
    """Daily revenue run-rate split by plan, matching both revenue charts."""

    aggregate_stmt = (
        select(
            Subscription.plan_id,
            func.count(Subscription.id),
            func.coalesce(func.sum(Subscription.mrr), 0),
        )
        .join(Tenant, Tenant.id == Subscription.tenant_id)
        .where(
            Subscription.is_deleted.is_(False),
            Subscription.status == "active",
            Tenant.is_deleted.is_(False),
        )
        .group_by(Subscription.plan_id)
    )
    if tenant_id is not None:
        aggregate_stmt = aggregate_stmt.where(Subscription.tenant_id == tenant_id)
    aggregates = {
        plan_id: (int(count or 0), float(mrr or 0))
        for plan_id, count, mrr in db.execute(aggregate_stmt).all()
    }

    # Include current catalog plans plus any inactive legacy plan that still
    # has an active subscription. Inactive, unused catalog rows are omitted.
    plan_filter = Plan.status == "active"
    if aggregates:
        plan_filter = or_(plan_filter, Plan.id.in_(tuple(aggregates)))
    plan_rows = db.execute(
        select(
            Plan.id,
            Plan.code,
            Plan.name,
            Plan.currency,
        )
        .where(Plan.is_deleted.is_(False), plan_filter)
        .order_by(Plan.sort_order.asc(), Plan.code.asc())
    ).all()

    inr_rate = effective_rate(db, "INR")
    plans = [
        {
            "plan_code": code,
            "plan_name": name,
            "currency": currency,
            "active_subscriptions": aggregates.get(plan_id, (0, 0.0))[0],
            "mrr": aggregates.get(plan_id, (0, 0.0))[1],
            "mrr_inr": (
                float(convert_from_usd(aggregates.get(plan_id, (0, 0.0))[1], inr_rate))
                if inr_rate and (currency or "USD") == "USD"
                else None
            ),
            "exchange_rate_inr": (
                float(inr_rate) if inr_rate and (currency or "USD") == "USD" else None
            ),
        }
        for plan_id, code, name, currency in plan_rows
    ]

    rows: list[dict] = []
    for day in _date_sequence(start, end):
        for plan in plans:
            rows.append(
                {
                    "date": day,
                    **plan,
                    "daily_revenue": round(plan["mrr"] / 30, 2),
                }
            )
    return rows


def build_report(
    db: Session,
    report_type: str,
    *,
    start: date,
    end: date,
    tenant_id: str | None = None,
    bot_id: str | None = None,
) -> ReportData:
    definition = REPORT_REGISTRY[report_type]
    if report_type == "usage":
        rows = _usage_rows(
            db, start=start, end=end, tenant_id=tenant_id, bot_id=bot_id
        )
    elif report_type == "ai_cost":
        rows = _ai_cost_rows(
            db, start=start, end=end, tenant_id=tenant_id, bot_id=bot_id
        )
    else:
        rows = _revenue_rows(db, start=start, end=end, tenant_id=tenant_id)
    return ReportData(definition=definition, rows=rows)
