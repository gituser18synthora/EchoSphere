"""Compliance-policy management (Super Admin).

Policies are versioned rows that move through ``draft → approved → active →
retired``. Only ACTIVE policies enforce at runtime. Activation is the
compliance-owner sign-off moment: it stamps ``approved_by``/``approved_at``
and requires an approval note — the platform never silently turns a draft
legal interpretation into production enforcement. Editing is draft-only;
correcting an approved/active policy means creating the next version.
Wordings are immutable: create-only, versioned per (code, language).
"""

from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import require_super_admin
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from shared.models import CompliancePolicy, ComplianceWording, Tenant, User

router = APIRouter(tags=["Compliance"])

_STATUS_FLOW = {
    "draft": {"approved", "retired"},
    "approved": {"active", "draft", "retired"},
    "active": {"retired"},
    "retired": set(),
}


def serialize_policy(p: CompliancePolicy) -> dict:
    return {
        "id": p.id,
        "tenantId": p.tenant_id or "",
        "code": p.code,
        "version": p.version,
        "name": p.name,
        "description": p.description or "",
        "jurisdiction": p.jurisdiction or "",
        "regulator": p.regulator or "",
        "status": p.status,
        "effectiveDate": p.effective_date.isoformat() if p.effective_date else None,
        "appliesTo": p.applies_to or {},
        "timezone": p.timezone,
        "callingWindows": p.calling_windows or [],
        "contactLimits": p.contact_limits or {},
        "prohibitedConduct": p.prohibited_conduct or [],
        "waiverRules": p.waiver_rules or {},
        "escalationRules": p.escalation_rules or {},
        "sources": p.sources or [],
        "approvedBy": p.approved_by or "",
        "approvedAt": p.approved_at.isoformat() + "Z" if p.approved_at else None,
        "approvalNote": p.approval_note or "",
        "wordings": [
            {"id": w.id, "code": w.code, "language": w.language,
             "version": w.version, "text": w.text, "exact": bool(w.exact)}
            for w in (p.wordings or [])
        ],
        "createdAt": p.created_at.isoformat() + "Z" if p.created_at else None,
        "updatedAt": p.updated_at.isoformat() + "Z" if p.updated_at else None,
    }


def _policy_or_404(db: Session, policy_id: str) -> CompliancePolicy:
    row = db.get(CompliancePolicy, policy_id)
    if row is None or row.is_deleted:
        raise NotFoundError("Compliance policy")
    return row


@router.get("/compliance-policies")
def list_policies(
    tenant_id: str | None = Query(None, alias="tenantId"),
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    stmt = select(CompliancePolicy).where(CompliancePolicy.is_deleted.is_(False))
    if tenant_id:
        stmt = stmt.where(CompliancePolicy.tenant_id == tenant_id)
    rows = db.scalars(stmt.order_by(
        CompliancePolicy.code, CompliancePolicy.version
    )).all()
    return ok([serialize_policy(p) for p in rows])


class PolicyRequest(BaseModel):
    tenant_id: str | None = Field(default=None, alias="tenantId")
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    jurisdiction: str | None = Field(default=None, max_length=10)
    regulator: str | None = Field(default=None, max_length=40)
    effective_date: date | None = Field(default=None, alias="effectiveDate")
    applies_to: dict | None = Field(default=None, alias="appliesTo")
    timezone_name: str = Field(default="UTC", alias="timezone", max_length=64)
    calling_windows: list | None = Field(default=None, alias="callingWindows")
    contact_limits: dict | None = Field(default=None, alias="contactLimits")
    prohibited_conduct: list | None = Field(default=None, alias="prohibitedConduct")
    waiver_rules: dict | None = Field(default=None, alias="waiverRules")
    escalation_rules: dict | None = Field(default=None, alias="escalationRules")
    sources: list | None = None

    model_config = {"populate_by_name": True}


def _validate_windows(windows: list | None) -> None:
    from shared.compliance.calling_hours import _parse_hhmm

    for window in windows or []:
        if not isinstance(window, dict):
            raise ApiError("Each calling window must be an object.", 422)
        if _parse_hhmm(window.get("start", "")) is None or \
                _parse_hhmm(window.get("end", "")) is None:
            raise ApiError(
                "Calling windows need 'start' and 'end' as HH:MM.", 422
            )
        days = window.get("days")
        if days is not None and (
            not isinstance(days, list)
            or any(not isinstance(d, int) or d < 0 or d > 6 for d in days)
        ):
            raise ApiError("Window 'days' must be integers 0 (Mon) – 6 (Sun).", 422)


def _validate_timezone(name: str) -> None:
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"Unknown IANA timezone '{name}'.", 422) from exc


def _apply_policy_fields(row: CompliancePolicy, body: PolicyRequest) -> None:
    row.name = body.name.strip()
    row.description = body.description
    row.jurisdiction = body.jurisdiction
    row.regulator = body.regulator
    row.effective_date = body.effective_date
    row.applies_to = body.applies_to
    row.timezone = body.timezone_name
    row.calling_windows = body.calling_windows
    row.contact_limits = body.contact_limits
    row.prohibited_conduct = body.prohibited_conduct
    row.waiver_rules = body.waiver_rules
    row.escalation_rules = body.escalation_rules
    row.sources = body.sources


@router.post("/compliance-policies", status_code=201)
def create_policy(
    body: PolicyRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if body.tenant_id:
        tenant = db.get(Tenant, body.tenant_id)
        if tenant is None or tenant.is_deleted:
            raise ApiError("Unknown tenant.", 422)
    _validate_timezone(body.timezone_name)
    _validate_windows(body.calling_windows)
    code = body.code.strip().lower().replace(" ", "_")
    latest = db.scalar(
        select(func.max(CompliancePolicy.version)).where(
            CompliancePolicy.tenant_id == body.tenant_id,
            CompliancePolicy.code == code,
        )
    ) or 0
    row = CompliancePolicy(
        id=new_id("cp"), tenant_id=body.tenant_id, code=code,
        version=latest + 1, status="draft", created_by=user.id,
    )
    _apply_policy_fields(row, body)
    db.add(row)
    record_audit(
        db, user=user, action="Created compliance policy (draft)",
        entity_type="compliance_policy", entity_id=row.id,
        target_label=f"{code} v{row.version}", tenant_id=body.tenant_id,
        new_value={"code": code, "version": row.version,
                   "regulator": row.regulator, "status": "draft"},
        request=request,
    )
    db.commit()
    return ok(serialize_policy(row))


@router.patch("/compliance-policies/{policy_id}")
def update_policy(
    policy_id: str,
    body: PolicyRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = _policy_or_404(db, policy_id)
    if row.status != "draft":
        raise ApiError(
            "Only draft policies can be edited — approved/active policies are "
            "immutable; create the next version instead.", 409,
        )
    _validate_timezone(body.timezone_name)
    _validate_windows(body.calling_windows)
    _apply_policy_fields(row, body)
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Updated compliance policy draft",
        entity_type="compliance_policy", entity_id=row.id,
        target_label=f"{row.code} v{row.version}", tenant_id=row.tenant_id,
        request=request,
    )
    db.commit()
    return ok(serialize_policy(row))


class PolicyStatusRequest(BaseModel):
    status: str = Field(pattern="^(draft|approved|active|retired)$")
    # Mandatory when approving/activating: the compliance owner's sign-off.
    approval_note: str | None = Field(
        default=None, alias="approvalNote", max_length=500
    )

    model_config = {"populate_by_name": True}


@router.post("/compliance-policies/{policy_id}/status")
def set_policy_status(
    policy_id: str,
    body: PolicyStatusRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    row = _policy_or_404(db, policy_id)
    if body.status == row.status:
        return ok(serialize_policy(row))
    if body.status not in _STATUS_FLOW.get(row.status, set()):
        raise ApiError(
            f"A {row.status} policy cannot move to {body.status}.", 409
        )
    if body.status in ("approved", "active"):
        if not (body.approval_note or "").strip():
            raise ApiError(
                "Approving or activating a policy requires an approval note "
                "from the compliance owner.", 422,
            )
        row.approved_by = user.id
        row.approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
        row.approval_note = body.approval_note.strip()
    if body.status == "active":
        # Exactly one active version per (tenant, code): the previous active
        # version retires in the same transaction.
        for other in db.scalars(select(CompliancePolicy).where(
            CompliancePolicy.tenant_id == row.tenant_id,
            CompliancePolicy.code == row.code,
            CompliancePolicy.status == "active",
            CompliancePolicy.id != row.id,
        )):
            other.status = "retired"
    before = {"status": row.status}
    row.status = body.status
    row.updated_by = user.id
    record_audit(
        db, user=user, action=f"Compliance policy {body.status}",
        entity_type="compliance_policy", entity_id=row.id,
        target_label=f"{row.code} v{row.version}", tenant_id=row.tenant_id,
        previous_value=before,
        new_value={"status": row.status, "approvalNote": row.approval_note},
        request=request,
    )
    db.commit()
    return ok(serialize_policy(row))


class WordingRequest(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    language: str = Field(default="en", max_length=15)
    text: str = Field(min_length=1)
    exact: bool = True


@router.post("/compliance-policies/{policy_id}/wordings", status_code=201)
def add_wording(
    policy_id: str,
    body: WordingRequest,
    request: Request,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Wordings are immutable — this always CREATES the next version for the
    (code, language); there is no update or delete path."""
    row = _policy_or_404(db, policy_id)
    code = body.code.strip().lower().replace(" ", "_")
    latest = db.scalar(
        select(func.max(ComplianceWording.version)).where(
            ComplianceWording.policy_id == row.id,
            ComplianceWording.code == code,
            ComplianceWording.language == body.language,
        )
    ) or 0
    wording = ComplianceWording(
        id=new_id("cw"), policy_id=row.id, code=code, language=body.language,
        version=latest + 1, text=body.text, exact=body.exact,
        created_by=user.id,
    )
    db.add(wording)
    record_audit(
        db, user=user, action="Added compliance wording version",
        entity_type="compliance_policy", entity_id=row.id,
        target_label=f"{row.code} · {code} v{wording.version} ({body.language})",
        tenant_id=row.tenant_id,
        new_value={"wording": code, "version": wording.version,
                   "language": body.language, "exact": body.exact},
        request=request,
    )
    db.commit()
    db.refresh(row)  # the selectin-loaded wordings list must include the new row
    return ok(serialize_policy(row))
