"""Plans, subscriptions, invoices (Super Admin)."""

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from backend.core.deps import require_super_admin
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from shared.db.mysql import get_db
from shared.models import Invoice, Plan, Subscription, Tenant, UsageRecord, User
from backend.serializers import serialize_invoice, serialize_plan, serialize_subscription

router = APIRouter(tags=["Billing"])


@router.get("/plans")
def list_plans(user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    plans = db.scalars(select(Plan).order_by(Plan.price_monthly.asc())).all()
    return ok([serialize_plan(p) for p in plans])


@router.get("/subscriptions")
def list_subscriptions(
    params: PageParams = Depends(page_params),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    today = date.today()
    stmt = (
        select(Subscription, Tenant.name, Plan.code)
        .join(Tenant, Tenant.id == Subscription.tenant_id)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(Subscription.is_deleted.is_(False), Tenant.is_deleted.is_(False))
    )
    if params.search:
        stmt = stmt.where(Tenant.name.like(f"%{params.search}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Subscription.created_at.asc()).offset(params.offset).limit(params.page_size)
    ).all()

    tenant_ids = [s.tenant_id for s, _, _ in rows]
    minutes = {}
    if tenant_ids:
        minutes = dict(
            db.execute(
                select(UsageRecord.tenant_id, func.coalesce(func.sum(UsageRecord.minutes), 0))
                .where(
                    UsageRecord.tenant_id.in_(tenant_ids),
                    UsageRecord.bot_id.is_(None),
                    extract("year", UsageRecord.date) == today.year,
                    extract("month", UsageRecord.date) == today.month,
                )
                .group_by(UsageRecord.tenant_id)
            ).all()
        )
    return paginated(
        [
            serialize_subscription(
                s, tenant_name=tname, plan_code=pcode,
                minutes_used=float(minutes.get(s.tenant_id, 0)),
            )
            for s, tname, pcode in rows
        ],
        page=params.page, page_size=params.page_size, total=total,
    )


@router.get("/invoices")
def list_invoices(
    params: PageParams = Depends(page_params),
    status: str | None = Query(None),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Invoice, Tenant.name)
        .join(Tenant, Tenant.id == Invoice.tenant_id)
        .where(Invoice.is_deleted.is_(False))
    )
    if status:
        stmt = stmt.where(Invoice.status == status)
    if params.search:
        stmt = stmt.where(Tenant.name.like(f"%{params.search}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Invoice.issued_at.desc()).offset(params.offset).limit(params.page_size)
    ).all()
    return paginated(
        [serialize_invoice(i, tenant_name=tname) for i, tname in rows],
        page=params.page, page_size=params.page_size, total=total,
    )
