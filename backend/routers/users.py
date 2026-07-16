"""Users (team members + platform users), roles, permissions."""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    SUPER_ADMIN,
    assert_tenant_access,
    get_current_user,
    is_super_admin,
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.errors import ApiError, NotFoundError
from backend.core.ids import new_id
from backend.core.pagination import PageParams, page_params
from backend.core.responses import ok, paginated
from backend.core.security import hash_password
from backend.core.softdelete import guard_hard_delete, soft_delete
from backend.db.mysql import get_db
from backend.models import Permission, Role, User, VoiceBot
from backend.serializers import serialize_role, serialize_team_member

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
    scope: str = Query("tenant", pattern="^(tenant|platform)$"),
    tenant_id: str | None = Query(None, alias="tenantId"),
    params: PageParams = Depends(page_params),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(User).where(User.is_deleted.is_(False))
    if scope == "platform":
        if not is_super_admin(user):
            raise ApiError("Only platform administrators can list platform users.", 403)
        stmt = stmt.where(User.tenant_id.is_(None))
    else:
        stmt = stmt.where(User.tenant_id == resolve_tenant_id(user, tenant_id))
    if params.search:
        like = f"%{params.search}%"
        stmt = stmt.where(or_(User.name.like(like), User.email.like(like)))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(User.created_at.asc()).offset(params.offset).limit(params.page_size)
    ).all()
    owned = _bots_owned_map(db, [u.id for u in rows])
    return paginated(
        [serialize_team_member(u, bots_owned=owned.get(u.id, 0)) for u in rows],
        page=params.page, page_size=params.page_size, total=total,
    )


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    role_code: str = Field(alias="roleCode")
    tenant_id: str | None = Field(default=None, alias="tenantId")
    password: str | None = Field(default=None, min_length=8, max_length=128)

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
    else:
        tenant_id = resolve_tenant_id(user, body.tenant_id)

    email = body.email.lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ApiError("A user with this email already exists.", 409)

    # Invited users receive a temporary password; must be rotated on first login.
    import secrets

    password = body.password or secrets.token_urlsafe(12)
    row = User(
        id=new_id("usr"),
        email=email,
        name=body.name,
        password_hash=hash_password(password),
        role_id=role.id,
        tenant_id=tenant_id,
        status="invited" if body.password is None else "active",
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


class UpdateUserRequest(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    role_code: str | None = Field(default=None, alias="roleCode")
    status: str | None = Field(default=None, pattern="^(active|invited|deactivated)$")

    model_config = {"populate_by_name": True}


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

    before = {"name": row.name, "role": row.role.code, "status": row.status}
    if body.name:
        row.name = body.name
    if body.status:
        if row.id == user.id and body.status == "deactivated":
            raise ApiError("You cannot deactivate your own account.", 400)
        row.status = body.status
    if body.role_code:
        role = db.scalar(select(Role).where(Role.code == body.role_code))
        if role is None:
            raise ApiError("Unknown role.", 422)
        if role.scope == "platform" and not is_super_admin(user):
            raise ApiError("Only platform administrators can grant platform roles.", 403)
        if role.scope == "platform" and row.tenant_id is not None:
            raise ApiError("Tenant members cannot hold platform roles.", 422)
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
        new_value={"name": row.name, "status": row.status}, request=request,
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
def list_roles(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    roles = db.scalars(select(Role).order_by(Role.created_at.asc())).all()
    counts = dict(
        db.execute(
            select(User.role_id, func.count())
            .where(User.is_deleted.is_(False))
            .group_by(User.role_id)
        ).all()
    )
    return ok([serialize_role(r, members=counts.get(r.id, 0)) for r in roles])


@router.get("/permissions")
def list_permissions(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    perms = db.scalars(select(Permission).order_by(Permission.category, Permission.code)).all()
    return ok([
        {"id": p.id, "code": p.code, "name": p.name, "category": p.category,
         "description": p.description or ""}
        for p in perms
    ])
