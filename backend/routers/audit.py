"""Audit trail listing (Super Admin: all; Tenant Admin: own tenant)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.deps import is_super_admin, require_tenant_admin
from backend.core.pagination import PageParams, page_params
from backend.core.responses import paginated
from backend.db.mysql import get_db
from backend.models import AuditLog, Tenant, User
from backend.serializers import serialize_audit

router = APIRouter(tags=["Audit"])


@router.get("/audit")
def list_audit(
    params: PageParams = Depends(page_params),
    entity_type: str | None = Query(None, alias="entityType"),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    stmt = select(AuditLog)
    if not is_super_admin(user):
        stmt = stmt.where(AuditLog.tenant_id == user.tenant_id)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(
            or_(
                AuditLog.action.like(like),
                AuditLog.actor_name.like(like),
                AuditLog.target_label.like(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AuditLog.created_at.desc()).offset(params.offset).limit(params.page_size)
    ).all()
    tenant_names = dict(db.execute(select(Tenant.id, Tenant.name)).all())
    return paginated(
        [serialize_audit(a, tenant_name=tenant_names.get(a.tenant_id)) for a in rows],
        page=params.page, page_size=params.page_size, total=total,
    )
