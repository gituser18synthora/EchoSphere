"""Authentication: login, current user."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import get_current_user
from backend.core.errors import ApiError
from backend.core.responses import ok
from backend.core.security import create_access_token, verify_password
from backend.db.mysql import get_db
from backend.models import Tenant, User
from backend.serializers import serialize_user_public

router = APIRouter(prefix="/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


@router.post("/login")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    user = db.scalar(
        select(User).where(User.email == body.email.lower(), User.is_deleted.is_(False))
    )
    if user is None or not verify_password(body.password, user.password_hash):
        raise ApiError("Invalid email or password.", 401)
    if user.status == "deactivated":
        raise ApiError("This account has been deactivated.", 403)

    tenant_name = None
    if user.tenant_id:
        tenant = db.get(Tenant, user.tenant_id)
        if tenant is None or tenant.is_deleted:
            raise ApiError("Your organization is no longer active.", 403)
        if tenant.status == "suspended":
            raise ApiError("Your organization is suspended. Contact support.", 403)
        tenant_name = tenant.name

    user.last_login_at = datetime.now(timezone.utc)
    user.last_active_at = user.last_login_at
    if user.status == "invited":
        user.status = "active"
    record_audit(
        db, user=user, action="Signed in", entity_type="user", entity_id=user.id,
        target_label=user.email, request=request,
    )
    db.commit()

    token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id)
    payload = serialize_user_public(user)
    payload["tenantName"] = tenant_name
    return ok({"token": token, "user": payload})


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
    record_audit(db, user=user, action="Signed out", entity_type="user",
                 entity_id=user.id, target_label=user.email, request=request)
    db.commit()
    return ok({"signedOut": True})
