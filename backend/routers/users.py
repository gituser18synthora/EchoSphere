"""Users (team members + platform users), roles, permissions."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    SUPER_ADMIN,
    TENANT_USER,
    assert_tenant_access,
    get_current_user,
    is_super_admin,
    require_permission,
    require_tenant_admin,
    require_super_admin,
    resolve_tenant_id,
)
from shared.errors import ApiError, NotFoundError
from shared.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.security import (
    MIN_PASSWORD_LENGTH,
    hash_password,
    validate_password_policy,
)
from backend.core.softdelete import guard_hard_delete, soft_delete
from shared.db.mysql import get_db
from shared.models import Permission, Role, User, VoiceBot, Tenant
from backend.serializers import serialize_role, serialize_team_member, serialize_user_public

router = APIRouter(tags=["Users & Roles"])


def _bots_owned_map(db: Session, user_ids: list[str]) -> dict[str, int]:
    if not user_ids:
        return {}
    rows = db.execute(
        select(VoiceBot.owner_user_id, func.count())
        .where(VoiceBot.owner_user_id.in_(user_ids), VoiceBot.is_deleted.is_(False))
        .group_by(VoiceBot.owner_user_id)
    ).all()
    return {uid: n for uid, n in rows}


@router.get("/users")
def list_users(
    request: Request,
    scope: str = Query("tenant", pattern="^(tenant|platform|all)$"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    role_code: str | None = Query(None, alias="roleCode", max_length=50),
    status: str | None = Query(None, pattern="^(active|invited|deactivated)$"),
    params: PageParams = Depends(page_params),
    # Team data is for team managers only — a tenant_user must not be able to
    # enumerate their organization's members (names, emails, activity).
    user: User = Depends(require_permission("team.manage", "security.manage")),
    db: Session = Depends(get_db),
):
    # Service accounts (tenant API tokens) are machine identities: managed
    # from the tenant's API-token card, never listed or edited as team members.
    stmt = select(User).where(User.is_deleted.is_(False), User.is_service_account.is_(False))
    if scope in ("platform", "all"):
        if not is_super_admin(user):
            raise ApiError("Only platform administrators can list platform users.", 403)
        if scope == "platform":
            stmt = stmt.where(User.tenant_id.is_(None))
        elif tenant_id:
            stmt = stmt.where(User.tenant_id == tenant_id)
    else:
        stmt = stmt.where(User.tenant_id == resolve_tenant_id(user, tenant_id))
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(or_(User.name.like(like), User.email.like(like)))
    if role_code:
        stmt = stmt.where(User.role.has(Role.code == role_code))
    if status:
        stmt = stmt.where(User.status == status)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(User.created_at.asc(), User.id.asc()).offset(params.offset).limit(params.page_size)
    ).all()
    owned = _bots_owned_map(db, [u.id for u in rows])
    tenant_names = dict(db.execute(select(Tenant.id, Tenant.name)).all()) if is_super_admin(user) else {}
    return paginated(
        [{**serialize_team_member(u, bots_owned=owned.get(u.id, 0)), "tenantName": tenant_names.get(u.tenant_id)} for u in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    role_code: str = Field(alias="roleCode")
    tenant_id: str | None = Field(default=None, alias="tenantId")
    password: str | None = Field(default=None, min_length=MIN_PASSWORD_LENGTH, max_length=128)

    model_config = {"populate_by_name": True}


@router.post("/users", status_code=201)
def create_user(
    body: CreateUserRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    role = db.scalar(select(Role).where(Role.code == body.role_code))
    if role is None:
        raise ApiError("Unknown role.", 422)

    if role.scope == "platform":
        if not is_super_admin(user):
            raise ApiError("Only platform administrators can create platform users.", 403)
        tenant_id = None
        if body.tenant_id:
            raise ApiError("Platform users cannot belong to an organization.", 422)
    else:
        # Tenant admins add members only as Tenant User — never another admin
        # or any internal role. Platform admins keep full role assignment
        # (tenant onboarding creates the first tenant admin).
        if not is_super_admin(user) and role.code != TENANT_USER:
            raise ApiError("Team members can only be added as Tenant User.", 403)
        tenant_id = resolve_tenant_id(user, body.tenant_id)
        tenant = db.get(Tenant, tenant_id)
        if tenant is None or tenant.is_deleted:
            raise NotFoundError("Organization")

    email = body.email.lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ApiError("A user with this email already exists.", 409)

    # Invited users receive a temporary password; must be rotated on first login.
    # An admin-chosen password must satisfy the shared policy; the generated
    # temporary password is always well above the minimum length.
    import secrets

    if body.password is not None:
        validate_password_policy(body.password, field="password")
    password = body.password or secrets.token_urlsafe(12)
    row = User(
        id=new_id("usr"),
        email=email,
        name=body.name,
        password_hash=hash_password(password),
        role_id=role.id,
        tenant_id=tenant_id,
        status="invited" if body.password is None else "active",
        must_change_password=body.password is None,
        created_by=user.id,
    )
    db.add(row)
    record_audit(
        db, user=user, action="Created user", entity_type="user", entity_id=row.id,
        target_label=email, tenant_id=tenant_id,
        new_value={"name": body.name, "role": role.code}, request=request,
    )
    db.commit()
    data = serialize_team_member(row, bots_owned=0)
    if body.password is None:
        data["temporaryPassword"] = password
    return ok(data)


# ── Own profile & password (declared before /users/{user_id} so "me" wins) ────


class UpdateMyProfileRequest(BaseModel):
    first_name: str | None = Field(default=None, alias="firstName", max_length=80)
    last_name: str | None = Field(default=None, alias="lastName", max_length=80)
    phone: str | None = Field(default=None, max_length=30)
    avatar_url: str | None = Field(default=None, alias="avatarUrl", max_length=500)
    locale: str | None = Field(default=None, max_length=15)
    timezone: str | None = Field(default=None, max_length=64)

    model_config = {"populate_by_name": True, "extra": "forbid"}


@router.patch("/users/me")
def update_my_profile(
    body: UpdateMyProfileRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from backend.serializers import serialize_user_public

    before = {"name": user.name, "phone": user.phone, "locale": user.locale,
              "timezone": user.timezone}
    for field in ("phone", "avatar_url", "locale", "timezone"):
        val = getattr(body, field)
        if val is not None:
            setattr(user, field, val)
    if body.first_name is not None or body.last_name is not None:
        first = (body.first_name if body.first_name is not None else
                 user.first_name or user.name.split(" ")[0]).strip()
        last = (body.last_name if body.last_name is not None else
                user.last_name or " ".join(user.name.split(" ")[1:])).strip()
        if not first:
            raise ApiError("First name cannot be empty.", 422,
                           errors=[{"field": "firstName", "message": "First name is required."}])
        user.first_name = first
        user.last_name = last
        user.name = f"{first} {last}".strip()
    user.updated_by = user.id
    record_audit(
        db, user=user, action="Updated own profile", entity_type="user",
        entity_id=user.id, target_label=user.email, tenant_id=user.tenant_id,
        previous_value=before,
        new_value={"name": user.name, "phone": user.phone, "locale": user.locale,
                   "timezone": user.timezone},
        request=request,
    )
    db.commit()
    return ok(serialize_user_public(user))


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(alias="currentPassword", min_length=1, max_length=200)
    new_password: str = Field(alias="newPassword", min_length=1, max_length=128)
    confirm_password: str = Field(alias="confirmPassword", min_length=1, max_length=128)

    model_config = {"populate_by_name": True}


@router.post("/users/me/password")
def change_my_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Change own password. Invalidates every other session (tokens issued
    before the change are rejected) and returns a fresh token so the current
    session continues."""
    from datetime import datetime, timezone as tz

    from backend.core.security import create_access_token, hash_password, verify_password

    user = db.get(User, user.id, with_for_update=True, populate_existing=True)
    if user.status == "deactivated":
        raise ApiError("This account has been deactivated.", 403)

    if not verify_password(body.current_password, user.password_hash):
        raise ApiError("The current password is incorrect.", 400,
                       errors=[{"field": "currentPassword", "message": "Incorrect password."}])
    if body.new_password != body.confirm_password:
        raise ApiError("The new password and confirmation do not match.", 422,
                       errors=[{"field": "confirmPassword", "message": "Passwords do not match."}])
    if body.new_password == body.current_password:
        raise ApiError("The new password must be different from the current password.", 422,
                       errors=[{"field": "newPassword", "message": "Must differ from current password."}])
    validate_password_policy(body.new_password)

    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = datetime.now(tz.utc)
    user.auth_version += 1
    user.updated_by = user.id
    if user.status == "invited":
        user.status = "active"
    user.must_change_password = False
    # Never log the password — only the fact that it changed.
    record_audit(
        db, user=user, action="Changed own password", entity_type="user",
        entity_id=user.id, target_label=user.email, tenant_id=user.tenant_id,
        request=request,
    )
    db.commit()
    token = create_access_token(user_id=user.id, role=user.role.code, tenant_id=user.tenant_id, auth_version=user.auth_version)
    payload = serialize_user_public(user)
    tenant = db.get(Tenant, user.tenant_id) if user.tenant_id else None
    payload["tenantName"] = tenant.name if tenant else None
    return ok({"changed": True, "token": token,
               "user": payload,
               "message": "Password changed. Other sessions have been signed out."})


class AdminResetPasswordRequest(BaseModel):
    """Optional body: when a new password is supplied the admin chooses it
    directly; when omitted a one-time temporary password is issued."""

    new_password: str | None = Field(default=None, alias="newPassword", max_length=128)
    confirm_password: str | None = Field(default=None, alias="confirmPassword", max_length=128)

    model_config = {"populate_by_name": True, "extra": "forbid"}


@router.post("/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: str,
    request: Request,
    body: AdminResetPasswordRequest | None = None,
    user: User = Depends(require_permission("reset_user_password")),
    db: Session = Depends(get_db),
):
    """Admin password reset. Two modes, one security path:

    - With ``newPassword``/``confirmPassword``: sets the chosen password after
      match + policy validation (used by Edit Tenant → reset tenant admin).
    - Without a body: issues a one-time temporary password (returned once).

    Either way the plain text is never stored or logged, and the target's
    existing sessions are invalidated (tokens issued before the change are
    rejected via ``password_changed_at``)."""
    from datetime import datetime, timezone as tz

    from backend.core.security import hash_password

    row = db.get(User, user_id, with_for_update=True, populate_existing=True)
    if row is None or row.is_deleted:
        raise NotFoundError("User")
    if row.tenant_id is not None:
        assert_tenant_access(user, row.tenant_id)
    elif not is_super_admin(user):
        raise NotFoundError("User")
    if row.is_service_account:
        raise ApiError("Service accounts have no password — rotate the tenant API token instead.", 400)
    if row.role.code == SUPER_ADMIN and not is_super_admin(user):
        raise ApiError("Only platform administrators can reset platform accounts.", 403)
    if row.id == user.id:
        raise ApiError("Use the change-password form for your own account.", 400)

    chosen = body is not None and (
        body.new_password is not None or body.confirm_password is not None
    )
    if chosen:
        if not body.new_password or body.new_password != body.confirm_password:
            raise ApiError(
                "The new password and confirmation do not match.", 422,
                errors=[{"field": "confirmPassword", "message": "Passwords do not match."}],
            )
        validate_password_policy(body.new_password)
        row.password_hash = hash_password(body.new_password)
        row.must_change_password = False
        # An explicitly chosen password is a real credential — no forced
        # rotation; invited accounts become active, deactivated stay locked.
        if row.status == "invited":
            row.status = "active"
        response: dict = {"reset": True, "sessionsInvalidated": True}
    else:
        import secrets

        temp_password = secrets.token_urlsafe(12)
        row.password_hash = hash_password(temp_password)
        row.must_change_password = True
        if row.status != "deactivated":
            row.status = "invited"
        response = {"reset": True, "sessionsInvalidated": True,
                    "temporaryPassword": temp_password}

    row.password_changed_at = datetime.now(tz.utc)
    row.auth_version += 1
    row.updated_by = user.id
    record_audit(
        db, user=user, action="Reset user password", entity_type="user",
        entity_id=row.id, target_label=row.email, tenant_id=row.tenant_id,
        new_value={"method": "admin-set" if chosen else "temporary",
                   "sessionsInvalidated": True},
        request=request,
    )
    db.commit()
    return ok(response)


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    email: EmailStr | None = None
    role_code: str | None = Field(default=None, alias="roleCode")
    status: str | None = Field(default=None, pattern="^(active|invited|deactivated)$")

    model_config = {"populate_by_name": True, "extra": "forbid"}


def _protect_user_access(db: Session, row: User, actor: User, *, role_code: str | None = None, status: str | None = None):
    if row.is_service_account:
        raise ApiError("Manage this machine identity through its API token.", 400)
    if row.id == actor.id and (status == "deactivated" or (role_code and role_code != row.role.code)):
        raise ApiError("You cannot disable or change the role of your own account.", 400)
    if row.role.code == SUPER_ADMIN and row.status == "active" and (status == "deactivated" or (role_code and role_code != SUPER_ADMIN)):
        # Serialize administrator removals on one shared row, including concurrent requests.
        db.scalar(select(Role).where(Role.code == SUPER_ADMIN).with_for_update())
        remaining = list(db.scalars(select(User.id).where(
            User.role_id == row.role_id, User.id != row.id, User.status == "active", User.is_deleted.is_(False)).with_for_update()).all())
        if not remaining:
            raise ApiError("At least one active Super Admin must remain.", 409)


@router.patch("/users/{user_id}")
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    request: Request,
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(User, user_id)
    if row is None or row.is_deleted:
        raise NotFoundError("User")
    if row.tenant_id is None and not is_super_admin(user):
        raise NotFoundError("User")
    if row.tenant_id is not None:
        assert_tenant_access(user, row.tenant_id)

    _protect_user_access(db, row, user, role_code=body.role_code, status=body.status)
    before = {"name": row.name, "email": row.email, "role": row.role.code, "status": row.status}
    if body.name:
        row.name = body.name
    if body.email:
        email = body.email.lower()
        if db.scalar(select(User.id).where(User.email == email, User.id != row.id)):
            raise ApiError("A user with this email already exists.", 409)
        row.email = email
    if body.status:
        if row.id == user.id and body.status == "deactivated":
            raise ApiError("You cannot deactivate your own account.", 400)
        if row.status == "invited":
            row.must_change_password = True
        if body.status == "deactivated" and row.status != "deactivated":
            row.auth_version = User.auth_version + 1
        row.status = body.status
    if body.role_code:
        role = db.scalar(select(Role).where(Role.code == body.role_code))
        if role is None:
            raise ApiError("Unknown role.", 422)
        if role.scope == "platform" and not is_super_admin(user):
            raise ApiError("Only platform administrators can grant platform roles.", 403)
        if role.scope == "platform" and row.tenant_id is not None:
            raise ApiError("Tenant members cannot hold platform roles.", 422)
        if role.scope == "tenant" and row.tenant_id is None:
            raise ApiError("A platform account cannot be assigned a tenant role.", 422)
        # Same rule as creation: a tenant admin cannot promote a member to
        # any role beyond Tenant User (no create-then-promote bypass).
        if role.scope != "platform" and not is_super_admin(user) and role.code != TENANT_USER:
            raise ApiError("Team members can only hold the Tenant User role.", 403)
        if row.role_id != role.id:
            row.role_id = role.id
            record_audit(
                db, user=user, action="Changed user role", entity_type="user",
                entity_id=row.id, target_label=row.email, tenant_id=row.tenant_id,
                previous_value={"role": before["role"]}, new_value={"role": role.code},
                request=request,
            )
    record_audit(
        db, user=user, action="Updated user", entity_type="user", entity_id=row.id,
        target_label=row.email, tenant_id=row.tenant_id, previous_value=before,
        new_value={"name": row.name, "email": row.email, "role": row.role.code, "status": row.status}, request=request,
    )
    db.commit()
    db.refresh(row)
    owned = _bots_owned_map(db, [row.id])
    return ok(serialize_team_member(row, bots_owned=owned.get(row.id, 0)))


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    request: Request,
    hard: bool = Query(False),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    row = db.get(User, user_id)
    if row is None or row.is_deleted:
        raise NotFoundError("User")
    if row.tenant_id is not None:
        assert_tenant_access(user, row.tenant_id)
    elif not is_super_admin(user):
        raise NotFoundError("User")
    if row.id == user.id:
        raise ApiError("You cannot delete your own account.", 400)
    _protect_user_access(db, row, user, status="deactivated")
    if row.is_service_account:
        raise ApiError("This is the tenant's API-token identity — revoke the token instead.", 400)
    if hard:
        guard_hard_delete()
    soft_delete(row, user)
    row.status = "deactivated"
    record_audit(
        db, user=user, action="Archived user", entity_type="user", entity_id=row.id,
        target_label=row.email, tenant_id=row.tenant_id, request=request,
    )
    db.commit()
    return ok({"archived": True, "id": row.id})


@router.get("/roles")
def list_roles(
    user: User = Depends(require_permission("team.manage", "security.manage")),
    db: Session = Depends(get_db),
):
    """Role catalog with member counts.

    Tenant callers see only tenant-scope roles, and the member counts cover
    ONLY their own tenant — a platform-wide count is cross-tenant data
    (it reveals how many admins/users exist across other organizations).
    Platform admins keep the full catalog with global counts."""
    role_stmt = select(Role).order_by(Role.created_at.asc())
    count_stmt = select(User.role_id, func.count()).where(User.is_deleted.is_(False))
    if not is_super_admin(user):
        role_stmt = role_stmt.where(Role.scope == "tenant")
        count_stmt = count_stmt.where(User.tenant_id == user.tenant_id)
    roles = db.scalars(role_stmt).all()
    counts = dict(db.execute(count_stmt.group_by(User.role_id)).all())
    return ok([serialize_role(r, members=counts.get(r.id, 0)) for r in roles])


@router.get("/permissions")
def list_permissions(
    user: User = Depends(require_permission("team.manage", "security.manage")),
    db: Session = Depends(get_db),
):
    perms = db.scalars(select(Permission).order_by(Permission.category, Permission.code)).all()
    return ok([
        {"id": p.id, "code": p.code, "name": p.name, "category": p.category,
         "description": p.description or ""}
        for p in perms
    ])


class RolePermissionsRequest(BaseModel):
    permission_codes: list[str] = Field(alias="permissionCodes", max_length=300)
    expected_permission_codes: list[str] = Field(alias="expectedPermissionCodes", max_length=300)
    model_config = {"extra": "forbid"}


@router.put("/roles/{role_id}/permissions")
def update_role_permissions(role_id: str, body: RolePermissionsRequest, request: Request,
                           user: User = Depends(require_super_admin), db: Session = Depends(get_db)):
    role = db.scalar(select(Role).where(Role.id == role_id).with_for_update())
    if role is None:
        raise NotFoundError("Role")
    if role.code not in ("tenant_admin", "tenant_user"):
        raise ApiError("Permissions for this protected system role cannot be changed.", 403)
    db.expire(role, ["permissions"])
    before = sorted(p.code for p in role.permissions)
    if set(before) != set(body.expected_permission_codes):
        raise ApiError("Permissions changed since you opened this role. Reload before saving.", 409)
    codes = set(body.permission_codes)
    permissions = list(db.scalars(select(Permission).where(Permission.code.in_(codes))).all()) if codes else []
    if {p.code for p in permissions} != codes:
        raise ApiError("One or more permission codes are unknown.", 422)
    if any(p.category == "platform" for p in permissions):
        raise ApiError("Platform permissions cannot be granted to a tenant role.", 403)
    members = db.scalar(select(func.count()).select_from(User).where(User.role_id == role.id, User.is_deleted.is_(False))) or 0
    role.permissions = permissions
    role.permissions_customized = True
    record_audit(db, user=user, action="Updated role permissions", entity_type="role", entity_id=role.id,
                 target_label=role.name, previous_value={"permissions": before},
                 new_value={"permissions": sorted(codes), "affectedMembers": members}, request=request)
    db.commit()
    return ok(serialize_role(role, members=members))
