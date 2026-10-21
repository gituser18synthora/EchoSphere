"""Tenant API tokens — long-lived, tenant-scoped, read-only credentials.

Design (why not a second auth scheme):

- A token authenticates AS the tenant's **service account**: an ordinary
  ``users`` row flagged ``is_service_account`` with the Tenant User role. So
  every existing dependency — ``resolve_tenant_id``, ``assert_tenant_access``,
  ``require_permission``, ``can_view_costs``, audit attribution — applies to
  API calls exactly as it does to a signed-in person. Nothing in the routers
  needs to know a token exists.
- The service account cannot sign in (``/auth/login`` refuses it), has an
  unusable random password, is hidden from team listings and seat counts,
  and token requests are restricted to read methods in ``get_current_user``.
- Only the SHA-256 of the key is stored. A key carries 256 bits of entropy,
  so an unsalted digest is a sufficient one-way store (passwords need bcrypt
  because people choose them; machines do not). The plaintext is returned
  exactly once — by the call that generated it — and never logged; the audit
  log and the UI only ever see ``key_prefix``.
- Rotation revokes the active row and inserts a new one in the same
  transaction, so there is never a window with two valid tokens or none.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.security import hash_password
from shared.errors import ApiError
from shared.ids import new_id
from shared.models import Role, Tenant, TenantApiKey, User

logger = logging.getLogger(__name__)

KEY_PREFIX = "esk_"                 # EchoSphere key — tells a token from a JWT at a glance
DISPLAY_PREFIX_CHARS = 12
SERVICE_ACCOUNT_ROLE = "tenant_user"   # read permissions, no costs.view, no admin surfaces
SERVICE_ACCOUNT_DOMAIN = "service.echosphere.internal"
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def generate_api_key() -> str:
    """A fresh token: ``esk_`` + 32 random bytes, URL-safe (43 chars)."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def display_prefix(raw: str) -> str:
    return raw[:DISPLAY_PREFIX_CHARS]


def looks_like_api_key(token: str | None) -> bool:
    return bool(token) and token.startswith(KEY_PREFIX)


def service_account_email(tenant_id: str) -> str:
    return f"api-{tenant_id}@{SERVICE_ACCOUNT_DOMAIN}".lower()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_service_account(db: Session, tenant: Tenant, actor: User | None) -> User:
    """The tenant's machine identity (created on first use, reused after)."""
    email = service_account_email(tenant.id)
    row = db.scalar(select(User).where(User.email == email))
    if row is not None:
        if row.is_deleted or row.tenant_id != tenant.id or not row.is_service_account:
            raise ApiError(
                "The tenant's service account is in an unexpected state; "
                "contact platform support.", 500,
            )
        if row.status != "active":
            row.status = "active"
        return row
    role = db.scalar(select(Role).where(Role.code == SERVICE_ACCOUNT_ROLE))
    if role is None:
        raise ApiError(f"{SERVICE_ACCOUNT_ROLE} role is missing — run the seed.", 500)
    row = User(
        id=new_id("usr"),
        email=email,
        name=f"{tenant.name} API integration",
        # Never used: sign-in is refused for service accounts regardless.
        password_hash=hash_password(secrets.token_urlsafe(48)),
        role_id=role.id,
        tenant_id=tenant.id,
        status="active",
        is_service_account=True,
        created_by=actor.id if actor is not None else None,
    )
    db.add(row)
    db.flush()
    return row


def active_api_key(db: Session, tenant_id: str) -> TenantApiKey | None:
    return db.scalar(
        select(TenantApiKey)
        .where(TenantApiKey.tenant_id == tenant_id, TenantApiKey.status == "active")
        .order_by(TenantApiKey.created_at.desc())
    )


def latest_api_key(db: Session, tenant_id: str) -> TenantApiKey | None:
    return db.scalar(
        select(TenantApiKey)
        .where(TenantApiKey.tenant_id == tenant_id)
        .order_by(TenantApiKey.created_at.desc(), TenantApiKey.id.desc())
    )


def revoke_api_keys(
    db: Session, tenant: Tenant, actor: User | None, *, request: Request | None = None,
    reason: str = "Revoked tenant API token",
) -> list[TenantApiKey]:
    """Revoke every active key of the tenant (flush only; caller commits)."""
    rows = db.scalars(
        select(TenantApiKey)
        .where(TenantApiKey.tenant_id == tenant.id, TenantApiKey.status == "active")
    ).all()
    now = _now()
    for row in rows:
        row.status = "revoked"
        row.revoked_at = now
        row.revoked_by = actor.id if actor is not None else None
        row.updated_by = actor.id if actor is not None else None
        record_audit(
            db, user=actor, action=reason, entity_type="tenant_api_key",
            entity_id=row.id, target_label=f"{tenant.name} · {row.key_prefix}…",
            tenant_id=tenant.id, previous_value={"status": "active"},
            new_value={"status": "revoked", "prefix": row.key_prefix}, request=request,
        )
    db.flush()
    return rows


def provision_api_key(
    db: Session, tenant: Tenant, actor: User | None, *,
    request: Request | None = None, name: str | None = None,
) -> tuple[TenantApiKey, str]:
    """Create (or rotate) the tenant's token. Returns ``(row, plaintext)``.

    The plaintext is the caller's to return ONCE; it is not stored, not
    logged and not recoverable afterwards — rotation is the only way to get
    a usable token again.
    """
    account = ensure_service_account(db, tenant, actor)
    previous = revoke_api_keys(
        db, tenant, actor, request=request, reason="Rotated tenant API token",
    )
    raw = generate_api_key()
    row = TenantApiKey(
        id=new_id("tak"),
        tenant_id=tenant.id,
        user_id=account.id,
        name=(name or "Integration token").strip()[:100] or "Integration token",
        key_prefix=display_prefix(raw),
        key_hash=hash_api_key(raw),
        status="active",
        created_by=actor.id if actor is not None else None,
    )
    db.add(row)
    record_audit(
        db, user=actor,
        action="Rotated tenant API token" if previous else "Created tenant API token",
        entity_type="tenant_api_key", entity_id=row.id,
        target_label=f"{tenant.name} · {row.key_prefix}…", tenant_id=tenant.id,
        new_value={"prefix": row.key_prefix, "serviceAccount": account.email,
                   "replaced": [p.key_prefix for p in previous]},
        request=request,
    )
    db.flush()
    return row, raw


def authenticate_api_key(db: Session, raw: str) -> tuple[TenantApiKey, User] | None:
    """The active key row + its service account for a presented token, or None.

    Every negative outcome is the same ``None`` on purpose: a caller must not
    be able to tell "unknown token" from "revoked" from "account disabled".
    """
    if not looks_like_api_key(raw) or len(raw) > 200:
        return None
    row = db.scalar(select(TenantApiKey).where(TenantApiKey.key_hash == hash_api_key(raw)))
    if row is None or row.status != "active":
        return None
    user = db.get(User, row.user_id)
    if (
        user is None or user.is_deleted or user.status != "active"
        or not user.is_service_account or user.tenant_id != row.tenant_id
    ):
        return None
    return row, user


def serialize_api_key(
    row: TenantApiKey | None, account: User | None = None, *, token: str | None = None,
) -> dict:
    """Metadata for the admin UI. ``token`` (plaintext) only right after generation."""
    if row is None:
        return {"configured": False, "status": None, "prefix": None, "token": None}
    data = {
        "configured": row.status == "active",
        "id": row.id,
        "name": row.name,
        "prefix": row.key_prefix,
        "status": row.status,
        "createdAt": _iso(row.created_at),
        "lastUsedAt": _iso(row.last_used_at),
        "revokedAt": _iso(row.revoked_at),
        "serviceAccountEmail": account.email if account is not None else None,
        "role": account.role.code if account is not None else None,
        "readOnly": True,
        "token": token,
    }
    return data


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace("+00:00", "Z")
