"""Bot Export / Import: move ONE bot between environments (local → live) as a
portable JSON package, preserving ``tenant_id`` and ``bot_id``.

Flow: local ``GET /bots/{id}/export`` → download ``bot_<id>.json`` → live
``POST /bots/import/preview`` (dry run, shows create/update + warnings) →
live ``POST /bots/import`` → the same bot_id exists on live under the same
tenant with the package's configuration.

Package layout (``schema_version`` 1)::

    {
      "kind": "echosphere.bot.export", "schema_version": 1, "exported_at": …,
      "tenant_id": "tn_…", "bot_id": "bot_…",
      "source": {tenant_name, bot_name},
      "bot": {voice_bots row, "languages": [...], "readiness": [...]},
      "resources": {                 # bot-OWNED configuration (source of truth)
        "voice_bot_settings": {...} | null,
        "prompts": [{..., "versions": [...]}], "workflows": [...],
        "intents": [...], "api_connections": [...bot-owned tools...],
        "knowledge_sources": [...bot-scoped KBs...], "test_scenarios": [...],
        "runtime_context_schema": {...} | null, "releases": [...]
      },
      "shared": {                    # tenant/platform rows the bot REFERENCES
        "guardrails", "guardrail_profiles", "voice_profiles" (platform),
        "tenant_voice_profiles" (clones), "entity_defs",
        "api_connections" (tenant-wide tools), "knowledge_sources" (reference only)
      },
      "environment": {               # environment-specific runtime assignments
        "channel_configs": [...], "phone_numbers": [...]
      },
      "knowledge_plane": {"documents": [{..., "chunks": [...]}]} | null,
      "integrity": "sha256:…"        # tamper evidence for the identity graph
    }

Semantics
---------
* **Identity is never changed.** The bot is created with exactly the exported
  ``bot_id`` when absent, or updated in place when present. Every child row
  keeps its exported id as well.
* **Same tenant only.** The destination tenant (the caller's tenant, or the
  ``tenantId`` a super admin selects) must equal the package's ``tenant_id``,
  the package must be internally consistent (every row's tenant_id/bot_id)
  and the identity manifest must match ``integrity`` — editing ``tenant_id``
  in the JSON is detected and rejected before anything is written.
* **Bot-owned resources are reconciled** — the package becomes the source of
  truth: rows present on live but absent from the package are soft-deleted
  (prompt versions, which have no soft-delete column, are removed), rows in
  the package are created/updated with their ids.
* **Shared resources are resolved, never overwritten**: reuse by id → remap
  by natural key (code/name) → create only when absent (guardrails, guardrail
  profiles, voice profiles, entity definitions, tenant-wide API connections).
  Tenant/global knowledge sources are reference-only: a missing one produces a
  warning, never an empty knowledge base.
* **Environment-specific values are preserved**: an existing live channel
  keeps its configuration (differences are reported), a live phone number is
  never stolen or replaced, and an API connection URL that points at a
  local/private host never overwrites a live URL. ``apply_environment=True``
  applies the package's channel configuration too, but still only claims a
  phone number that is free on the destination.
* **Secrets** travel as references only (``secret://`` / ``env:VAR``);
  references that do not resolve on the destination are reported so the
  operator configures them instead of the bot silently failing.
* **Transactional**: everything runs on the caller's MySQL session (flush
  only); the router commits or rolls back. The PostgreSQL knowledge plane is
  written first and compensated if the MySQL commit fails (same pattern as
  tenant transfer / bot clone). ``dry_run=True`` computes the full report and
  the caller rolls back — that is what the import preview shows.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.bot_clone import remap_ids, remap_route
from backend.core.softdelete import soft_delete
from backend.core.tenant_transfer import (
    ImportCollision,
    _apply,
    _CHANNEL_CREDENTIAL_KEYS,
    _coerce_values,
    _ENV_REFERENCE_RE,
    _Report,
    _resolve_shared,
    _revive,
    _sync_bot_languages,
    _upsert_readiness,
    dump_row,
    export_knowledge_plane,  # noqa: F401  (re-exported for the router)
    import_knowledge_plane,  # noqa: F401  (re-exported for the router)
)
from shared.errors import ApiError
from shared.ids import new_id
from shared.models import (
    ApiConnection,
    ChannelConfig,
    EntityDef,
    Guardrail,
    GuardrailProfile,
    Intent,
    KnowledgeSource,
    PhoneNumber,
    Prompt,
    PromptVersion,
    Release,
    RuntimeContextSchema,
    SupportedLanguage,
    Tenant,
    TestScenario,
    User,
    VoiceBot,
    VoiceBotSetting,
    VoiceProfile,
    Workflow,
)
from shared.readiness import refresh_readiness
from shared.secrets import resolve_secret

SCHEMA_VERSION = 1
PACKAGE_KIND = "echosphere.bot.export"

# Bot-owned sections (import order respects logical dependencies).
_OWNED_LIST_SECTIONS = (
    "api_connections", "knowledge_sources", "workflows", "prompts", "intents",
    "test_scenarios", "releases",
)
_OWNED_SINGLE_SECTIONS = ("voice_bot_settings", "runtime_context_schema")
_SHARED_SECTIONS = (
    "guardrails", "guardrail_profiles", "voice_profiles",
    "tenant_voice_profiles", "entity_defs", "api_connections", "knowledge_sources",
)
_ENVIRONMENT_SECTIONS = ("channel_configs", "phone_numbers")

# Bot statuses under which the runtime serves live traffic.
_LIVE_STATUS = "published"


class InvalidBotPackage(ApiError):
    def __init__(self, message: str):
        super().__init__(f"Invalid bot package: {message}", 422)


class TenantMismatch(ApiError):
    def __init__(self, package_tenant: str, destination_tenant: str):
        super().__init__(
            f"This package belongs to tenant '{package_tenant}' but the "
            f"destination tenant is '{destination_tenant}'. A bot can only be "
            "imported into the tenant it was exported from.",
            409,
        )


# ── Environment-specific value detection ─────────────────────────────────────

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|\[?::1\]?|"
    r"[^.]+|[^.]+\.(local|localdomain|internal|lan))$",
    re.IGNORECASE,
)


def looks_environment_local(url: str | None) -> bool:
    """True for URLs that only make sense on the machine/network they were
    written on: localhost, loopback, RFC1918 ranges, bare hostnames and
    ``.local``/``.internal``-style suffixes. Templates like ``{{base}}`` are
    treated as portable."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
    except ValueError:
        return False
    host = (parsed.hostname or "").strip()
    if not host or "{{" in host:
        return False
    return bool(_PRIVATE_HOST_RE.match(host))


# ── Integrity (identity manifest) ────────────────────────────────────────────


def _identity_manifest(package: dict) -> list:
    """Everything that binds the package to a tenant and a bot, as plain
    strings — immune to float/whitespace formatting differences between the
    Python export and a browser JSON round-trip."""
    bot = package.get("bot") or {}
    manifest: list = [
        ["package", str(package.get("tenant_id")), str(package.get("bot_id"))],
        ["bot", str(bot.get("id")), str(bot.get("tenant_id")), str(bot.get("name"))],
    ]
    resources = package.get("resources") or {}
    for section in _OWNED_LIST_SECTIONS:
        for row in resources.get(section) or []:
            manifest.append([f"resources.{section}", str(row.get("id")),
                             str(row.get("tenant_id")), str(row.get("bot_id"))])
    for section in _OWNED_SINGLE_SECTIONS:
        row = resources.get(section)
        if row:
            manifest.append([f"resources.{section}", str(row.get("id")),
                             str(row.get("tenant_id")), str(row.get("bot_id"))])
    shared = package.get("shared") or {}
    for section in _SHARED_SECTIONS:
        for row in shared.get(section) or []:
            manifest.append([f"shared.{section}", str(row.get("id")),
                             str(row.get("tenant_id"))])
    environment = package.get("environment") or {}
    for row in environment.get("channel_configs") or []:
        manifest.append(["environment.channel_configs", str(row.get("id")),
                         str(row.get("tenant_id")), str(row.get("bot_id")),
                         str(row.get("type"))])
    for row in environment.get("phone_numbers") or []:
        manifest.append(["environment.phone_numbers", str(row.get("number"))])
    return manifest


def compute_integrity(package: dict) -> str:
    canonical = json.dumps(_identity_manifest(package), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def seal_package(package: dict) -> dict:
    package["integrity"] = compute_integrity(package)
    return package


# ── Export ────────────────────────────────────────────────────────────────────


_ID_LIKE_RE = re.compile(r"^[A-Za-z]{1,8}[_-][A-Za-z0-9_-]{2,}$")


def _collect_strings(value, out: set[str]) -> None:
    """Every id-shaped string inside a JSON structure (prompt bodies and other
    prose are skipped so the reference lookups stay small)."""
    if isinstance(value, str):
        candidate = value.partition(":")[2] if ":" in value else value
        # intent routes are "workflow:<id>" / "tool:<id>"
        if len(candidate) <= 64 and _ID_LIKE_RE.match(candidate):
            out.add(candidate)
    elif isinstance(value, list):
        for v in value:
            _collect_strings(v, out)
    elif isinstance(value, dict):
        for v in value.values():
            _collect_strings(v, out)


def _voice_ids(bot: VoiceBot, settings_row: VoiceBotSetting | None) -> set[str]:
    ids = {bot.voice_id} if bot.voice_id else set()
    if settings_row is not None:
        if settings_row.voice_id:
            ids.add(settings_row.voice_id)
        for key, value in (settings_row.language_voice_map or {}).items():
            if key != "default" and isinstance(value, str) and value:
                ids.add(value)
    return ids


def export_bot(db: Session, bot: VoiceBot) -> dict:
    """Build the portable package for one bot (MySQL plane). The PostgreSQL
    knowledge plane is attached by the caller (``export_knowledge_plane``)
    and the package is then sealed with ``seal_package``."""
    tid, bid = bot.tenant_id, bot.id
    tenant = db.get(Tenant, tid)

    def owned(model, *extra):
        return db.scalars(select(model).where(
            model.bot_id == bid, model.is_deleted.is_(False), *extra
        )).all()

    settings_row = db.scalar(select(VoiceBotSetting).where(VoiceBotSetting.bot_id == bid))
    schema_row = db.scalar(select(RuntimeContextSchema).where(
        RuntimeContextSchema.bot_id == bid, RuntimeContextSchema.is_deleted.is_(False)
    ))

    bot_entry = dump_row(bot)
    bot_entry["languages"] = sorted(l.language_code for l in bot.languages)
    bot_entry["readiness"] = [dump_row(item) for item in bot.readiness_items]

    prompts_payload = []
    for prompt in owned(Prompt):
        entry = dump_row(prompt)
        entry["versions"] = [dump_row(v) for v in prompt.versions]
        prompts_payload.append(entry)

    resources = {
        "voice_bot_settings": dump_row(settings_row) if settings_row else None,
        "prompts": prompts_payload,
        "workflows": [dump_row(w) for w in owned(Workflow)],
        "intents": [dump_row(i) for i in owned(Intent)],
        "api_connections": [dump_row(a) for a in owned(ApiConnection)],
        "knowledge_sources": [
            dump_row(k) for k in owned(KnowledgeSource, KnowledgeSource.scope == "bot")
        ],
        "test_scenarios": [dump_row(t) for t in owned(TestScenario)],
        "runtime_context_schema": dump_row(schema_row) if schema_row else None,
        "releases": [dump_row(r) for r in owned(Release)],
    }

    # ── shared: what the bot references outside its own rows ──────────────
    referenced: set[str] = set()
    _collect_strings(resources, referenced)
    _collect_strings(bot_entry, referenced)

    tenant_api_rows = db.scalars(select(ApiConnection).where(
        ApiConnection.tenant_id == tid, ApiConnection.bot_id.is_(None),
        ApiConnection.is_deleted.is_(False),
    )).all()
    shared_apis = [a for a in tenant_api_rows if a.id in referenced]

    shared_kbs = db.scalars(select(KnowledgeSource).where(
        KnowledgeSource.id.in_(referenced),
        KnowledgeSource.is_deleted.is_(False),
        KnowledgeSource.scope != "bot",
    )).all() if referenced else []

    entity_names: set[str] = set()
    for intent in owned(Intent):
        for name in (intent.entities or []) + (intent.optional_entities or []):
            if isinstance(name, str):
                entity_names.add(name)
    entity_rows = db.scalars(select(EntityDef).where(
        EntityDef.tenant_id == tid, EntityDef.name.in_(entity_names),
        EntityDef.is_deleted.is_(False),
    )).all() if entity_names else []

    profile_ids = {bot.guardrail_profile_id} - {None}
    profiles = db.scalars(select(GuardrailProfile).where(
        GuardrailProfile.id.in_(profile_ids), GuardrailProfile.is_deleted.is_(False),
    )).all() if profile_ids else []
    guardrail_ids = {r.guardrail_id for p in profiles for r in p.rules}
    guardrails = db.scalars(select(Guardrail).where(
        Guardrail.id.in_(guardrail_ids), Guardrail.is_deleted.is_(False),
    )).all() if guardrail_ids else []
    profiles_payload = []
    for profile in profiles:
        entry = dump_row(profile)
        entry["rules"] = [{"id": r.id, "guardrail_id": r.guardrail_id} for r in profile.rules]
        profiles_payload.append(entry)

    voice_ids = _voice_ids(bot, settings_row)
    voices = db.scalars(select(VoiceProfile).where(
        VoiceProfile.id.in_(voice_ids), VoiceProfile.is_deleted.is_(False),
    )).all() if voice_ids else []
    platform_voices = [v for v in voices if v.tenant_id is None]
    tenant_voices = [v for v in voices if v.tenant_id == tid]

    # ── environment: runtime assignments that belong to THIS environment ──
    channels = owned(ChannelConfig)
    numbers = db.scalars(select(PhoneNumber).where(
        PhoneNumber.bot_id == bid, PhoneNumber.is_deleted.is_(False),
    )).all()

    package = {
        "kind": PACKAGE_KIND,
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tid,
        "bot_id": bid,
        "source": {
            "tenant_name": tenant.name if tenant else None,
            "bot_name": bot.name,
            "bot_status": bot.status,
        },
        "bot": bot_entry,
        "resources": resources,
        "shared": {
            "guardrails": [dump_row(g) for g in guardrails],
            "guardrail_profiles": profiles_payload,
            "voice_profiles": [dump_row(v) for v in platform_voices],
            "tenant_voice_profiles": [dump_row(v) for v in tenant_voices],
            "entity_defs": [dump_row(e) for e in entity_rows],
            "api_connections": [dump_row(a) for a in shared_apis],
            # Reference-only: documents of tenant/global knowledge are not
            # part of a bot package (they belong to the tenant).
            "knowledge_sources": [
                {"id": k.id, "tenant_id": k.tenant_id, "name": k.name,
                 "scope": k.scope, "reference_only": True}
                for k in shared_kbs
            ],
        },
        "environment": {
            "channel_configs": [dump_row(c) for c in channels],
            "phone_numbers": [
                {"number": n.number, "country": n.country, "provider": n.provider,
                 "status": n.status}
                for n in numbers
            ],
        },
        "knowledge_plane": None,
    }
    return package


# ── Validation ────────────────────────────────────────────────────────────────


def _require_list(section: dict, key: str, where: str) -> list:
    value = section.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise InvalidBotPackage(f"{where}.{key} must be a list of objects.")
    return value


def validate_package(package: dict) -> None:
    """Structural + identity validation. Raises InvalidBotPackage (422) and
    never touches the database."""
    if not isinstance(package, dict):
        raise InvalidBotPackage("expected a JSON object.")
    if package.get("kind") != PACKAGE_KIND:
        raise InvalidBotPackage(
            f"kind must be '{PACKAGE_KIND}' (got {package.get('kind')!r}). "
            "Tenant packages are imported from Admin → Organizations."
        )
    if package.get("schema_version") != SCHEMA_VERSION:
        raise InvalidBotPackage(
            f"unsupported schema_version {package.get('schema_version')!r} "
            f"(this server supports {SCHEMA_VERSION})."
        )
    tid = package.get("tenant_id")
    bid = package.get("bot_id")
    if not isinstance(tid, str) or not tid:
        raise InvalidBotPackage("tenant_id is required.")
    if not isinstance(bid, str) or not bid:
        raise InvalidBotPackage("bot_id is required.")
    bot = package.get("bot")
    if not isinstance(bot, dict) or not bot.get("id"):
        raise InvalidBotPackage("bot section with an id is required.")
    if bot.get("id") != bid:
        raise InvalidBotPackage(f"bot.id '{bot.get('id')}' does not match bot_id '{bid}'.")
    if bot.get("tenant_id", tid) != tid:
        raise InvalidBotPackage(
            f"bot.tenant_id '{bot.get('tenant_id')}' does not match tenant_id '{tid}'."
        )
    if not bot.get("name"):
        raise InvalidBotPackage("bot.name is required.")
    languages = bot.get("languages")
    if languages is not None and (
        not isinstance(languages, list) or any(not isinstance(v, str) for v in languages)
    ):
        raise InvalidBotPackage("bot.languages must be a list of language codes.")

    resources = package.get("resources")
    if not isinstance(resources, dict):
        raise InvalidBotPackage("missing 'resources' section.")
    shared = package.get("shared")
    if shared is not None and not isinstance(shared, dict):
        raise InvalidBotPackage("'shared' must be an object.")
    environment = package.get("environment")
    if environment is not None and not isinstance(environment, dict):
        raise InvalidBotPackage("'environment' must be an object.")
    unknown = set(resources) - set(_OWNED_LIST_SECTIONS) - set(_OWNED_SINGLE_SECTIONS)
    if unknown:
        raise InvalidBotPackage(
            f"unsupported resources sections: {', '.join(sorted(unknown))}."
        )
    unknown = set(shared or {}) - set(_SHARED_SECTIONS)
    if unknown:
        raise InvalidBotPackage(f"unsupported shared sections: {', '.join(sorted(unknown))}.")
    unknown = set(environment or {}) - set(_ENVIRONMENT_SECTIONS)
    if unknown:
        raise InvalidBotPackage(
            f"unsupported environment sections: {', '.join(sorted(unknown))}."
        )

    def check_owned(row: dict, where: str) -> None:
        if not row.get("id"):
            raise InvalidBotPackage(f"every row in {where} needs an id.")
        if row.get("tenant_id", tid) != tid:
            raise InvalidBotPackage(
                f"{where} row '{row['id']}' belongs to tenant '{row.get('tenant_id')}', "
                f"not the package tenant '{tid}'."
            )
        if row.get("bot_id", bid) != bid:
            raise InvalidBotPackage(
                f"{where} row '{row['id']}' belongs to bot '{row.get('bot_id')}', "
                f"not the package bot '{bid}'."
            )

    for section in _OWNED_LIST_SECTIONS:
        for row in _require_list(resources, section, "resources"):
            check_owned(row, f"resources.{section}")
    for section in _OWNED_SINGLE_SECTIONS:
        row = resources.get(section)
        if row is not None:
            if not isinstance(row, dict):
                raise InvalidBotPackage(f"resources.{section} must be an object or null.")
            check_owned(row, f"resources.{section}")
    for prompt in resources.get("prompts") or []:
        for version in _require_list(prompt, "versions", f"resources.prompts[{prompt['id']}]"):
            if not version.get("id") or version.get("version") is None:
                raise InvalidBotPackage(
                    f"prompt '{prompt['id']}' has a version without id/version."
                )
            if version.get("prompt_id", prompt["id"]) != prompt["id"]:
                raise InvalidBotPackage(
                    f"prompt version '{version['id']}' belongs to prompt "
                    f"'{version.get('prompt_id')}', not '{prompt['id']}'."
                )
    for kb in resources.get("knowledge_sources") or []:
        if kb.get("scope", "bot") != "bot":
            raise InvalidBotPackage(
                f"resources.knowledge_sources row '{kb['id']}' has scope "
                f"'{kb.get('scope')}' — only bot-scoped knowledge is bot-owned."
            )
    for conn in resources.get("api_connections") or []:
        ref = conn.get("secret_ref")
        if ref and not str(ref).startswith("secret://"):
            raise InvalidBotPackage(
                f"api connection '{conn['id']}' secret_ref must be a masked "
                "secret:// reference, never a raw secret."
            )

    for section in _SHARED_SECTIONS:
        for row in _require_list(shared or {}, section, "shared"):
            if not row.get("id"):
                raise InvalidBotPackage(f"every row in shared.{section} needs an id.")
    for row in (shared or {}).get("tenant_voice_profiles") or []:
        if row.get("tenant_id") != tid:
            raise InvalidBotPackage(
                f"shared.tenant_voice_profiles row '{row['id']}' is not owned by "
                f"tenant '{tid}'."
            )
    for row in (shared or {}).get("entity_defs") or []:
        if row.get("tenant_id", tid) != tid:
            raise InvalidBotPackage(
                f"shared.entity_defs row '{row['id']}' is not owned by tenant '{tid}'."
            )
    for row in (shared or {}).get("api_connections") or []:
        if row.get("tenant_id", tid) != tid or row.get("bot_id") is not None:
            raise InvalidBotPackage(
                f"shared.api_connections row '{row['id']}' must be a tenant-wide "
                f"connection of tenant '{tid}'."
            )
        ref = row.get("secret_ref")
        if ref and not str(ref).startswith("secret://"):
            raise InvalidBotPackage(
                f"api connection '{row['id']}' secret_ref must be a masked "
                "secret:// reference, never a raw secret."
            )
    for row in (shared or {}).get("voice_profiles") or []:
        if row.get("tenant_id") is not None:
            raise InvalidBotPackage(
                f"shared.voice_profiles row '{row['id']}' must be a platform voice."
            )

    for row in _require_list(environment or {}, "channel_configs", "environment"):
        check_owned(row, "environment.channel_configs")
        if not row.get("type"):
            raise InvalidBotPackage(f"channel config '{row['id']}' has no type.")
        config = row.get("config") or {}
        for key in _CHANNEL_CREDENTIAL_KEYS & set(config):
            value = config[key]
            if value and not _ENV_REFERENCE_RE.match(str(value)):
                raise InvalidBotPackage(
                    f"channel '{row['id']}' config.{key} must be an environment "
                    "reference like env:VAR_NAME, never a raw secret."
                )
    for row in _require_list(environment or {}, "phone_numbers", "environment"):
        if not row.get("number"):
            raise InvalidBotPackage("environment.phone_numbers rows need a number.")

    plane = package.get("knowledge_plane")
    if plane is not None:
        if not isinstance(plane, dict):
            raise InvalidBotPackage("knowledge_plane must be an object or null.")
        kb_ids = {k["id"] for k in resources.get("knowledge_sources") or []}
        for doc in _require_list(plane, "documents", "knowledge_plane"):
            if doc.get("kb_id") not in kb_ids:
                raise InvalidBotPackage(
                    f"knowledge document '{doc.get('id')}' references knowledge "
                    f"source '{doc.get('kb_id')}' which is not in the package."
                )
            if doc.get("tenant_id", tid) != tid:
                raise InvalidBotPackage(
                    f"knowledge document '{doc.get('id')}' belongs to another tenant."
                )

    integrity = package.get("integrity")
    if not integrity:
        raise InvalidBotPackage("missing integrity field — the file looks incomplete.")
    if integrity != compute_integrity(package):
        raise InvalidBotPackage(
            "the package was modified after export (tenant/bot identity or row "
            "ownership no longer matches its integrity seal). Re-export the bot "
            "instead of editing the file."
        )


# ── Import report ─────────────────────────────────────────────────────────────


class BotImportReport(_Report):
    def __init__(self) -> None:
        super().__init__()
        self.removed: dict[str, int] = {}
        self.preserved: list[dict] = []
        self.secrets_missing: list[dict] = []
        self.bot_id: str | None = None
        self.tenant_id: str | None = None
        self.bot_name: str | None = None
        self.existing: bool = False
        self.dry_run: bool = False
        self.knowledge_documents: int = 0

    def preserve(self, kind: str, label: str, reason: str, **extra) -> None:
        self.preserved.append({"kind": kind, "label": label, "reason": reason, **extra})

    def as_dict(self) -> dict:
        base = super().as_dict()
        base.update({
            "botId": self.bot_id,
            "tenantId": self.tenant_id,
            "botName": self.bot_name,
            "existing": self.existing,
            "action": "update" if self.existing else "create",
            "dryRun": self.dry_run,
            "removed": self.removed,
            "preserved": self.preserved,
            "secretsMissing": self.secrets_missing,
            "knowledgeDocuments": self.knowledge_documents,
        })
        return base


# ── Import helpers ────────────────────────────────────────────────────────────


def _upsert_owned(db: Session, model, raw: dict, *, bot: VoiceBot, user: User,
                  report: BotImportReport, label: str):
    """Id-preserving upsert of a bot-owned row. The id may already exist only
    if it belongs to this very bot (same tenant); anything else is a
    collision with another resource and aborts the import."""
    values = _coerce_values(model, raw)
    values["tenant_id"] = bot.tenant_id
    values["bot_id"] = bot.id
    existing = db.get(model, raw["id"])
    if existing is not None:
        if getattr(existing, "tenant_id", bot.tenant_id) != bot.tenant_id:
            raise ImportCollision(
                f"{label} '{raw['id']}' already exists and belongs to tenant "
                f"'{existing.tenant_id}'."
            )
        if getattr(existing, "bot_id", bot.id) != bot.id:
            raise ImportCollision(
                f"{label} '{raw['id']}' already exists and belongs to bot "
                f"'{existing.bot_id}'."
            )
        _apply(existing, values)
        _revive(existing)
        if hasattr(existing, "updated_by"):
            existing.updated_by = user.id
        report.count(report.updated, label)
        return existing
    instance = model(**values)
    if hasattr(instance, "created_by"):
        instance.created_by = user.id
    db.add(instance)
    report.count(report.created, label)
    return instance


def _retire_stale(db: Session, model, keep_ids: set[str], *, bot: VoiceBot,
                  user: User, report: BotImportReport, label: str,
                  extra_filter=None) -> None:
    """Soft-delete this bot's live rows that are not in the package. The
    package is the source of truth for bot-owned configuration; history
    (conversations) keeps referencing the retired rows, so nothing is purged."""
    stmt = select(model).where(model.bot_id == bot.id, model.is_deleted.is_(False))
    if extra_filter is not None:
        stmt = stmt.where(extra_filter)
    for row in db.scalars(stmt).all():
        if row.id not in keep_ids:
            soft_delete(row, user)
            report.count(report.removed, label)
    db.flush()


def _clear_same_bot_natural_key(db: Session, model, raw: dict, *columns: str,
                                bot: VoiceBot, report: BotImportReport,
                                label: str) -> None:
    """A live row of THIS bot holding the package row's unique key under a
    different id (e.g. an intent deleted and re-created locally under the
    same name) would violate the unique constraint — soft-deleted rows still
    hold the key. It is this bot's own retired row being replaced by the same
    logical resource, so it is removed. The same key held by another bot or
    tenant is a genuine collision."""
    conditions = [getattr(model, col) == raw.get(col) for col in columns]
    for other in db.scalars(select(model).where(*conditions, model.id != raw["id"])).all():
        if getattr(other, "bot_id", None) == bot.id and other.tenant_id == bot.tenant_id:
            db.delete(other)
            report.warnings.append(
                f"{label} '{other.id}' on this environment held the same "
                f"{'/'.join(columns)} as package row '{raw['id']}' — replaced."
            )
        else:
            key = ", ".join(f"{col}={raw.get(col)!r}" for col in columns)
            raise ImportCollision(
                f"{label} with {key} already exists on this environment under a "
                f"different id ('{other.id}' vs package '{raw['id']}')."
            )
    db.flush()


def _replace_singleton(db: Session, model, raw: dict | None, *, bot: VoiceBot,
                       user: User, report: BotImportReport, label: str,
                       soft: bool):
    """One-row-per-bot tables (voice settings, runtime-context schema): the
    package row wins with its id; a live row under a different id is removed
    (soft-deleted when the table supports it) so the unique bot_id key frees."""
    live_rows = db.scalars(select(model).where(model.bot_id == bot.id)).all()
    keep_id = raw["id"] if raw else None
    for row in live_rows:
        if row.id == keep_id:
            continue
        if soft and raw is None:
            # Nothing replaces it: retire it like any other stale row.
            if not row.is_deleted:
                soft_delete(row, user)
                report.count(report.removed, label)
            continue
        # The unique bot_id key ignores is_deleted — the package row can only
        # take it once the live row is gone.
        db.delete(row)
        if not getattr(row, "is_deleted", False):
            report.count(report.removed, label)
    db.flush()
    if raw is None:
        return None
    return _upsert_owned(db, model, raw, bot=bot, user=user, report=report, label=label)


def _resolve_tenant_shared(db: Session, shared: dict, *, tenant_id: str,
                           user: User, report: BotImportReport) -> dict[str, str]:
    """Tenant-level rows the bot references (cloned voices, entity definitions,
    tenant-wide API connections): reuse by id, remap by natural key, create
    only when absent. Content of existing rows is never modified — they are
    shared with the tenant's other bots."""
    id_map: dict[str, str] = {}

    for raw in shared.get("tenant_voice_profiles") or []:
        existing = db.get(VoiceProfile, raw["id"])
        if existing is not None:
            if existing.tenant_id not in (None, tenant_id):
                raise ImportCollision(
                    f"voice profile '{raw['id']}' belongs to tenant '{existing.tenant_id}'."
                )
            report.count(report.reused, "tenant_voice_profile")
            continue
        match = None
        if raw.get("provider") and raw.get("provider_voice_id"):
            match = db.scalar(select(VoiceProfile).where(
                VoiceProfile.tenant_id == tenant_id,
                VoiceProfile.provider == raw["provider"],
                VoiceProfile.provider_voice_id == raw["provider_voice_id"],
                VoiceProfile.is_deleted.is_(False),
            ).limit(1))
        if match is None and raw.get("name"):
            match = db.scalar(select(VoiceProfile).where(
                VoiceProfile.tenant_id == tenant_id,
                VoiceProfile.name == raw["name"],
                VoiceProfile.is_deleted.is_(False),
            ).limit(1))
        if match is not None:
            id_map[raw["id"]] = match.id
            report.remapped_ids[raw["id"]] = match.id
            report.count(report.reused, "tenant_voice_profile")
        else:
            values = _coerce_values(VoiceProfile, raw)
            values["tenant_id"] = tenant_id
            db.add(VoiceProfile(**values, created_by=user.id))
            report.count(report.created, "tenant_voice_profile")
            report.warnings.append(
                f"cloned voice '{raw.get('name')}' was created here from the package; "
                "the provider-side clone must exist in this environment's provider account."
            )

    for raw in shared.get("entity_defs") or []:
        existing = db.get(EntityDef, raw["id"])
        if existing is not None:
            if existing.tenant_id != tenant_id:
                raise ImportCollision(
                    f"entity '{raw['id']}' belongs to tenant '{existing.tenant_id}'."
                )
            if existing.is_deleted:
                _revive(existing)
                report.warnings.append(
                    f"entity '{existing.name}' was deleted here — restored because "
                    "the imported bot's intents use it."
                )
            report.count(report.reused, "entity_def")
            continue
        match = db.scalar(select(EntityDef).where(
            EntityDef.tenant_id == tenant_id, EntityDef.name == raw.get("name"),
        ).limit(1))
        if match is not None:
            if match.is_deleted:
                _revive(match)
            id_map[raw["id"]] = match.id
            report.remapped_ids[raw["id"]] = match.id
            report.count(report.reused, "entity_def")
        else:
            values = _coerce_values(EntityDef, raw)
            values["tenant_id"] = tenant_id
            db.add(EntityDef(**values, created_by=user.id))
            report.count(report.created, "entity_def")

    for raw in shared.get("api_connections") or []:
        existing = db.get(ApiConnection, raw["id"])
        if existing is not None:
            if existing.tenant_id != tenant_id:
                raise ImportCollision(
                    f"api connection '{raw['id']}' belongs to tenant '{existing.tenant_id}'."
                )
            if existing.bot_id is not None:
                raise ImportCollision(
                    f"api connection '{raw['id']}' is owned by bot '{existing.bot_id}' "
                    "here but is tenant-wide in the package."
                )
            if existing.is_deleted:
                _revive(existing)
                report.warnings.append(
                    f"tenant API connection '{existing.name}' was deleted here — "
                    "restored because the imported bot uses it."
                )
            report.count(report.reused, "tenant_api_connection")
            _check_secret(existing.secret_ref, f"tenant API connection '{existing.name}'",
                          report)
            continue
        match = db.scalar(select(ApiConnection).where(
            ApiConnection.tenant_id == tenant_id, ApiConnection.bot_id.is_(None),
            ApiConnection.name == raw.get("name"), ApiConnection.is_deleted.is_(False),
        ).limit(1))
        if match is not None:
            id_map[raw["id"]] = match.id
            report.remapped_ids[raw["id"]] = match.id
            report.count(report.reused, "tenant_api_connection")
            _check_secret(match.secret_ref, f"tenant API connection '{match.name}'", report)
        else:
            values = _coerce_values(ApiConnection, raw)
            values["tenant_id"] = tenant_id
            values["bot_id"] = None
            db.add(ApiConnection(**values, created_by=user.id))
            report.count(report.created, "tenant_api_connection")
            if looks_environment_local(raw.get("url")):
                report.warnings.append(
                    f"tenant API connection '{raw.get('name')}' was created with the "
                    f"source environment's URL '{raw.get('url')}' — point it at this "
                    "environment's service."
                )
            _check_secret(raw.get("secret_ref"), f"tenant API connection '{raw.get('name')}'",
                          report)

    for raw in shared.get("knowledge_sources") or []:
        existing = db.get(KnowledgeSource, raw["id"])
        if existing is None or existing.is_deleted:
            report.warnings.append(
                f"knowledge source '{raw.get('name')}' ({raw['id']}, {raw.get('scope')} "
                "scope) is referenced by the bot but does not exist on this environment "
                "— create/index it here; the reference is kept."
            )
        elif existing.tenant_id not in (None, tenant_id):
            raise ImportCollision(
                f"knowledge source '{raw['id']}' belongs to tenant '{existing.tenant_id}'."
            )
        else:
            report.count(report.reused, "knowledge_source")

    db.flush()
    return id_map


def _check_secret(reference: str | None, owner: str, report: BotImportReport) -> None:
    """A reference that resolves to nothing here means the operator must set
    the secret in this environment — say so instead of letting the bot fail
    at call time."""
    if not reference:
        return
    if not resolve_secret(str(reference)):
        entry = {"owner": owner, "reference": str(reference)}
        if entry not in report.secrets_missing:
            report.secrets_missing.append(entry)


def _config_diff_keys(live: dict | None, package: dict | None) -> list[str]:
    live, package = live or {}, package or {}
    return sorted(k for k in set(live) | set(package) if live.get(k) != package.get(k))


def _find_number(db: Session, number: str) -> PhoneNumber | None:
    row = db.scalar(select(PhoneNumber).where(PhoneNumber.number == number))
    if row is not None:
        return row
    digits = re.sub(r"[^\d+]", "", number)
    for candidate in db.scalars(select(PhoneNumber).where(PhoneNumber.is_deleted.is_(False))):
        if re.sub(r"[^\d+]", "", candidate.number) == digits:
            return candidate
    return None


def _claim_number_if_free(db: Session, bot: VoiceBot, config: dict, user: User,
                          report: BotImportReport) -> bool:
    """Assign the voice channel's number to the bot ONLY when nobody else
    holds it. Never steals: a number serving another bot/tenant is reported."""
    number = config.get("phoneNumber")
    if not number:
        return False
    row = _find_number(db, number)
    if row is None:
        db.add(PhoneNumber(
            id=new_id("pn"), number=number, tenant_id=bot.tenant_id, bot_id=bot.id,
            provider=config.get("telephonyProvider"), status="assigned",
            created_by=user.id,
        ))
        report.count(report.created, "phone_number")
        report.warnings.append(
            f"phone number {number} was not known here — registered and assigned to "
            "the bot from the package; confirm it is routed to this environment."
        )
        return True
    if (row.bot_id and row.bot_id != bot.id) or (row.tenant_id and row.tenant_id != bot.tenant_id):
        report.warnings.append(
            f"phone number {number} is assigned to another channel on this environment "
            "— not reassigned."
        )
        return False
    if not row.is_active and row.bot_id != bot.id:
        report.warnings.append(f"phone number {number} is deactivated here — not assigned.")
        return False
    row.tenant_id = bot.tenant_id
    row.bot_id = bot.id
    row.status = "assigned"
    row.provider = config.get("telephonyProvider") or row.provider
    row.updated_by = user.id
    if row.is_deleted:
        _revive(row)
    report.count(report.updated, "phone_number")
    return True


def _release_number(db: Session, bot: VoiceBot, number: str, user: User,
                    report: BotImportReport) -> None:
    row = _find_number(db, number)
    if row is not None and row.bot_id == bot.id:
        row.bot_id = None
        row.tenant_id = None
        row.status = "available"
        row.updated_by = user.id
        report.warnings.append(
            f"phone number {number} was released from the bot (replaced by the "
            "package's number)."
        )


def _reconcile_environment(db: Session, environment: dict, *, bot: VoiceBot,
                           existing_bot: bool, user: User, report: BotImportReport,
                           apply_environment: bool) -> None:
    """Channels and phone numbers are runtime assignments of THIS environment.

    Default: an existing live channel keeps its configuration (differences are
    reported); a channel type the bot does not have here is created from the
    package but DISABLED and without claiming its phone number, so nothing
    from the source environment starts routing traffic unreviewed.
    ``apply_environment``: the package's channel configuration is applied and
    a free phone number is claimed — a held number is still never taken."""
    live_channels = {
        row.type: row for row in db.scalars(select(ChannelConfig).where(
            ChannelConfig.bot_id == bot.id
        )).all()
    }
    package_channels = environment.get("channel_configs") or []
    package_types = {c.get("type") for c in package_channels}

    for raw in package_channels:
        ctype = raw["type"]
        config = copy.deepcopy(raw.get("config") or {})
        for key in _CHANNEL_CREDENTIAL_KEYS & set(config):
            _check_secret(config[key], f"channel '{ctype}' {key}", report)
        live = live_channels.get(ctype)
        if live is not None and not live.is_deleted:
            diff = _config_diff_keys(live.config, config)
            if not apply_environment:
                if diff or live.workflow_name != raw.get("workflow_name"):
                    report.preserve(
                        "channel", ctype,
                        "kept this environment's channel configuration",
                        differs=diff,
                    )
                    report.warnings.append(
                        f"channel '{ctype}' kept its configuration on this environment"
                        + (f" (package differs in: {', '.join(diff)})" if diff else "")
                        + ". Use 'apply environment values' to overwrite it."
                    )
                else:
                    report.count(report.reused, "channel_config")
                continue
            # Explicit apply: overwrite config, keep the channel's live id and
            # gate state, never steal a number.
            if ctype == "voice" and config.get("phoneNumber"):
                live_number = (live.config or {}).get("phoneNumber")
                if live_number and live_number != config["phoneNumber"]:
                    claimed = _claim_number_if_free(db, bot, config, user, report)
                    if claimed:
                        # Same semantics as saving the channel with a new
                        # number: the bot's previous number returns to the pool.
                        _release_number(db, bot, live_number, user, report)
                    else:
                        config["phoneNumber"] = live_number
                        report.preserve("phone_number", live_number,
                                        "package number unavailable here — live number kept")
                else:
                    _claim_number_if_free(db, bot, config, user, report)
            live.config = config
            live.detail = raw.get("detail")
            live.workflow_name = raw.get("workflow_name")
            live.updated_by = user.id
            report.count(report.updated, "channel_config")
            continue

        # No usable live channel of this type: create from the package.
        row = live if live is not None else None  # soft-deleted row holds the unique key
        if row is None and db.get(ChannelConfig, raw["id"]) is not None:
            other = db.get(ChannelConfig, raw["id"])
            raise ImportCollision(
                f"channel config '{raw['id']}' already exists and belongs to bot "
                f"'{other.bot_id}'."
            )
        values = _coerce_values(ChannelConfig, raw)
        values.update(tenant_id=bot.tenant_id, bot_id=bot.id, config=config)
        values["last_test"] = None
        claimed = False
        if apply_environment:
            if ctype == "voice":
                claimed = _claim_number_if_free(db, bot, config, user, report)
            values["enabled"] = bool(raw.get("enabled", True))
            values["status"] = "configured" if raw.get("status") in ("live", "configured", "testing") else raw.get("status", "configured")
        else:
            values["enabled"] = False
            values["status"] = "configured"
        if row is not None:
            values.pop("id", None)
            _apply(row, values)
            _revive(row)
            row.updated_by = user.id
        else:
            row = ChannelConfig(**values, created_by=user.id)
            db.add(row)
        report.count(report.created, "channel_config")
        if not apply_environment:
            report.warnings.append(
                f"channel '{ctype}' was imported DISABLED with the source environment's "
                "settings"
                + (f" (phone number {config.get('phoneNumber')} not assigned here)"
                   if ctype == "voice" and config.get("phoneNumber") else "")
                + " — review it in Channels, save and activate."
            )
        elif ctype == "voice" and config.get("phoneNumber") and not claimed:
            report.warnings.append(
                f"channel 'voice' imported but its number {config.get('phoneNumber')} could "
                "not be assigned to the bot here — inbound calls will not route until a "
                "live number is assigned."
            )

    for ctype, live in live_channels.items():
        if ctype not in package_types and not live.is_deleted:
            report.preserve("channel", ctype,
                            "exists only on this environment — kept")

    # Informational: how the bot's numbers differ between environments.
    live_numbers = sorted(n.number for n in db.scalars(select(PhoneNumber).where(
        PhoneNumber.bot_id == bot.id, PhoneNumber.is_deleted.is_(False),
    )).all())
    package_numbers = sorted(
        n["number"] for n in environment.get("phone_numbers") or [] if n.get("number")
    )
    if package_numbers and package_numbers != live_numbers:
        if live_numbers:
            report.preserve(
                "phone_number", ", ".join(live_numbers),
                "this environment's number assignment is kept",
                package=package_numbers,
            )
        elif not apply_environment:
            report.warnings.append(
                "the source bot had phone number(s) "
                f"{', '.join(package_numbers)}; none is assigned to the bot here."
            )
    db.flush()


# ── Import ────────────────────────────────────────────────────────────────────


def import_bot(db: Session, package: dict, *, destination_tenant_id: str, user: User,
               apply_environment: bool = False,
               dry_run: bool = False) -> tuple[BotImportReport, dict]:
    """Validate, then upsert the package on the caller's session (flush only —
    the caller commits, or rolls back for a dry run). Returns
    ``(report, knowledge_plane)``; the knowledge plane (possibly remapped) is
    the input of the separate PostgreSQL step."""
    validate_package(package)
    report = BotImportReport()
    report.dry_run = dry_run

    tid = package["tenant_id"]
    bid = package["bot_id"]
    if tid != destination_tenant_id:
        raise TenantMismatch(tid, destination_tenant_id)
    tenant = db.get(Tenant, tid)
    if tenant is None or tenant.is_deleted:
        raise InvalidBotPackage(
            f"tenant '{tid}' does not exist on this environment — import the tenant first."
        )
    report.tenant_id = tid
    report.bot_id = bid
    report.bot_name = package["bot"].get("name")

    # Existence / ownership of the bot id decides create vs update.
    existing_bot = db.get(VoiceBot, bid)
    if existing_bot is not None and existing_bot.tenant_id != tid:
        raise ImportCollision(
            f"bot '{bid}' already exists and belongs to a different tenant."
        )
    report.existing = existing_bot is not None and not existing_bot.is_deleted
    if existing_bot is not None and existing_bot.is_deleted:
        report.warnings.append(
            f"bot '{bid}' was deleted on this environment — restored by this import."
        )

    # ── shared resources: resolve, never overwrite ────────────────────────
    shared = copy.deepcopy(package.get("shared") or {})
    id_map = _resolve_shared(db, shared, report, user)
    id_map.update(_resolve_tenant_shared(db, shared, tenant_id=tid, user=user, report=report))

    bot_raw = copy.deepcopy(package["bot"])
    resources = copy.deepcopy(package["resources"])
    environment = copy.deepcopy(package.get("environment") or {})
    if id_map:
        bot_raw = remap_ids(bot_raw, id_map)
        resources = remap_ids(resources, id_map)
        environment = remap_ids(environment, id_map)
        for intent in resources.get("intents") or []:
            intent["route"] = remap_route(intent.get("route"), id_map)

    # ── the bot row ───────────────────────────────────────────────────────
    languages = bot_raw.pop("languages", None) or []
    readiness = bot_raw.pop("readiness", None) or []
    owner_id = bot_raw.get("owner_user_id")
    if owner_id:
        owner = db.get(User, owner_id)
        if owner is None or owner.tenant_id not in (None, tid):
            bot_raw["owner_user_id"] = (
                existing_bot.owner_user_id if existing_bot is not None else None
            )
            report.warnings.append(
                f"owner user '{owner_id}' does not exist here — "
                + ("the existing owner was kept." if existing_bot is not None
                   else "owner cleared.")
            )
    if bot_raw.get("voice_id") and db.get(VoiceProfile, bot_raw["voice_id"]) is None:
        raise InvalidBotPackage(
            f"bot references voice profile '{bot_raw['voice_id']}' which is neither "
            "in the package nor on this environment."
        )
    profile_id = bot_raw.get("guardrail_profile_id")
    if profile_id and db.get(GuardrailProfile, profile_id) is None:
        raise InvalidBotPackage(
            f"bot is assigned guardrail profile '{profile_id}' which is neither in "
            "the package nor on this environment."
        )
    if existing_bot is not None and not existing_bot.is_deleted:
        if existing_bot.status == _LIVE_STATUS and bot_raw.get("status") != _LIVE_STATUS:
            report.warnings.append(
                f"bot status changes from '{existing_bot.status}' to "
                f"'{bot_raw.get('status')}' — live calls are refused until it is "
                "published again."
            )
        elif existing_bot.status != _LIVE_STATUS and bot_raw.get("status") == _LIVE_STATUS:
            report.warnings.append(
                f"bot status changes from '{existing_bot.status}' to 'published' — "
                "it takes live traffic on its assigned channels immediately."
            )
    bot_raw["tenant_id"] = tid
    values = _coerce_values(VoiceBot, bot_raw)
    if existing_bot is not None:
        _apply(existing_bot, values)
        _revive(existing_bot)
        existing_bot.updated_by = user.id
        bot = existing_bot
        report.count(report.updated, "bot")
    else:
        bot = VoiceBot(**values, created_by=user.id)
        db.add(bot)
        report.count(report.created, "bot")
    db.flush()

    known_codes = set(db.scalars(select(SupportedLanguage.code)).all())
    try:
        _sync_bot_languages(db, bot.id, languages, known_codes)
    except ApiError as exc:  # tenant_transfer's InvalidPackage → bot wording
        raise InvalidBotPackage(str(exc.message).removeprefix("Invalid tenant package: ")) from exc
    _upsert_readiness(db, bot.id, readiness, user)
    db.flush()

    # ── bot-owned resources: package is the source of truth ───────────────
    # API connections first: workflows/intents/schema reference them.
    conns = resources.get("api_connections") or []
    _retire_stale(db, ApiConnection, {c["id"] for c in conns}, bot=bot, user=user,
                  report=report, label="api_connection")
    for raw in conns:
        live = db.get(ApiConnection, raw["id"])
        if (live is not None and live.bot_id == bot.id and not apply_environment
                and raw.get("url") != live.url and looks_environment_local(raw.get("url"))
                and not looks_environment_local(live.url)):
            report.preserve("api_connection_url", raw.get("name") or raw["id"],
                            "package URL points at the source environment's local host",
                            live=live.url, package=raw.get("url"))
            report.warnings.append(
                f"API connection '{raw.get('name')}' kept its URL '{live.url}' "
                f"(package has local URL '{raw.get('url')}')."
            )
            raw["url"] = live.url
        elif live is None and looks_environment_local(raw.get("url")):
            report.warnings.append(
                f"API connection '{raw.get('name')}' uses the source environment's URL "
                f"'{raw.get('url')}' — point it at this environment's service."
            )
        _upsert_owned(db, ApiConnection, raw, bot=bot, user=user, report=report,
                      label="api_connection")
        _check_secret(raw.get("secret_ref"), f"API connection '{raw.get('name')}'", report)
    db.flush()

    kbs = resources.get("knowledge_sources") or []
    _retire_stale(db, KnowledgeSource, {k["id"] for k in kbs}, bot=bot, user=user,
                  report=report, label="knowledge_source",
                  extra_filter=KnowledgeSource.scope == "bot")
    for raw in kbs:
        raw["scope"] = "bot"
        _upsert_owned(db, KnowledgeSource, raw, bot=bot, user=user, report=report,
                      label="knowledge_source")
    db.flush()

    workflows = resources.get("workflows") or []
    _retire_stale(db, Workflow, {w["id"] for w in workflows}, bot=bot, user=user,
                  report=report, label="workflow")
    for raw in workflows:
        _upsert_owned(db, Workflow, raw, bot=bot, user=user, report=report, label="workflow")
    db.flush()

    prompts = resources.get("prompts") or []
    _retire_stale(db, Prompt, {p["id"] for p in prompts}, bot=bot, user=user,
                  report=report, label="prompt")
    for raw in prompts:
        raw = dict(raw)
        versions = raw.pop("versions", None) or []
        prompt = _upsert_owned(db, Prompt, raw, bot=bot, user=user, report=report,
                               label="prompt")
        db.flush()
        live_versions = db.scalars(select(PromptVersion).where(
            PromptVersion.prompt_id == prompt.id
        )).all()
        wanted_ids = {v["id"] for v in versions}
        for live in live_versions:
            if live.id not in wanted_ids:
                # Stale history, or the same version number under a different
                # id (the package row re-creates it below). Versions have no
                # soft-delete column and the (prompt, version) key is unique.
                db.delete(live)
                report.count(report.removed, "prompt_version")
        db.flush()
        for version_raw in versions:
            existing = db.get(PromptVersion, version_raw["id"])
            if existing is not None and existing.prompt_id != prompt.id:
                raise ImportCollision(
                    f"prompt version '{version_raw['id']}' belongs to prompt "
                    f"'{existing.prompt_id}'."
                )
            values = _coerce_values(PromptVersion, version_raw)
            values["prompt_id"] = prompt.id
            if existing is not None:
                _apply(existing, values)
                report.count(report.updated, "prompt_version")
            else:
                db.add(PromptVersion(**values))
                report.count(report.created, "prompt_version")
        db.flush()

    intents = resources.get("intents") or []
    _retire_stale(db, Intent, {i["id"] for i in intents}, bot=bot, user=user,
                  report=report, label="intent")
    for raw in intents:
        _clear_same_bot_natural_key(db, Intent, raw, "bot_id", "name", bot=bot,
                                    report=report, label="intent")
        _upsert_owned(db, Intent, raw, bot=bot, user=user, report=report, label="intent")
    db.flush()

    scenarios = resources.get("test_scenarios") or []
    _retire_stale(db, TestScenario, {t["id"] for t in scenarios}, bot=bot, user=user,
                  report=report, label="test_scenario")
    for raw in scenarios:
        _upsert_owned(db, TestScenario, raw, bot=bot, user=user, report=report,
                      label="test_scenario")

    releases = resources.get("releases") or []
    _retire_stale(db, Release, {r["id"] for r in releases}, bot=bot, user=user,
                  report=report, label="release")
    for raw in releases:
        _upsert_owned(db, Release, raw, bot=bot, user=user, report=report, label="release")
    db.flush()

    settings_raw = resources.get("voice_bot_settings")
    if settings_raw is not None:
        for key in ("voice_id",):
            if settings_raw.get(key) and db.get(VoiceProfile, settings_raw[key]) is None:
                raise InvalidBotPackage(
                    f"voice settings reference voice profile '{settings_raw[key]}' "
                    "which is neither in the package nor on this environment."
                )
    _replace_singleton(db, VoiceBotSetting, settings_raw, bot=bot, user=user,
                       report=report, label="voice_bot_settings", soft=False)

    schema_raw = resources.get("runtime_context_schema")
    if schema_raw is not None and schema_raw.get("api_connection_id"):
        conn = db.get(ApiConnection, schema_raw["api_connection_id"])
        if conn is None or conn.tenant_id != tid:
            raise InvalidBotPackage(
                f"runtime context schema references API connection "
                f"'{schema_raw['api_connection_id']}' which is neither in the package "
                "nor on this environment."
            )
    _replace_singleton(db, RuntimeContextSchema, schema_raw, bot=bot, user=user,
                       report=report, label="runtime_context_schema", soft=True)

    # ── environment-specific runtime assignments ──────────────────────────
    _reconcile_environment(db, environment, bot=bot, existing_bot=report.existing,
                           user=user, report=report, apply_environment=apply_environment)

    # Readiness completion reflects THIS environment's state.
    db.flush()
    db.refresh(bot)
    refresh_readiness(db, bot)
    db.flush()

    kb_plane = package.get("knowledge_plane") or {}
    if id_map and kb_plane:
        kb_plane = remap_ids(kb_plane, id_map)
    return report, kb_plane


def package_kb_ids(package: dict) -> set[str]:
    return {k["id"] for k in (package.get("resources") or {}).get("knowledge_sources") or []}
