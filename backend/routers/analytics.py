"""Analytics & dashboards — computed from usage_records + conversation_sessions."""

from collections import Counter
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.deps import get_current_user, require_super_admin, resolve_tenant_id
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import (
    ApiConnection,
    ChannelConfig,
    ConversationSession,
    Intent,
    KnowledgeSource,
    Plan,
    Subscription,
    Tenant,
    UsageRecord,
    User,
    VoiceBot,
)

router = APIRouter(tags=["Analytics"])


def _label(d: date) -> str:
    return f"{d.strftime('%b')} {d.day}"


def _window(days: int) -> tuple[date, date]:
    end = date.today()
    start = end - timedelta(days=days - 1)
    return start, end


def _daily_usage(db: Session, tenant_id: str | None, start: date, end: date,
                 bot_id: str | None = None) -> dict[date, dict]:
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
        .where(UsageRecord.date >= start, UsageRecord.date <= end)
        .where(Tenant.is_deleted.is_(False))
        .group_by(UsageRecord.date)
    )
    if tenant_id is not None:
        stmt = stmt.where(UsageRecord.tenant_id == tenant_id)
    if bot_id is not None:
        stmt = stmt.where(UsageRecord.bot_id == bot_id)
    else:
        stmt = stmt.where(UsageRecord.bot_id.is_(None))
    out = {}
    for row in db.execute(stmt).all():
        out[row[0]] = {
            "calls": int(row[1]), "contained": int(row[2]), "escalations": int(row[3]),
            "minutes": float(row[4]), "csat": float(row[5]) if row[5] is not None else None,
            "llm": float(row[6]), "tts": float(row[7]), "stt": float(row[8]),
            "telephony": float(row[9]), "embedding": float(row[10]),
        }
    return out


def _ai_cost_sum(daily: dict[date, dict]) -> float:
    """AI provider spend (LLM + TTS + STT + embeddings), excluding telephony."""
    return (
        _sum(daily, "llm") + _sum(daily, "tts") + _sum(daily, "stt")
        + _sum(daily, "embedding")
    )


def _sum(daily: dict[date, dict], key: str) -> float:
    return sum(d[key] for d in daily.values())


def _delta(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _kpi(label: str, value: str, delta: float | None, spark: list, intent: str) -> dict:
    out = {"label": label, "value": value, "spark": spark, "intent": intent}
    if delta is not None:
        out["delta"] = delta
    return out


@router.get("/analytics/tenant")
def tenant_analytics(
    days: int = Query(30, ge=7, le=90),
    tenant_id: str | None = Query(None, alias="tenantId"),
    bot_id: str | None = Query(None, alias="botId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    start, end = _window(days)
    prev_start, prev_end = start - timedelta(days=days), start - timedelta(days=1)
    daily = _daily_usage(db, tid, start, end, bot_id)
    prev = _daily_usage(db, tid, prev_start, prev_end, bot_id)

    dates = [start + timedelta(days=i) for i in range(days)]
    calls_series = [daily.get(d, {}).get("calls", 0) for d in dates]
    contained_series = [daily.get(d, {}).get("contained", 0) for d in dates]
    rate_series = [
        round(c / t * 100, 1) if t else 0
        for c, t in zip(contained_series, calls_series)
    ]

    total_calls = int(_sum(daily, "calls"))
    total_contained = int(_sum(daily, "contained"))
    total_escalations = int(_sum(daily, "escalations"))
    containment = round(total_contained / total_calls * 100, 1) if total_calls else 0.0
    ai_cost = _ai_cost_sum(daily)
    total_cost = ai_cost + _sum(daily, "telephony")
    cost_per_call = round(total_cost / total_calls, 3) if total_calls else 0.0

    prev_calls = int(_sum(prev, "calls"))
    prev_contained = int(_sum(prev, "contained"))
    prev_containment = (prev_contained / prev_calls * 100) if prev_calls else 0
    prev_ai_cost = _ai_cost_sum(prev)
    prev_escalations = int(_sum(prev, "escalations"))

    # Sentiment / language / intents from conversation metadata in the window.
    conv_stmt = select(
        ConversationSession.started_at,
        ConversationSession.sentiment, ConversationSession.language,
        ConversationSession.intents, ConversationSession.csat,
    ).where(
        ConversationSession.tenant_id == tid,
        ConversationSession.is_deleted.is_(False),
        func.date(ConversationSession.started_at) >= start,
        func.date(ConversationSession.started_at) <= end,
    )
    if bot_id:
        conv_stmt = conv_stmt.where(ConversationSession.bot_id == bot_id)
    sentiments: Counter = Counter()
    languages: Counter = Counter()
    intent_counts: Counter = Counter()
    csat_sum_by_day: dict[date, int] = {}
    csat_count_by_day: dict[date, int] = {}
    csat_sum = 0
    csat_count = 0
    for started_at, sentiment, language, intents, csat in db.execute(conv_stmt).all():
        sentiments[sentiment] += 1
        if language:
            languages[language] += 1
        for name in intents or []:
            intent_counts[name] += 1
        if csat is not None:
            csat_sum += csat
            csat_count += 1
            csat_day = started_at.date()
            csat_sum_by_day[csat_day] = csat_sum_by_day.get(csat_day, 0) + csat
            csat_count_by_day[csat_day] = csat_count_by_day.get(csat_day, 0) + 1
    total_conv = sum(sentiments.values())
    avg_csat = round(csat_sum / csat_count, 1) if csat_count else 0.0
    daily_csat = {
        day: total / csat_count_by_day[day] for day, total in csat_sum_by_day.items()
    }

    def pct(n: int) -> int:
        return round(n / total_conv * 100) if total_conv else 0

    lang_names = {
        "en-US": "English (US)", "es-US": "Spanish (US)", "es-MX": "Spanish (MX)",
        "en-GB": "English (UK)", "hi-IN": "Hindi", "vi-VN": "Vietnamese", "fr-FR": "French",
    }

    knowledge = db.scalars(
        select(KnowledgeSource)
        .where(
            KnowledgeSource.is_deleted.is_(False),
            (KnowledgeSource.tenant_id == tid) | (KnowledgeSource.scope == "global"),
        )
        .order_by(KnowledgeSource.usage_30d.desc())
        .limit(5)
    ).all()

    recommendations = _tenant_recommendations(db, tid)

    return ok({
        "kpis": [
            _kpi("Total calls", f"{total_calls:,}", _delta(total_calls, prev_calls),
                 calls_series[-14:], "up-good"),
            _kpi("Containment rate", f"{containment}%",
                 _delta(containment, prev_containment), rate_series[-14:], "up-good"),
            _kpi("Escalations", f"{total_escalations:,}",
                 _delta(total_escalations, prev_escalations),
                 [daily.get(d, {}).get("escalations", 0) for d in dates][-14:], "down-good"),
            _kpi("Avg CSAT", f"{avg_csat} / 5" if avg_csat else "—", None,
                 [round(daily_csat.get(d, 0) * 10) for d in dates][-14:], "up-good"),
            _kpi("AI cost", f"${ai_cost:,.0f}", _delta(ai_cost, prev_ai_cost),
                 [round(daily.get(d, {}).get("llm", 0)) for d in dates][-14:], "down-good"),
            _kpi("Avg cost / call", f"${cost_per_call:.3f}", None,
                 calls_series[-14:], "down-good"),
        ],
        "callsSeries": [
            {"t": _label(d), "calls": calls_series[i], "contained": contained_series[i]}
            for i, d in enumerate(dates)
        ],
        "containmentSeries": [
            {"t": _label(d), "rate": rate_series[i]} for i, d in enumerate(dates)
        ],
        "sentimentSplit": [
            {"label": "Positive", "value": pct(sentiments.get("positive", 0))},
            {"label": "Neutral", "value": pct(sentiments.get("neutral", 0))},
            {"label": "Negative", "value": pct(sentiments.get("negative", 0))},
        ],
        "languageMix": [
            {"label": lang_names.get(code, code), "value": pct(n)}
            for code, n in languages.most_common(5)
        ],
        "topIntents": [
            {"label": name, "value": n, "trend": 0}
            for name, n in intent_counts.most_common(6)
        ],
        "knowledgeUsage": [
            {"label": k.name, "value": k.usage_30d} for k in knowledge
        ],
        "costSeries": [
            {
                "t": _label(d),
                "llm": round(daily.get(d, {}).get("llm", 0), 2),
                "tts": round(daily.get(d, {}).get("tts", 0), 2),
                "stt": round(daily.get(d, {}).get("stt", 0), 2),
                "telephony": round(daily.get(d, {}).get("telephony", 0), 2),
            }
            for d in dates
        ],
        "recommendations": recommendations,
    })


def _tenant_recommendations(db: Session, tenant_id: str) -> list[dict]:
    """Data-driven recommendations — replaces the old hardcoded list."""
    recs: list[dict] = []
    stale = db.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.tenant_id == tenant_id,
            KnowledgeSource.status == "stale",
            KnowledgeSource.is_deleted.is_(False),
        ).limit(2)
    ).all()
    for k in stale:
        recs.append({
            "id": f"rc-stale-{k.id}",
            "title": f"Re-sync stale knowledge: {k.name}",
            "detail": "This source is stale; answers based on it may be outdated. Re-syncing restores retrieval quality.",
            "impact": "high",
            "link": f"/t/bots/{k.bot_id}/knowledge" if k.bot_id else "/t/knowledge",
        })
    weak_intents = db.scalars(
        select(Intent).where(
            Intent.tenant_id == tenant_id,
            Intent.status == "needs_samples",
            Intent.is_deleted.is_(False),
        ).limit(2)
    ).all()
    for i in weak_intents:
        recs.append({
            "id": f"rc-intent-{i.id}",
            "title": f"Add samples to {i.name} intent",
            "detail": f"Average confidence {i.avg_confidence_30d:.2f} is below its {i.confidence_threshold:.2f} threshold.",
            "impact": "high",
            "link": f"/t/bots/{i.bot_id}/intents",
        })
    failing_apis = db.scalars(
        select(ApiConnection).where(
            ApiConnection.tenant_id == tenant_id,
            ApiConnection.status == "failing",
            ApiConnection.is_deleted.is_(False),
        ).limit(2)
    ).all()
    for a in failing_apis:
        recs.append({
            "id": f"rc-api-{a.id}",
            "title": f"Fix failing API: {a.name}",
            "detail": "This endpoint is failing, forcing escalations on flows that depend on it.",
            "impact": "high",
            "link": f"/t/bots/{a.bot_id}/apis" if a.bot_id else "/t/integrations",
        })
    unconfigured = db.execute(
        select(ChannelConfig.bot_id, VoiceBot.name)
        .join(VoiceBot, VoiceBot.id == ChannelConfig.bot_id)
        .where(
            ChannelConfig.tenant_id == tenant_id,
            ChannelConfig.type == "web",
            ChannelConfig.status == "not_configured",
            ChannelConfig.is_deleted.is_(False),
        ).limit(1)
    ).first()
    if unconfigured:
        recs.append({
            "id": f"rc-web-{unconfigured[0]}",
            "title": f"Enable web channel for {unconfigured[1]}",
            "detail": "A web widget deflects calls at lower cost for users already on your portal.",
            "impact": "medium",
            "link": f"/t/bots/{unconfigured[0]}/channels",
        })
    return recs[:4]


@router.get("/analytics/platform")
def platform_analytics(
    days: int = Query(30, ge=7, le=90),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    start, end = _window(days)
    daily = _daily_usage(db, None, start, end)
    dates = [start + timedelta(days=i) for i in range(days)]
    labels = [_label(d) for d in dates]
    call_vol = [daily.get(d, {}).get("calls", 0) for d in dates]
    ai_cost = [
        round(
            daily.get(d, {}).get("llm", 0)
            + daily.get(d, {}).get("tts", 0)
            + daily.get(d, {}).get("stt", 0)
            + daily.get(d, {}).get("embedding", 0),
            2,
        )
        for d in dates
    ]

    total_mrr = float(
        db.scalar(
            select(func.coalesce(func.sum(Subscription.mrr), 0))
            .join(Tenant, Tenant.id == Subscription.tenant_id)
            .join(Plan, Plan.id == Subscription.plan_id)
            .where(
                Subscription.is_deleted.is_(False),
                Subscription.status == "active",
                Tenant.is_deleted.is_(False),
                Plan.is_deleted.is_(False),
            )
        ) or 0
    )
    daily_revenue = round(total_mrr / 30, 2)

    plan_counts = dict(
        db.execute(
            select(Plan.code, func.count())
            .join(Subscription, Subscription.plan_id == Plan.id)
            .join(Tenant, Tenant.id == Subscription.tenant_id)
            .where(
                Subscription.is_deleted.is_(False),
                Subscription.status == "active",
                Tenant.is_deleted.is_(False),
                Plan.is_deleted.is_(False),
            )
            .group_by(Plan.code)
        ).all()
    )
    mrr_by_plan = db.execute(
        select(
            Plan.name,
            func.coalesce(func.sum(Subscription.mrr), 0),
        )
        .join(Subscription, Subscription.plan_id == Plan.id)
        .join(Tenant, Tenant.id == Subscription.tenant_id)
        .where(
            Subscription.is_deleted.is_(False),
            Subscription.status == "active",
            Tenant.is_deleted.is_(False),
            Plan.is_deleted.is_(False),
        )
        .group_by(Plan.id, Plan.name, Plan.sort_order)
        .order_by(Plan.sort_order.asc(), Plan.name.asc())
    ).all()

    top_tenants = db.execute(
        select(Tenant.name, func.coalesce(func.sum(UsageRecord.calls), 0).label("calls"))
        .join(UsageRecord, UsageRecord.tenant_id == Tenant.id)
        .where(
            UsageRecord.date >= start,
            UsageRecord.date <= end,
            UsageRecord.bot_id.is_(None),
            Tenant.is_deleted.is_(False),
        )
        .group_by(Tenant.id, Tenant.name)
        .order_by(func.sum(UsageRecord.calls).desc())
        .limit(6)
    ).all()

    cost_llm = _sum(daily, "llm")
    cost_tts = _sum(daily, "tts")
    cost_stt = _sum(daily, "stt")

    return ok({
        "labels": labels,
        "callVol": call_vol,
        "revenue": [daily_revenue] * days,
        "aiCost": ai_cost,
        "callsSeries": [{"t": labels[i], "calls": call_vol[i]} for i in range(days)],
        "revVsCost": [
            {"t": labels[i], "revenue": daily_revenue, "aiCost": ai_cost[i]}
            for i in range(days)
        ],
        "planMix": [
            {"label": "Enterprise", "value": plan_counts.get("enterprise", 0)},
            {"label": "Growth", "value": plan_counts.get("growth", 0)},
            {"label": "Starter", "value": plan_counts.get("starter", 0)},
        ],
        "mrrByPlan": [
            {"label": name, "value": float(mrr)}
            for name, mrr in mrr_by_plan
        ],
        "topTenantsByCalls": [
            {"label": name, "value": int(calls)} for name, calls in top_tenants if calls
        ],
        "aiCostByProvider": [
            {"label": "LLM", "value": round(cost_llm)},
            {"label": "STT", "value": round(cost_stt)},
            {"label": "TTS", "value": round(cost_tts)},
            {"label": "Telephony", "value": round(_sum(daily, "telephony"))},
        ],
    })


@router.get("/dashboard/admin")
def admin_dashboard(
    user: User = Depends(require_super_admin), db: Session = Depends(get_db)
):
    """Platform KPI cards — real counts, not hardcoded values."""
    start, end = _window(30)
    prev_start = start - timedelta(days=30)
    daily = _daily_usage(db, None, start, end)
    prev = _daily_usage(db, None, prev_start, start - timedelta(days=1))
    dates = [start + timedelta(days=i) for i in range(30)]

    active_tenants = db.scalar(
        select(func.count()).select_from(Tenant).where(
            Tenant.is_deleted.is_(False), Tenant.status.in_(["active", "trial"])
        )
    ) or 0
    live_bots = db.scalar(
        select(func.count()).select_from(VoiceBot).where(
            VoiceBot.is_deleted.is_(False), VoiceBot.status == "published"
        )
    ) or 0
    total_mrr = float(
        db.scalar(
            select(func.coalesce(func.sum(Subscription.mrr), 0)).where(
                Subscription.is_deleted.is_(False), Subscription.status == "active"
            )
        ) or 0
    )
    calls_30d = int(_sum(daily, "calls"))
    prev_calls = int(_sum(prev, "calls"))
    ai_cost = _ai_cost_sum(daily)
    prev_ai = _ai_cost_sum(prev)
    calls_spark = [daily.get(d, {}).get("calls", 0) for d in dates][-14:]

    return ok({
        "kpis": [
            _kpi("Active tenants", str(active_tenants), None, [], "up-good"),
            _kpi("Live VoiceBots", str(live_bots), None, [], "up-good"),
            _kpi("Calls (30d)", f"{calls_30d:,}", _delta(calls_30d, prev_calls),
                 calls_spark, "up-good"),
            _kpi("MRR", f"${total_mrr:,.0f}", None, [], "up-good"),
            _kpi("AI cost (30d)", f"${ai_cost:,.0f}", _delta(ai_cost, prev_ai),
                 [round(daily.get(d, {}).get("llm", 0)) for d in dates][-14:], "down-good"),
        ],
        "activeTenants": active_tenants,
        "liveBots": live_bots,
    })
