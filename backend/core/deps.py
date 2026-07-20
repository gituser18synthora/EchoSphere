"""Request dependencies: current user, role guards, tenant scoping.

Tenant isolation rule: the effective tenant_id ALWAYS comes from the
authenticated user's token for tenant roles. A client-supplied tenant_id is
honored only for super admins (platform scope); anyone else asking for another
tenant's data gets 403.
"""

from datetime import datetime, timezone

import jwt as pyjwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from backend.core.errors import ApiError, ForbiddenError
from backend.db.mysql import get_db
from backend.models import User

_bearer = HTTPBearer(auto_error=False)

SUPER_ADMIN = "super_admin"
TENANT_ADMIN = "tenant_admin"
TENANT_USER = "tenant_user"


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or not credentials.credentials:
        raise ApiError("Authentication required.", 401)
    from backend.core.security import decode_access_token

    try:
        payload = decode_access_token(credentials.credentials)
    except pyjwt.ExpiredSignatureError:
        raise ApiError("Session expired — please sign in again.", 401)
    except pyjwt.InvalidTokenError:
        raise ApiError("Invalid authentication token.", 401)

    user = db.get(User, payload.get("sub"))
    if user is None or user.is_deleted or user.status == "deactivated":
        raise ApiError("Account is not active.", 401)

    # Tokens issued before the last password change are no longer valid —
    # this is how "sign out other sessions" works with stateless JWTs.
    issued_at = payload.get("iat")
    if user.password_changed_at is not None and issued_at is not None:
        changed_ts = user.password_changed_at.replace(tzinfo=timezone.utc).timestamp()
        if float(issued_at) < changed_ts - 1:  # 1s clock-skew grace
            raise ApiError("Session expired — please sign in again.", 401)

    user.last_active_at = datetime.now(timezone.utc)
    db.commit()
    request.state.current_user = user
    return user


def require_roles(*role_codes: str):
    """Dependency factory: allow only the given role codes."""

    def _guard(user: User = Depends(get_current_user)) -> User:
        if user.role.code not in role_codes:
            raise ForbiddenError()
        return user

    return _guard


require_super_admin = require_roles(SUPER_ADMIN)
require_tenant_member = require_roles(SUPER_ADMIN, TENANT_ADMIN, TENANT_USER)
require_tenant_admin = require_roles(SUPER_ADMIN, TENANT_ADMIN)


def has_permission(user: User, code: str) -> bool:
    """True when the user's role carries the permission code (seeded via
    role_permissions). Super admins are granted every permission in the seed,
    so no implicit bypass is needed here."""
    return any(p.code == code for p in user.role.permissions)


def require_permission(*codes: str):
    """Dependency factory: the user's role must hold at least one of the given
    permission codes. This is the server-side enforcement — hidden frontend
    buttons are never the security boundary."""

    def _guard(user: User = Depends(get_current_user)) -> User:
        if not any(has_permission(user, c) for c in codes):
            raise ForbiddenError()
        return user

    return _guard


def is_super_admin(user: User) -> bool:
    return user.role.code == SUPER_ADMIN


def resolve_tenant_id(user: User, requested_tenant_id: str | None = None) -> str:
    """Effective tenant for a tenant-owned query. Never trusts the client for
    tenant roles; super admins may target any tenant explicitly."""
    if is_super_admin(user):
        if requested_tenant_id:
            return requested_tenant_id
        raise ApiError("tenant_id is required for platform administrators.", 400)
    if requested_tenant_id and requested_tenant_id != user.tenant_id:
        raise ForbiddenError("You cannot access another tenant's data.")
    if not user.tenant_id:
        raise ForbiddenError("Your account is not linked to a tenant.")
    return user.tenant_id


def assert_tenant_access(user: User, row_tenant_id: str | None) -> None:
    """Guard direct-by-id reads/writes of tenant-owned rows."""
    if is_super_admin(user):
        return
    if row_tenant_id is None or row_tenant_id != user.tenant_id:
        # 404, not 403 — do not leak the existence of other tenants' records.
        from backend.core.errors import NotFoundError

        raise NotFoundError()
