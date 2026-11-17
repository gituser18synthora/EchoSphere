"""Security management contracts against disposable, in-memory SQLite only."""
import csv
import io
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.core.security import create_access_token, hash_password
from backend.routers import users, auth, audit, bots
from shared.db.mysql import get_db
from shared.errors import install_error_handlers
from shared.models import Base, User, Role, Permission, Tenant, AuditLog, VoiceBot, VoiceBotSetting

PASSWORD = "LocalTest9!"
PASSWORD_HASH = hash_password(PASSWORD)
API = "/api/v1"


@pytest.fixture()
def workspace(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    app = FastAPI()
    install_error_handlers(app)
    for router in (users.router, auth.router, audit.router, bots.router):
        app.include_router(router, prefix=API)
    def database():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = database
    with Session(engine) as db:
        permissions = {code: Permission(id="p" + str(i), code=code, name=code, category=category)
            for i, (code, category) in enumerate([
                ("security.manage", "platform"), ("team.manage", "tenant"), ("reset_user_password", "tenant"),
                ("manage_voices", "tenant"), ("bots.manage", "tenant"), ("manage_workflows", "tenant")])}
        roles = {code: Role(id=code, code=code, name=code, scope="platform" if code == "super_admin" else "tenant")
                 for code in ("super_admin", "tenant_admin", "tenant_user")}
        roles["super_admin"].permissions = list(permissions.values())
        roles["tenant_admin"].permissions = [p for code, p in permissions.items() if code != "security.manage"]
        roles["tenant_user"].permissions = [permissions["manage_voices"]]
        db.add_all(list(roles.values()))
        db.add_all([Tenant(id=t, name=t, domain=t + ".example.test", status="active") for t in ("tn_a", "tn_b")])
        db.flush()
        for uid, role, tenant, status in (("root", "super_admin", None, "active"), ("other_root", "super_admin", None, "active"),
                ("admin_a", "tenant_admin", "tn_a", "active"), ("member", "tenant_user", "tn_a", "active"),
                ("admin_b", "tenant_admin", "tn_b", "active"), ("invite", "tenant_user", "tn_a", "invited")):
            db.add(User(id=uid, name=uid, email=uid + "@example.com", role_id=role,
                        tenant_id=tenant, password_hash=PASSWORD_HASH, status=status))
        db.add(VoiceBot(id="bot_test", tenant_id="tn_a", name="Local bot", owner_user_id="admin_a"))
        db.add(VoiceBotSetting(id="settings_test", tenant_id="tn_a", bot_id="bot_test"))
        for i in range(7):
            db.add(AuditLog(id=f"event_{i}", tenant_id="tn_a" if i < 5 else "tn_b", user_id="member",
                actor_name="Member", actor_role="tenant_user", action="Changed voice", entity_type="voice_bot",
                entity_id="bot_test", target_label="=FORMULA()" if i == 0 else "Local bot", created_at=datetime(2026, 11, 13, 10, 0),
                previous_value={"api-key": "hidden", "nested": {"password": "hidden"}}, new_value={"enabled": True}))
        db.commit()
    def headers(uid="root"):
        with Session(engine) as db:
            u = db.get(User, uid)
            return {"Authorization": "Bearer " + create_access_token(user_id=u.id, role=u.role.code, tenant_id=u.tenant_id, auth_version=u.auth_version)}
    monkeypatch.setattr("shared.bot_config.invalidate_bot_config_sync", lambda *args: None)
    with TestClient(app) as client:
        yield client, engine, headers
    engine.dispose()


def data(response):
    assert response.status_code < 300, response.text
    return response.json()["data"]


def test_user_filters_and_pagination(workspace):
    client, _, headers = workspace
    response = client.get(API + "/users?scope=all&tenantId=tn_a&roleCode=tenant_user&pageSize=1", headers=headers())
    assert response.json()["meta"]["total"] == 2
    assert len(data(response)) == 1
    assert data(response)[0]["tenantId"] == "tn_a"
    assert client.get(API + "/users?scope=all", headers=headers("admin_a")).status_code == 403


def test_create_and_role_scope_validation(workspace):
    client, _, headers = workspace
    body = {"name": "New member", "email": "new@example.com", "roleCode": "tenant_user", "tenantId": "tn_a"}
    created = data(client.post(API + "/users", json=body, headers=headers()))
    assert created["status"] == "invited" and created["temporaryPassword"]
    body.update(email="wrong@example.com", roleCode="super_admin")
    assert client.post(API + "/users", json=body, headers=headers()).status_code == 422
    assert client.patch(API + "/users/root", json={"roleCode": "tenant_user"}, headers=headers("other_root")).status_code == 422
    assert client.patch(API + "/users/member", json={"roleCode": "super_admin"}, headers=headers()).status_code == 422
    assert client.patch(API + "/users/admin_b", json={"name": "No"}, headers=headers("admin_a")).status_code == 404


def test_self_and_last_admin_protection(workspace):
    client, engine, headers = workspace
    assert client.patch(API + "/users/root", json={"roleCode": "tenant_user"}, headers=headers()).status_code == 400
    assert client.patch(API + "/users/root", json={"status": "deactivated"}, headers=headers()).status_code == 400
    with Session(engine) as db:
        db.get(User, "other_root").status = "deactivated"
        db.commit()
        with pytest.raises(Exception, match="At least one active Super Admin"):
            users._protect_user_access(db, db.get(User, "root"), db.get(User, "admin_a"), status="deactivated")


def test_service_identity_cannot_be_edited(workspace):
    client, engine, headers = workspace
    with Session(engine) as db:
        db.get(User, "member").is_service_account = True
        db.commit()
    assert client.patch(API + "/users/member", json={"name": "Changed"}, headers=headers()).status_code == 400


def test_permission_editor_conflicts_scope_audit_and_access(workspace):
    client, engine, headers = workspace
    url = API + "/roles/tenant_user/permissions"
    body = {"permissionCodes": ["manage_voices", "manage_workflows"], "expectedPermissionCodes": ["manage_voices"]}
    assert client.put(url, json=body, headers=headers("admin_a")).status_code == 403
    result = data(client.put(url, json=body, headers=headers()))
    assert set(result["permissions"]) == set(body["permissionCodes"])
    assert result["permissionsCustomized"] is True
    assert client.put(url, json=body, headers=headers()).status_code == 409
    body["expectedPermissionCodes"] = result["permissions"]
    body["permissionCodes"] = ["security.manage"]
    assert client.put(url, json=body, headers=headers()).status_code == 403
    assert client.put(API + "/roles/super_admin/permissions", json=body, headers=headers()).status_code == 403
    with Session(engine) as db:
        row = db.scalar(select(AuditLog).where(AuditLog.action == "Updated role permissions"))
        assert row.previous_value["permissions"] == ["manage_voices"]
        assert "manage_workflows" in row.new_value["permissions"]
    switched = client.patch(API + "/bots/bot_test/goal-policy/end-call", json={"enabled": True}, headers=headers("member"))
    assert data(switched)["enabled"] is True


def test_permission_revocation_survives_reseed(workspace):
    _, engine, _ = workspace
    from backend.seeds.base_seed import run_base_seed
    with Session(engine) as db:
        role = db.get(Role, "tenant_user")
        role.permissions_customized = True
        role.permissions = []
        db.commit()
        run_base_seed(db=db)
        db.expire_all()
        assert db.get(Role, "tenant_user").permissions == []


def test_temporary_password_is_restricted_until_changed(workspace):
    client, _, _ = workspace
    login = data(client.post(API + "/auth/login", json={"email": "invite@example.com", "password": PASSWORD}))
    assert login["requiresPasswordChange"] is True
    h = {"Authorization": "Bearer " + login["token"]}
    assert client.get(API + "/auth/me", headers=h).status_code == 403
    changed = data(client.post(API + "/users/me/password", headers=h, json={
        "currentPassword": PASSWORD, "newPassword": "ChosenPassword8!", "confirmPassword": "ChosenPassword8!"}))
    assert data(client.get(API + "/auth/me", headers={"Authorization": "Bearer " + changed["token"]}))["status"] == "active"
    assert client.get(API + "/auth/me", headers=h).status_code in (401, 403)


def test_temporary_reset_preserves_disabled_status_and_cannot_bypass_rotation(workspace):
    client, engine, headers = workspace
    with Session(engine) as db:
        db.get(User, "member").status = "deactivated"
        db.commit()
    reset = data(client.post(API + "/users/member/reset-password", headers=headers()))
    with Session(engine) as db:
        assert db.get(User, "member").status == "deactivated"
    data(client.patch(API + "/users/member", json={"status": "active"}, headers=headers()))
    result = data(client.post(API + "/auth/login", json={"email": "member@example.com", "password": reset["temporaryPassword"]}))
    assert result["requiresPasswordChange"] is True


def test_logout_revokes_current_token_and_records_event(workspace):
    client, _, headers = workspace
    h = headers("member")
    assert client.get(API + "/auth/me", headers=h).status_code == 200
    assert client.post(API + "/auth/logout", headers=h).status_code == 200
    assert client.get(API + "/auth/me", headers=h).status_code == 401
    assert client.get(API + "/auth/me", headers=headers("member")).status_code == 200


def test_failed_login_records_no_credentials(workspace):
    client, engine, _ = workspace
    assert client.post(API + "/auth/login", json={"email": "member@example.com", "password": "WRONG-SECRET"}).status_code == 401
    with Session(engine) as db:
        event = db.scalar(select(AuditLog).where(AuditLog.action == "Sign-in failed"))
        assert event is not None
        assert "WRONG-SECRET" not in str(event.new_value)


def test_audit_filters_details_scope_and_stable_pages(workspace):
    client, _, headers = workspace
    first = client.get(API + "/audit?pageSize=2&dateFrom=2026-11-13&dateTo=2026-11-13&actor=Member", headers=headers("admin_a"))
    assert first.json()["meta"]["total"] == 5
    ids = {r["id"] for r in data(first)}
    second = client.get(API + "/audit?pageSize=2&page=2&dateFrom=2026-11-13&dateTo=2026-11-13&actor=Member", headers=headers("admin_a"))
    assert not ids.intersection(r["id"] for r in data(second))
    assert client.get(API + "/audit/event_6", headers=headers("admin_a")).status_code == 404
    assert client.get(API + "/audit?tenantId=tn_b", headers=headers("admin_a")).status_code == 404
    detail = data(client.get(API + "/audit/event_0", headers=headers("admin_a")))
    assert detail["previousValue"]["nested"]["password"] == "***"
    assert detail["previousValue"]["api-key"] == "***"
    assert client.get(API + "/audit?dateFrom=2026-11-13T10:00:00", headers=headers()).status_code == 422


def test_audit_export_filters_formula_and_limit(workspace, monkeypatch):
    client, _, headers = workspace
    response = client.get(API + "/audit/export?dateFrom=2026-11-13&dateTo=2026-11-13", headers=headers("admin_a"))
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 6
    assert "hidden" not in response.text
    assert any("'=FORMULA()" in row for row in rows)
    assert all("tn_b" not in row for row in rows)
    monkeypatch.setattr(audit, "EXPORT_LIMIT", 2)
    assert client.get(API + "/audit/export", headers=headers()).status_code == 422


def test_reset_and_disable_invalidate_sessions_even_in_the_same_second(workspace):
    client, _, headers = workspace
    original = headers("member")
    data(client.post(API + "/users/member/reset-password", headers=headers(), json={
        "newPassword": "Replacement9!", "confirmPassword": "Replacement9!"}))
    assert client.get(API + "/auth/me", headers=original).status_code == 401
    login = data(client.post(API + "/auth/login", json={"email": "member@example.com", "password": "Replacement9!"}))
    current = {"Authorization": "Bearer " + login["token"]}
    assert client.get(API + "/auth/me", headers=current).status_code == 200
    data(client.patch(API + "/users/member", json={"status": "deactivated"}, headers=headers()))
    data(client.patch(API + "/users/member", json={"status": "active"}, headers=headers()))
    assert client.get(API + "/auth/me", headers=current).status_code == 401


def test_voice_editor_uses_existing_permission_and_tenant_isolation(workspace):
    client, engine, headers = workspace
    assert data(client.patch(API + "/bots/bot_test/goal-policy/end-call", headers=headers("member"), json={"enabled": True}))["enabled"] is True
    assert client.patch(API + "/bots/bot_test/goal-policy/end-call", headers=headers("admin_b"), json={"enabled": False}).status_code == 404
    with Session(engine) as db:
        db.get(Role, "tenant_user").permissions = []
        db.commit()
    assert client.patch(API + "/bots/bot_test/goal-policy/end-call", headers=headers("member"), json={"enabled": False}).status_code == 403


def test_trusted_proxy_ip_and_csv_cell_protection():
    from starlette.requests import Request
    from backend.core.audit import client_ip
    def request(peer, chain):
        return Request({"type": "http", "client": (peer, 1234), "headers": [(b"x-forwarded-for", chain.encode())]})
    assert client_ip(request("203.0.113.20", "198.51.100.8")) == "203.0.113.20"
    assert client_ip(request("127.0.0.1", "198.51.100.9, 203.0.113.8")) == "203.0.113.8"
    for value in ("=SUM(1)", "+1", "-1", "@x", "  =X()", "\tvalue", "\rvalue", "\nvalue", "\ufeff=SUM(1)", "\x00+1"):
        assert audit._csv_cell(value).startswith("'")
    from shared.errors import ApiError
    with pytest.raises(ApiError):
        audit._date_bound("9999-12-31", end=True)


def test_security_schema_migration_on_an_isolated_old_schema():
    import importlib.util
    from pathlib import Path
    from sqlalchemy import text, inspect
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[2] / "backend/alembic/versions/5e9c3a7b1d2f_security_management.py"
    spec = importlib.util.spec_from_file_location("security_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE roles (id VARCHAR(40) PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE users (id VARCHAR(40) PRIMARY KEY, status VARCHAR(20))"))
        conn.execute(text("INSERT INTO users VALUES ('invite', 'invited'), ('normal', 'active')"))
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        assert conn.execute(text("SELECT must_change_password, auth_version FROM users WHERE id='invite'")).one() == (1, 0)
        assert "revoked_tokens" in inspect(conn).get_table_names()
        module.downgrade()
        assert "revoked_tokens" not in inspect(conn).get_table_names()
    engine.dispose()


def test_legacy_jwt_is_supported_and_can_be_revoked(workspace):
    import jwt
    from shared.config import get_settings
    client, _, _ = workspace
    settings = get_settings()
    now = datetime.now().timestamp()
    token = jwt.encode({"sub": "member", "role": "tenant_user", "tenant_id": "tn_a", "iat": int(now), "exp": int(now) + 3600}, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    headers = {"Authorization": "bearer " + token}
    assert client.get(API + "/auth/me", headers=headers).status_code == 200
    assert client.post(API + "/auth/logout", headers=headers).status_code == 200
    assert client.get(API + "/auth/me", headers=headers).status_code == 401
