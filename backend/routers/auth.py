"""Authentication: login, current user."""

from datetime import datetime, timezone
from hashlib import sha256

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import get_current_user
from shared.errors import ApiError
from backend.core.responses import ok
from backend.core.security import create_access_token, verify_password, decode_access_token
from shared.db.mysql import get_db
from shared.models import Tenant, User, RevokedToken
from backend.serializers import serialize_user_public

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


def _reject_login(db, request, email, user, message, status_code):
    record_audit(db, user=None, action="Sign-in failed", entity_type="user",
                 tenant_id=user.tenant_id if user else None, target_label=email.lower(),
                 new_value={"reason": message}, request=request)
    db.commit()
    raise ApiError(message, status_code)


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(
        select(User).where(User.email == body.email.lower(), User.is_deleted.is_(False))
    )
    if user is None or not verify_password(body.password, user.password_hash):
        _reject_login(db, request, body.email, user, "Invalid email or password.", 401)
    if user.status == "deactivated":
        _reject_login(db, request, body.email, user, "This account has been deactivated.", 403)
    if user.is_service_account:
        # Machine identity behind a tenant API token: never a session.
        _reject_login(db, request, body.email, user, "Service accounts cannot sign in — use the tenant API token.", 403)

    tenant_name = None
    if user.tenant_id:
        tenant = db.get(Tenant, user.tenant_id)
        if tenant is None or tenant.is_deleted:
            _reject_login(db, request, body.email, user, "Your organization is no longer active.", 403)
        if tenant.status == "suspended":
            _reject_login(db, request, body.email, user, "Your organization is suspended. Contact support.", 403)
        tenant_name = tenant.name

    user.last_login_at = datetime.now(timezone.utc)
    user.last_active_at = user.last_login_at
    requires_change = user.status == "invited" or user.must_change_password
    record_audit(
        db, user=user, action="Signed in", entity_type="user", entity_id=user.id,
        target_label=user.email, request=request,
    )
    db.commit()

    token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id,
                                purpose="password_change" if requires_change else "access", auth_version=user.auth_version)
    payload = serialize_user_public(user)
    payload["tenantName"] = tenant_name
    return ok({"token": token, "user": payload, "requiresPasswordChange": requires_change})


@router.get("/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    payload = serialize_user_public(user)
    if user.tenant_id:
        tenant = db.get(Tenant, user.tenant_id)
        payload["tenantName"] = tenant.name if tenant else None
    else:
        payload["tenantName"] = None
    return ok(payload)


@router.post("/logout")
def logout(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    token = request.headers.get("authorization", "").split(" ", 1)[-1].strip()
    payload = decode_access_token(token)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.scalar(select(User.id).where(User.id == user.id).with_for_update())
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at < now))
    db.merge(RevokedToken(token_hash=sha256(token.encode()).hexdigest(), user_id=user.id,
                         expires_at=datetime.fromtimestamp(payload["exp"], timezone.utc).replace(tzinfo=None)))
    record_audit(db, user=user, action="Signed out", entity_type="user",
                 entity_id=user.id, target_label=user.email, request=request)
    db.commit()
    return ok({"signedOut": True})
