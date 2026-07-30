"""Authorized platform and tenant report downloads."""

import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import is_super_admin, require_permission
from backend.reports.exporter import (
    CSV_CONTENT_TYPE,
    XLSX_CONTENT_TYPE,
    render_csv,
    render_xlsx,
    safe_filename,
)
from backend.reports.registry import REPORT_REGISTRY, build_report
from shared.db.mysql import get_db
from shared.errors import ApiError, ForbiddenError, NotFoundError
from shared.models import Tenant, User, VoiceBot

router = APIRouter(tags=["Reports"])


def _effective_scope(
    db: Session,
    user: User,
    requested_tenant_id: str | None,
    bot_id: str | None,
) -> tuple[str | None, str | None]:
    if is_super_admin(user):
        tenant_id = requested_tenant_id
    else:
        if requested_tenant_id and requested_tenant_id != user.tenant_id:
            raise ForbiddenError("You cannot export another tenant's data.")
        if not user.tenant_id:
            raise ForbiddenError("Your account is not linked to a tenant.")
        tenant_id = user.tenant_id

    if tenant_id:
        tenant = db.scalar(
            select(Tenant).where(
                Tenant.id == tenant_id,
                Tenant.is_deleted.is_(False),
            )
        )
        if tenant is None:
            raise NotFoundError("Tenant")

    if bot_id:
        bot = db.scalar(
            select(VoiceBot).where(
                VoiceBot.id == bot_id,
                VoiceBot.is_deleted.is_(False),
            )
        )
        if bot is None:
            raise NotFoundError("VoiceBot")
        if tenant_id is not None and bot.tenant_id != tenant_id:
            raise NotFoundError("VoiceBot")
        tenant_id = bot.tenant_id

    return tenant_id, bot_id


@router.get("/reports/{report_type}/export")
def export_report(
    report_type: str,
    request: Request,
    export_format: str = Query("csv", alias="format"),
    days: int = Query(30, ge=7, le=90),
    tenant_id: str | None = Query(None, alias="tenantId", max_length=40),
    bot_id: str | None = Query(None, alias="botId", max_length=40),
    user: User = Depends(require_permission("analytics.view")),
    db: Session = Depends(get_db),
):
    definition = REPORT_REGISTRY.get(report_type)
    if definition is None:
        raise ApiError(
            f"Unsupported report type '{report_type}'.",
            404,
            errors=[{"field": "reportType", "message": "Choose a registered report."}],
        )
    if export_format not in {"csv", "xlsx"}:
        raise ApiError(
            f"Unsupported export format '{export_format}'.",
            422,
            errors=[{"field": "format", "message": "Choose csv or xlsx."}],
        )
    if definition.platform_only and not is_super_admin(user):
        raise ForbiddenError("This report is available only to platform administrators.")
    if bot_id and not definition.supports_bot_filter:
        raise ApiError(
            "This report does not support a VoiceBot filter.",
            422,
            errors=[{"field": "botId", "message": "Remove the VoiceBot filter."}],
        )

    effective_tenant_id, effective_bot_id = _effective_scope(
        db, user, tenant_id, bot_id
    )
    end = date.today()
    start = end - timedelta(days=days - 1)
    report = build_report(
        db,
        report_type,
        start=start,
        end=end,
        tenant_id=effective_tenant_id,
        bot_id=effective_bot_id,
    )

    if export_format == "csv":
        content = render_csv(report)
        content_type = CSV_CONTENT_TYPE
    else:
        content = render_xlsx(report)
        content_type = XLSX_CONTENT_TYPE

    filename = safe_filename(
        f"echosphere-{report_type.replace('_', '-')}-{start.isoformat()}-to-{end.isoformat()}"
    ) + f".{export_format}"
    record_audit(
        db,
        user=user,
        action="report.export",
        entity_type="report",
        entity_id=report_type,
        target_label=definition.name,
        tenant_id=effective_tenant_id,
        new_value={
            "format": export_format,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "tenantId": effective_tenant_id,
            "botId": effective_bot_id,
            "rowCount": len(report.rows),
        },
        request=request,
    )
    db.commit()

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(content)),
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(io.BytesIO(content), media_type=content_type, headers=headers)
