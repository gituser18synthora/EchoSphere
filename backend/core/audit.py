"""Audit-trail writer. Values are sanitized — secrets never reach the log."""

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from shared.ids import new_id
from shared.models import AuditLog, User

_SENSITIVE_KEYS = {
    "password", "password_hash", "secret", "api_key", "apikey", "token",
    "access_token", "refresh_token", "authorization", "credential", "private_key",
}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in k.lower() for s in _SENSITIVE_KEYS) else _sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def record_audit(
    db: Session,
    *,
    user: User | None,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    target_label: str | None = None,
    tenant_id: str | None = None,
    previous_value: dict | None = None,
    new_value: dict | None = None,
    request: Request | None = None,
) -> None:
    """Queue an audit row on the caller's transaction (committed with it)."""
    db.add(
        AuditLog(
            id=new_id("au"),
            tenant_id=tenant_id or (user.tenant_id if user else None),
            user_id=user.id if user else None,
            actor_name=user.name if user else "System",
            actor_role=user.role.code if user else None,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            target_label=target_label,
            previous_value=_sanitize(previous_value) if previous_value else None,
            new_value=_sanitize(new_value) if new_value else None,
            ip_address=client_ip(request),
        )
    )
