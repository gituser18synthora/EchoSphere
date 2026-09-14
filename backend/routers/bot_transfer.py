"""Bot Export / Import — environment migration of ONE bot, identity preserved.

    GET  /bots/{bot_id}/export           → portable package (bot_<id>.json)
    POST /bots/import/preview            → validate + dry run (nothing written)
    POST /bots/import                    → create-or-update the bot in place

The destination tenant is never taken from the package alone: tenant roles
import into their own tenant, super admins name it with ``?tenantId=``; the
engine rejects the import when the package's tenant differs, when the package
is internally inconsistent, or when its identity seal does not match.
"""

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy.orm import Session

from backend.core.audit import record_audit
from backend.core.deps import (
    assert_tenant_access,
    has_permission,
    require_tenant_admin,
    resolve_tenant_id,
)
from backend.core.responses import ok
from shared.db.mysql import get_db
from shared.errors import ForbiddenError, NotFoundError
from shared.models import User, VoiceBot

router = APIRouter(tags=["VoiceBots"])


def _bot_checked(db: Session, bot_id: str, user: User) -> VoiceBot:
    bot = db.get(VoiceBot, bot_id)
    if bot is None or bot.is_deleted:
        raise NotFoundError("VoiceBot")
    try:
        assert_tenant_access(user, bot.tenant_id)
    except NotFoundError:
        raise NotFoundError("VoiceBot")  # cross-tenant probes look like a miss
    return bot


def _require_manage(user: User) -> None:
    if not has_permission(user, "bots.manage"):
        raise ForbiddenError()


@router.get("/bots/{bot_id}/export")
async def export_bot_configuration(
    bot_id: str,
    request: Request,
    include_knowledge: bool = Query(True, alias="includeKnowledge"),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    """Export one bot as a portable, id-preserving package: the bot row,
    languages, voice/STT/TTS/LLM settings, prompts + versions, workflows,
    intents, tools, bot-scoped knowledge (+ documents/chunks), test scenarios,
    runtime-context schema, releases, referenced shared resources and the
    environment section (channels, numbers). Secrets are references only."""
    from backend.core.bot_transfer import export_bot, export_knowledge_plane, seal_package

    _require_manage(user)
    bot = _bot_checked(db, bot_id, user)
    package = export_bot(db, bot)
    if include_knowledge:
        kb_ids = [k["id"] for k in package["resources"]["knowledge_sources"]]
        package["knowledge_plane"] = await export_knowledge_plane(bot.tenant_id, kb_ids)
    seal_package(package)
    record_audit(
        db, user=user, action="Exported bot configuration", entity_type="voice_bot",
        entity_id=bot.id, target_label=bot.name, tenant_id=bot.tenant_id,
        new_value={
            "includeKnowledge": include_knowledge,
            "workflows": len(package["resources"]["workflows"]),
            "prompts": len(package["resources"]["prompts"]),
            "intents": len(package["resources"]["intents"]),
        },
        request=request,
    )
    db.commit()
    return ok(package)


def _destination_tenant(user: User, tenant_id: str | None, package: dict) -> str:
    """Tenant roles always import into their own tenant. A super admin must
    name the destination; the package's tenant is only a default for them,
    and the engine still rejects any mismatch."""
    from backend.core.deps import is_super_admin

    if is_super_admin(user) and not tenant_id:
        tenant_id = package.get("tenant_id") if isinstance(package, dict) else None
    return resolve_tenant_id(user, tenant_id)


@router.post("/bots/import/preview")
def preview_bot_import(
    request: Request,
    package: dict = Body(...),
    tenant_id: str | None = Query(None, alias="tenantId"),
    apply_environment: bool = Query(False, alias="applyEnvironment"),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    """Validate the package against this environment and report exactly what
    an import would do (create vs update, resources created/updated/removed,
    preserved environment values, warnings) — then roll everything back."""
    from backend.core.bot_transfer import import_bot

    _require_manage(user)
    destination = _destination_tenant(user, tenant_id, package)
    try:
        report, kb_plane = import_bot(
            db, package, destination_tenant_id=destination, user=user,
            apply_environment=apply_environment, dry_run=True,
        )
        report.knowledge_documents = len((kb_plane or {}).get("documents") or [])
        result = report.as_dict()
    finally:
        db.rollback()  # a preview never writes
    return ok(result)


@router.post("/bots/import")
async def import_bot_configuration(
    request: Request,
    package: dict = Body(...),
    tenant_id: str | None = Query(None, alias="tenantId"),
    apply_environment: bool = Query(False, alias="applyEnvironment"),
    user: User = Depends(require_tenant_admin),
    db: Session = Depends(get_db),
):
    """Create the bot with the exported bot_id, or update the existing bot of
    the same id in place so its bot-owned configuration matches the package.
    Same tenant only. All-or-nothing on MySQL; the PostgreSQL knowledge plane
    is compensated if the commit fails."""
    from backend.core.bot_clone import delete_knowledge_documents
    from backend.core.bot_transfer import import_bot, import_knowledge_plane, package_kb_ids
    from shared.bot_config import invalidate_bot_config_sync

    _require_manage(user)
    destination = _destination_tenant(user, tenant_id, package)
    try:
        report, kb_plane = import_bot(
            db, package, destination_tenant_id=destination, user=user,
            apply_environment=apply_environment,
        )
        record_audit(
            db, user=user, action="Imported bot configuration", entity_type="voice_bot",
            entity_id=report.bot_id, target_label=report.bot_name,
            tenant_id=report.tenant_id,
            new_value={
                "action": "update" if report.existing else "create",
                "applyEnvironment": apply_environment,
                "created": report.created, "updated": report.updated,
                "removed": report.removed, "reused": report.reused,
            },
            request=request,
        )
    except Exception:
        db.rollback()  # all-or-nothing: the previous live configuration survives
        raise

    created_documents: list[str] = []
    try:
        created_documents = await import_knowledge_plane(
            kb_plane, tenant_id=report.tenant_id, kb_ids=package_kb_ids(package),
            user_id=user.id,
        )
        db.commit()
    except Exception:
        db.rollback()
        await delete_knowledge_documents(created_documents)
        raise

    invalidate_bot_config_sync(report.tenant_id, report.bot_id)
    report.knowledge_documents = len(created_documents)
    return ok(report.as_dict())
