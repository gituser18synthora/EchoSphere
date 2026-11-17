"""Searchable, tenant-scoped audit history and bounded, masked exports."""
import csv
import io
import json
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import _sanitize, record_audit
from backend.core.deps import is_super_admin, require_tenant_admin
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.serializers import serialize_audit
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.models import AuditLog, Tenant, User

router = APIRouter(tags=["Audit"])
EXPORT_LIMIT = 5000


def _date_bound(value: str | None, *, end: bool = False):
    if not value:
        return None
    try:
        if len(value) == 10:
            result = datetime.combine(date.fromisoformat(value), datetime.min.time())
            return result + timedelta(days=1) if end else result
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("A timezone is required")
        return result.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise ApiError("Use a valid UTC date or a timestamp with a timezone.", 422) from None


def audit_filters(
    search: str | None = Query(None, max_length=200),
    entity_type: str | None = Query(None, alias="entityType", max_length=50),
    actor: str | None = Query(None, max_length=150),
    action: str | None = Query(None, max_length=200),
    tenant_id: str | None = Query(None, alias="tenantId", max_length=40),
    date_from: str | None = Query(None, alias="dateFrom", max_length=64),
    date_to: str | None = Query(None, alias="dateTo", max_length=64),
):
    lower, upper = _date_bound(date_from), _date_bound(date_to, end=True)
    if lower and upper and lower >= upper:
        raise ApiError("The start date must be before the end date.", 422)
    return dict(search=search, entity_type=entity_type, actor=actor, action=action,
                tenant_id=tenant_id, lower=lower, upper=upper)


def _query(user: User, filters: dict):
    stmt = select(AuditLog)
    if not is_super_admin(user):
        if filters["tenant_id"] and filters["tenant_id"] != user.tenant_id:
            raise NotFoundError("Organization")
        stmt = stmt.where(AuditLog.tenant_id == user.tenant_id)
    elif filters["tenant_id"]:
        value = filters["tenant_id"]
        stmt = stmt.where(AuditLog.tenant_id.is_(None) if value == "__platform__" else AuditLog.tenant_id == value)
    for name, column in (("entity_type", AuditLog.entity_type),):
        if filters[name]:
            stmt = stmt.where(column == filters[name])
    for name, column in (("actor", AuditLog.actor_name), ("action", AuditLog.action)):
        if filters[name]:
            stmt = stmt.where(column.contains(filters[name], autoescape=True))
    if filters["search"]:
        value = filters["search"]
        stmt = stmt.where(or_(*(column.contains(value, autoescape=True) for column in
                               (AuditLog.action, AuditLog.actor_name, AuditLog.target_label))))
    if filters["lower"]:
        stmt = stmt.where(AuditLog.created_at >= filters["lower"])
    if filters["upper"]:
        stmt = stmt.where(AuditLog.created_at < filters["upper"])
    return stmt


def _names(db, rows):
    ids = {row.tenant_id for row in rows if row.tenant_id}
    return dict(db.execute(select(Tenant.id, Tenant.name).where(Tenant.id.in_(ids))).all()) if ids else {}


@router.get("/audit")
def list_audit(params: PageParams = Depends(page_params), filters: dict = Depends(audit_filters),
               user: User = Depends(require_tenant_admin), db: Session = Depends(get_db)):
    stmt = _query(user, filters)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.scalars(stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                           .offset(params.offset).limit(params.page_size)).all())
    names = _names(db, rows)
    return paginated([serialize_audit(row, tenant_name=names.get(row.tenant_id)) for row in rows],
                     page=params.page, page_size=params.page_size, total=total)


def _csv_cell(value):
    value = "" if value is None else str(value)
    probe = re.sub(r"^[\s\x00-\x1f\ufeff]*", "", value)
    if value.startswith(("\t", "\r", "\n")) or probe.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@router.get("/audit/export")
def export_audit(request: Request, filters: dict = Depends(audit_filters),
                 user: User = Depends(require_tenant_admin), db: Session = Depends(get_db)):
    rows = list(db.scalars(_query(user, filters).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(EXPORT_LIMIT + 1)).all())
    if len(rows) > EXPORT_LIMIT:
        raise ApiError(f"This export exceeds {EXPORT_LIMIT} events. Narrow the date range or filters.", 422)
    names = _names(db, rows)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Time (UTC)", "Actor", "Role", "Action", "Target", "Organization", "IP", "Before", "After"])
    for row in rows:
        item = serialize_audit(row, tenant_name=names.get(row.tenant_id))
        writer.writerow([_csv_cell(v) for v in (item["time"], item["actor"], item["actorRole"], item["action"],
            item["target"], item["tenant"] or "Platform", item["ip"],
            json.dumps(_sanitize(row.previous_value), ensure_ascii=False), json.dumps(_sanitize(row.new_value), ensure_ascii=False))])
    recorded_filters = {key: (value.isoformat() if isinstance(value, datetime) else value)
                        for key, value in filters.items() if value is not None}
    record_audit(db, user=user, action="Exported audit history", entity_type="audit",
                 new_value={"rowCount": len(rows), "filters": recorded_filters}, request=request)
    db.commit()
    return Response("\ufeff" + output.getvalue(), media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="echosphere-audit.csv"', "Cache-Control": "no-store"})


@router.get("/audit/{audit_id}")
def audit_detail(audit_id: str, user: User = Depends(require_tenant_admin), db: Session = Depends(get_db)):
    row = db.get(AuditLog, audit_id)
    if row is None or (not is_super_admin(user) and row.tenant_id != user.tenant_id):
        raise NotFoundError("Audit event")
    names = _names(db, [row])
    return ok({**serialize_audit(row, tenant_name=names.get(row.tenant_id)),
               "userId": row.user_id, "tenantId": row.tenant_id,
               "previousValue": _sanitize(row.previous_value), "newValue": _sanitize(row.new_value)})
