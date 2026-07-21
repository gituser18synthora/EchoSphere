"""Integrations: platform catalog + per-tenant connection state."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import get_current_user, require_tenant_admin, resolve_tenant_id
from shared.errors import NotFoundError
from shared.ids import new_id
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.models import Integration, TenantIntegration, User
from backend.serializers import serialize_integration

router = APIRouter(tags=["Integrations"])


@router.get("/integrations")
def list_integrations(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, tenant_id)
    catalog = db.scalars(
        select(Integration)
        .where(Integration.is_deleted.is_(False))
        .order_by(Integration.created_at.asc())
    ).all()
    states = {
        ti.integration_id: ti
        for ti in db.scalars(
            select(TenantIntegration).where(
                TenantIntegration.tenant_id == tid,
                TenantIntegration.is_deleted.is_(False),
            )
        ).all()
    }
    out = []
    for item in catalog:
        st = states.get(item.id)
        out.append(
            serialize_integration(
                item,
                status=st.status if st else "available",
                connected_at=st.connected_at if st else None,
            )
        )
    return ok(out)


class ConnectRequest(BaseModel):
    config: dict | None = None


@router.post("/integrations/{integration_id}/connect")
def connect_integration(
    integration_id: str,
    body: ConnectRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, None)
    item = db.get(Integration, integration_id)
    if item is None or item.is_deleted:
        raise NotFoundError("Integration")
    st = db.scalar(
        select(TenantIntegration).where(
            TenantIntegration.tenant_id == tid,
            TenantIntegration.integration_id == item.id,
        )
    )
    if st is None:
        st = TenantIntegration(
            id=new_id("ti"), tenant_id=tid, integration_id=item.id, created_by=user.id
        )
        db.add(st)
    st.status = "connected"
    st.is_deleted = False
    st.connected_at = datetime.now(timezone.utc)
    if body.config is not None:
        st.config = body.config
    st.updated_by = user.id
    record_audit(
        db, user=user, action="Connected integration", entity_type="integration",
        entity_id=item.id, target_label=item.name, tenant_id=tid, request=request,
    )
    db.commit()
    return ok(serialize_integration(item, status=st.status, connected_at=st.connected_at))


@router.post("/integrations/{integration_id}/disconnect")
def disconnect_integration(
    integration_id: str,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    tid = resolve_tenant_id(user, None)
    item = db.get(Integration, integration_id)
    if item is None or item.is_deleted:
        raise NotFoundError("Integration")
    st = db.scalar(
        select(TenantIntegration).where(
            TenantIntegration.tenant_id == tid,
            TenantIntegration.integration_id == item.id,
            TenantIntegration.is_deleted.is_(False),
        )
    )
    if st is not None:
        st.status = "available"
        st.connected_at = None
        st.updated_by = user.id
    record_audit(
        db, user=user, action="Disconnected integration", entity_type="integration",
        entity_id=item.id, target_label=item.name, tenant_id=tid, request=request,
    )
    db.commit()
    return ok(serialize_integration(item, status="available", connected_at=None))
