"""Authorized operational exports and non-tabular billing downloads."""

import io
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    has_permission,
    is_super_admin,
    require_permission,
)
from backend.reports.exporter import (
    CSV_CONTENT_TYPE,
    XLSX_CONTENT_TYPE,
    render_csv,
    render_xlsx,
    safe_filename,
)
from backend.reports.invoice_pdf import render_invoice_pdf
from backend.reports.operational import (
    CONVERSATION_SENTIMENTS,
    INVOICE_STATUSES,
    OPERATIONAL_EXPORT_REGISTRY,
    SUBSCRIPTION_STATUSES,
    build_conversations_export,
    build_invoices_export,
    build_subscriptions_export,
    build_transcript_export,
)
from shared.db.mongo import Mongo
from shared.db.mysql import get_db
from shared.errors import ApiError, ForbiddenError, NotFoundError
from shared.models import ConversationSession, Invoice, Plan, Tenant, User, VoiceBot

router = APIRouter(tags=["Exports"])


def _validate_format(export_format: str) -> None:
    if export_format not in {"csv", "xlsx"}:
        raise ApiError(
            f"Unsupported export format '{export_format}'.",
            422,
            errors=[{"field": "format", "message": "Choose csv or xlsx."}],
        )


def _validate_choice(
    value: str | None,
    *,
    field: str,
    allowed: frozenset[str],
) -> None:
    if value is not None and value not in allowed:
        raise ApiError(
            f"Unsupported {field} '{value}'.",
            422,
            errors=[
                {
                    "field": field,
                    "message": f"Choose one of: {', '.join(sorted(allowed))}.",
                }
            ],
        )


def _reject_filter(export_type: str, field: str, value: object) -> None:
    if value is not None:
        raise ApiError(
            f"The {field} filter is not supported for {export_type}.",
            422,
            errors=[{"field": field, "message": "Remove this filter."}],
        )


def _conversation_tenant(
    db: Session,
    user: User,
    requested_tenant_id: str | None,
) -> str:
    if is_super_admin(user):
        if not requested_tenant_id:
            raise ApiError(
                "tenantId is required for a platform conversation export.",
                422,
                errors=[{"field": "tenantId", "message": "Choose a tenant."}],
            )
        tenant_id = requested_tenant_id
    else:
        if requested_tenant_id and requested_tenant_id != user.tenant_id:
            raise ForbiddenError("You cannot export another tenant's conversations.")
        if not user.tenant_id:
            raise ForbiddenError("Your account is not linked to a tenant.")
        tenant_id = user.tenant_id
    tenant = db.scalar(
        select(Tenant).where(Tenant.id == tenant_id, Tenant.is_deleted.is_(False))
    )
    if tenant is None:
        raise NotFoundError("Tenant")
    return tenant_id


def _file_response(report, export_format: str, filename_stem: str):
    if export_format == "csv":
        content = render_csv(report)
        content_type = CSV_CONTENT_TYPE
    else:
        content = render_xlsx(report)
        content_type = XLSX_CONTENT_TYPE
    filename = f"{safe_filename(filename_stem)}.{export_format}"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(content)),
        "X-Content-Type-Options": "nosniff",
    }
    return StreamingResponse(
        io.BytesIO(content),
        media_type=content_type,
        headers=headers,
    )


@router.get("/exports/{export_type}")
def export_operational_data(
    export_type: str,
    request: Request,
    export_format: str = Query("csv", alias="format", max_length=8),
    search: str | None = Query(None, max_length=200),
    status: str | None = Query(None, max_length=30),
    plan: str | None = Query(None, max_length=50),
    tenant_id: str | None = Query(None, alias="tenantId", max_length=40),
    bot_id: str | None = Query(None, alias="botId", max_length=40),
    sentiment: str | None = Query(None, max_length=20),
    contained: bool | None = Query(None),
    flagged: bool | None = Query(None),
    user: User = Depends(require_permission("billing.manage", "conversations.view")),
    db: Session = Depends(get_db),
):
    definition = OPERATIONAL_EXPORT_REGISTRY.get(export_type)
    if definition is None:
        raise ApiError(
            f"Unsupported export type '{export_type}'.",
            404,
            errors=[{"field": "exportType", "message": "Choose a registered export."}],
        )
    _validate_format(export_format)
    canonical_search = search.strip() if search and search.strip() else None

    effective_tenant_id: str | None = None
    audit_filters: dict[str, object] = {
        "hasSearch": canonical_search is not None,
    }
    if export_type == "subscriptions":
        if not is_super_admin(user) or not has_permission(user, "billing.manage"):
            raise ForbiddenError("Only platform billing administrators can export subscriptions.")
        _validate_choice(status, field="status", allowed=SUBSCRIPTION_STATUSES)
        for field, value in (
            ("tenantId", tenant_id),
            ("botId", bot_id),
            ("sentiment", sentiment),
            ("contained", contained),
            ("flagged", flagged),
        ):
            _reject_filter(export_type, field, value)
        if plan:
            known_plan = db.scalar(
                select(Plan.id).where(
                    Plan.code == plan,
                    Plan.is_deleted.is_(False),
                )
            )
            if known_plan is None:
                raise ApiError(
                    f"Unknown plan '{plan}'.",
                    422,
                    errors=[{"field": "plan", "message": "Choose an existing plan."}],
                )
        report = build_subscriptions_export(
            db,
            search=canonical_search,
            status=status,
            plan=plan,
        )
        audit_filters.update({"status": status, "plan": plan})
    elif export_type == "invoices":
        if not is_super_admin(user) or not has_permission(user, "billing.manage"):
            raise ForbiddenError("Only platform billing administrators can export invoices.")
        _validate_choice(status, field="status", allowed=INVOICE_STATUSES)
        for field, value in (
            ("plan", plan),
            ("tenantId", tenant_id),
            ("botId", bot_id),
            ("sentiment", sentiment),
            ("contained", contained),
            ("flagged", flagged),
        ):
            _reject_filter(export_type, field, value)
        report = build_invoices_export(
            db,
            search=canonical_search,
            status=status,
        )
        audit_filters.update({"status": status})
    else:
        if not has_permission(user, "conversations.view"):
            raise ForbiddenError()
        _reject_filter(export_type, "plan", plan)
        _reject_filter(export_type, "status", status)
        _validate_choice(
            sentiment,
            field="sentiment",
            allowed=CONVERSATION_SENTIMENTS,
        )
        effective_tenant_id = _conversation_tenant(db, user, tenant_id)
        if bot_id:
            bot = db.scalar(
                select(VoiceBot).where(
                    VoiceBot.id == bot_id,
                    VoiceBot.tenant_id == effective_tenant_id,
                    VoiceBot.is_deleted.is_(False),
                )
            )
            if bot is None:
                raise NotFoundError("VoiceBot")
        report = build_conversations_export(
            db,
            tenant_id=effective_tenant_id,
            search=canonical_search,
            bot_id=bot_id,
            sentiment=sentiment,
            contained=contained,
            flagged=flagged,
        )
        audit_filters.update(
            {
                "botId": bot_id,
                "sentiment": sentiment,
                "contained": contained,
                "flagged": flagged,
            }
        )

    today = date.today().isoformat()
    record_audit(
        db,
        user=user,
        action="data.export",
        entity_type="export",
        entity_id=export_type,
        target_label=definition.name,
        tenant_id=effective_tenant_id,
        new_value={
            "format": export_format,
            "filters": audit_filters,
            "tenantScope": effective_tenant_id,
            "rowCount": len(report.rows),
        },
        request=request,
    )
    db.commit()
    return _file_response(
        report,
        export_format,
        f"echosphere-{export_type}-{today}",
    )


@router.get("/conversations/{conversation_id}/transcript/export")
async def export_conversation_transcript(
    conversation_id: str,
    request: Request,
    export_format: str = Query("csv", alias="format", max_length=8),
    user: User = Depends(require_permission("conversations.view")),
    db: Session = Depends(get_db),
):
    _validate_format(export_format)
    row = db.execute(
        select(ConversationSession, VoiceBot.name)
        .join(VoiceBot, VoiceBot.id == ConversationSession.bot_id)
        .where(
            ConversationSession.id == conversation_id,
            ConversationSession.is_deleted.is_(False),
            VoiceBot.is_deleted.is_(False),
        )
    ).first()
    if row is None:
        raise NotFoundError("Conversation")
    conversation, bot_name = row
    assert_tenant_access(user, conversation.tenant_id)
    transcript_doc = await Mongo.transcripts().find_one(
        {
            "session_id": conversation.id,
            "tenant_id": conversation.tenant_id,
        }
    )
    report = build_transcript_export((transcript_doc or {}).get("turns", []))
    record_audit(
        db,
        user=user,
        action="conversation.transcript.export",
        entity_type="conversation",
        entity_id=conversation.id,
        target_label=f"{bot_name} · {conversation.id}",
        tenant_id=conversation.tenant_id,
        new_value={
            "format": export_format,
            "rowCount": len(report.rows),
        },
        request=request,
    )
    db.commit()
    return _file_response(
        report,
        export_format,
        f"echosphere-transcript-{conversation.id}-{date.today().isoformat()}",
    )


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: str,
    request: Request,
    user: User = Depends(require_permission("billing.manage")),
    db: Session = Depends(get_db),
):
    if not is_super_admin(user):
        raise ForbiddenError("Only platform billing administrators can download invoices.")
    row = db.execute(
        select(Invoice, Tenant.name)
        .join(Tenant, Tenant.id == Invoice.tenant_id)
        .where(
            Invoice.id == invoice_id,
            Invoice.is_deleted.is_(False),
            Tenant.is_deleted.is_(False),
        )
    ).first()
    if row is None:
        raise NotFoundError("Invoice")
    invoice, tenant_name = row
    content = render_invoice_pdf(
        invoice_id=invoice.id,
        tenant_name=tenant_name,
        period=invoice.period,
        amount=invoice.amount,
        status=invoice.status,
        issued_at=invoice.issued_at,
    )
    filename = (
        safe_filename(
            f"echosphere-invoice-{invoice.id}-{date.today().isoformat()}"
        )
        + ".pdf"
    )
    record_audit(
        db,
        user=user,
        action="invoice.pdf.download",
        entity_type="invoice",
        entity_id=invoice.id,
        target_label=invoice.id,
        tenant_id=invoice.tenant_id,
        new_value={"format": "pdf"},
        request=request,
    )
    db.commit()
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
            "X-Content-Type-Options": "nosniff",
        },
    )
