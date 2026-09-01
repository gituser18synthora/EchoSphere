"""Tenant Copy/Paste deployment: export a tenant's complete configuration as a
portable JSON package and import it into another environment PRESERVING IDS.

Flow: local `GET /tenants/{id}/export` → copy the JSON → live
`POST /tenants/import` → the same tenant_id / bot_id / workflow_id / … exist on
live, ready to use.

What is exported (the full configuration plane a bot needs to behave the
same): the tenant row + tenant settings, tenant-owned voice profiles (clones),
pronunciation dictionaries, entity definitions, tenant compliance policies
(with wordings), API connections (tenant-wide and bot-owned; "tools"),
knowledge sources, bots (with languages and readiness checklist structure),
voice/STT/TTS/LLM settings, prompts with full version history, workflows,
intents, test-scenario definitions, runtime-context schemas, channel
configurations, and — optionally — the PostgreSQL knowledge plane (documents +
chunks with embeddings copied verbatim).

Shared platform resources the tenant references (guardrails, guardrail
profiles, platform voice profiles) are exported in a separate ``shared``
section. On import they are RESOLVED, never overwritten: an existing row with
the same id is reused as-is; a missing id is matched by natural key (code /
name) and the package's references are remapped to the live id; only when
neither exists is the row created (with the exported id). A tenant import must
never mutate platform-wide rows other tenants depend on.

What is never exported: users, subscriptions/invoices/usage/billing,
conversations, recordings, releases, audit history, runtime-context records
(customer data), voice-clone source audio, and phone-number assignments
(environment-specific).

Secrets: the platform never stores raw secrets — API connections hold
``secret://`` masked references and channel configs hold ``env:VAR``
references, both resolved from the target environment at use time. The import
validates that every secret field is a reference and rejects anything that
looks like an inline secret, so live keeps resolving from its own environment.

Import semantics (idempotent upsert, one MySQL transaction — the caller owns
commit/rollback):
- id absent on live  → created with exactly the exported id (never a new id),
- id present and owned by the same tenant / logically same resource → updated
  in place (soft-deleted rows are revived),
- id present but owned by a DIFFERENT tenant, or a natural-key conflict with a
  different id (bot+channel type, bot+intent name, tenant domain, …) → the
  whole import fails with 409 and nothing is written.
Environment-local state (call metrics, test results, connection health) is
excluded from the package: create uses model defaults, update leaves live
values untouched.

The PostgreSQL knowledge plane cannot join the MySQL transaction; like
``bot_clone.copy_knowledge_plane`` it is committed first and compensated
(created documents deleted) if the MySQL commit fails afterwards.
"""

import copy
import re
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import Session

from backend.core.bot_clone import remap_ids
from shared.errors import ApiError
from shared.ids import new_id
from shared.models import (
    ApiConnection,
    BotLanguage,
    ChannelConfig,
    CompliancePolicy,
    ComplianceWording,
    EntityDef,
    Guardrail,
    GuardrailProfile,
    GuardrailProfileRule,
    Intent,
    KnowledgeSource,
    Prompt,
    PromptVersion,
    PronunciationDictionary,
    RuntimeContextSchema,
    SupportedLanguage,
    Tenant,
    TenantSetting,
    TestScenario,
    User,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    VoiceProfile,
    Workflow,
)
from shared.readiness import refresh_readiness

SCHEMA_VERSION = 1
PACKAGE_KIND = "echosphere.tenant.export"

# Identity/audit/soft-delete columns always belong to the target environment.
_NEVER_EXPORTED = {
    "created_at", "updated_at", "created_by", "updated_by",
    "is_deleted", "deleted_at", "deleted_by",
}

# Environment-local operational state: excluded from the package so create
# starts from model defaults and update never clobbers live metrics.
_LOCAL_STATE: dict[type, set[str]] = {
    Tenant: {"health"},
    VoiceBot: {"health", "containment", "avg_cost_per_call", "csat"},
    ApiConnection: {"status", "last_tested_at", "last_latency_ms"},
    Intent: {"avg_confidence_30d", "test_pass", "test_total"},
    KnowledgeSource: {"usage_30d"},
    ChannelConfig: {"last_test"},
    TestScenario: {"last_run"},
    VoiceBotReadiness: {"done"},  # re-derived from LIVE state after import
}

_ENV_REFERENCE_RE = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")
# Channel-config keys that must hold env: references (see routers/channels.py).
_CHANNEL_CREDENTIAL_KEYS = {
    "authTokenReference", "apiKeyReference", "webhookSecretReference",
}


class InvalidPackage(ApiError):
    def __init__(self, message: str):
        super().__init__(f"Invalid tenant package: {message}", 422)


class ImportCollision(ApiError):
    def __init__(self, message: str):
        super().__init__(f"Import collision: {message}", 409)


# ── Row (de)serialization ─────────────────────────────────────────────────────


def _json_safe(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    return value


def dump_row(row, *, exclude: set[str] | None = None) -> dict:
    """Every mapped column as JSON-safe values, minus environment-local ones."""
    skip = _NEVER_EXPORTED | _LOCAL_STATE.get(type(row), set()) | (exclude or set())
    values = {}
    for attr in sa_inspect(type(row)).mapper.column_attrs:
        if attr.key in skip:
            continue
        values[attr.key] = _json_safe(copy.deepcopy(getattr(row, attr.key)))
    return values


def _coerce_values(model, raw: dict) -> dict:
    """Filter ``raw`` down to the model's known columns (never identity/audit
    fields) and parse temporal strings back into datetime/date objects."""
    values: dict = {}
    for attr in sa_inspect(model).mapper.column_attrs:
        key = attr.key
        if key in _NEVER_EXPORTED or key not in raw:
            continue
        value = raw[key]
        if isinstance(value, str):
            try:
                python_type = attr.columns[0].type.python_type
            except NotImplementedError:
                python_type = None
            if python_type is datetime:
                value = datetime.fromisoformat(value)
            elif python_type is date:
                value = date.fromisoformat(value)
        values[key] = value
    return values


# ── Export ────────────────────────────────────────────────────────────────────


def _voice_profile_ids_referenced(bots, settings_rows) -> set[str]:
    ids = {b.voice_id for b in bots if b.voice_id}
    for row in settings_rows:
        if row.voice_id:
            ids.add(row.voice_id)
        # Legacy per-language entries may be bare voice_profiles ids; locale
        # strings and provider objects simply won't match any profile row.
        for key, value in (row.language_voice_map or {}).items():
            if key != "default" and isinstance(value, str) and value:
                ids.add(value)
    return ids


def export_tenant(db: Session, tenant: Tenant) -> dict:
    """Build the portable package for a tenant (MySQL plane; the PostgreSQL
    knowledge plane is a separate async step — see export_knowledge_plane)."""
    tid = tenant.id

    def owned(model, *extra):
        return db.scalars(
            select(model).where(
                model.tenant_id == tid, model.is_deleted.is_(False), *extra
            )
        ).all()

    bots = owned(VoiceBot)
    bot_ids = [b.id for b in bots]

    def bot_owned(model):
        if not bot_ids:
            return []
        return db.scalars(
            select(model).where(
                model.bot_id.in_(bot_ids), model.is_deleted.is_(False)
            )
        ).all()

    settings_rows = (
        db.scalars(select(VoiceBotSetting).where(VoiceBotSetting.bot_id.in_(bot_ids))).all()
        if bot_ids else []
    )

    bots_payload = []
    for bot in bots:
        entry = dump_row(bot)
        entry["languages"] = sorted(l.language_code for l in bot.languages)
        entry["readiness"] = [dump_row(item) for item in bot.readiness_items]
        bots_payload.append(entry)

    prompts_payload = []
    for prompt in bot_owned(Prompt):
        entry = dump_row(prompt)
        entry["versions"] = [dump_row(v) for v in prompt.versions]
        prompts_payload.append(entry)

    policies_payload = []
    for policy in owned(CompliancePolicy):
        entry = dump_row(policy)
        entry["wordings"] = [dump_row(w) for w in policy.wordings]
        policies_payload.append(entry)

    # Shared platform resources the tenant references — resolved on import,
    # never overwritten there.
    profile_ids = {tenant.guardrail_profile_id} | {b.guardrail_profile_id for b in bots}
    profile_ids.discard(None)
    profiles = (
        db.scalars(select(GuardrailProfile).where(
            GuardrailProfile.id.in_(profile_ids),
            GuardrailProfile.is_deleted.is_(False),
        )).all()
        if profile_ids else []
    )
    guardrail_ids = {r.guardrail_id for p in profiles for r in p.rules}
    guardrails = (
        db.scalars(select(Guardrail).where(
            Guardrail.id.in_(guardrail_ids), Guardrail.is_deleted.is_(False)
        )).all()
        if guardrail_ids else []
    )
    profiles_payload = []
    for profile in profiles:
        entry = dump_row(profile)
        entry["rules"] = [
            {"id": r.id, "guardrail_id": r.guardrail_id} for r in profile.rules
        ]
        profiles_payload.append(entry)

    voice_ids = _voice_profile_ids_referenced(bots, settings_rows)
    referenced_voices = (
        db.scalars(select(VoiceProfile).where(
            VoiceProfile.id.in_(voice_ids), VoiceProfile.is_deleted.is_(False)
        )).all()
        if voice_ids else []
    )
    tenant_voices = owned(VoiceProfile)
    tenant_voice_ids = {v.id for v in tenant_voices}
    platform_voices = [
        v for v in referenced_voices
        if v.tenant_id is None and v.id not in tenant_voice_ids
    ]

    knowledge_sources = db.scalars(
        select(KnowledgeSource).where(
            KnowledgeSource.tenant_id == tid, KnowledgeSource.is_deleted.is_(False)
        )
    ).all()

    return {
        "kind": PACKAGE_KIND,
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source": {"tenant_id": tid, "name": tenant.name},
        "shared": {
            "guardrails": [dump_row(g) for g in guardrails],
            "guardrail_profiles": profiles_payload,
            "voice_profiles": [dump_row(v) for v in platform_voices],
        },
        "resources": {
            "tenant": dump_row(tenant),
            "tenant_settings": next(
                (dump_row(s) for s in db.scalars(
                    select(TenantSetting).where(TenantSetting.tenant_id == tid)
                )), None,
            ),
            "voice_profiles": [dump_row(v) for v in tenant_voices],
            "pronunciation_dictionaries": [dump_row(d) for d in owned(PronunciationDictionary)],
            "entity_defs": [dump_row(e) for e in owned(EntityDef)],
            "compliance_policies": policies_payload,
            "api_connections": [dump_row(a) for a in owned(ApiConnection)],
            "knowledge_sources": [dump_row(k) for k in knowledge_sources],
            "bots": bots_payload,
            "voice_bot_settings": [dump_row(s) for s in settings_rows],
            "prompts": prompts_payload,
            "workflows": [dump_row(w) for w in bot_owned(Workflow)],
            "intents": [dump_row(i) for i in bot_owned(Intent)],
            "test_scenarios": [dump_row(t) for t in bot_owned(TestScenario)],
            "runtime_context_schemas": [dump_row(r) for r in bot_owned(RuntimeContextSchema)],
            "channel_configs": [dump_row(c) for c in bot_owned(ChannelConfig)],
        },
        "knowledge_plane": None,
    }


async def export_knowledge_plane(tenant_id: str, kb_ids: list[str]) -> dict:
    """The tenant's PostgreSQL documents + chunks (embeddings verbatim)."""
    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

    documents = []
    if kb_ids:
        async with get_pg_sessionmaker()() as session:
            docs = (await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.kb_id.in_(kb_ids),
                    KnowledgeDocument.is_deleted.is_(False),
                )
            )).scalars().all()
            for doc in docs:
                entry = dump_row(doc)
                chunks = (await session.execute(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.document_id == doc.id,
                        KnowledgeChunk.is_deleted.is_(False),
                    )
                )).scalars().all()
                chunk_entries = []
                for chunk in chunks:
                    chunk_entry = dump_row(chunk)
                    if chunk_entry.get("embedding") is not None:
                        chunk_entry["embedding"] = [
                            float(v) for v in chunk_entry["embedding"]
                        ]
                    chunk_entries.append(chunk_entry)
                entry["chunks"] = chunk_entries
                documents.append(entry)
    return {"documents": documents}


# ── Package validation ────────────────────────────────────────────────────────


def _validate_secret_references(resources: dict) -> None:
    """Defense in depth: every secret field must be a reference, never an
    inline value. The live environment keeps resolving its own secrets."""
    for conn in resources.get("api_connections", []):
        ref = conn.get("secret_ref")
        if ref and not str(ref).startswith("secret://"):
            raise InvalidPackage(
                f"api connection '{conn.get('id')}' secret_ref must be a "
                "masked secret:// reference, never a raw secret."
            )
    for channel in resources.get("channel_configs", []):
        config = channel.get("config") or {}
        for key in _CHANNEL_CREDENTIAL_KEYS & set(config):
            value = config[key]
            if value and not _ENV_REFERENCE_RE.match(str(value)):
                raise InvalidPackage(
                    f"channel '{channel.get('id')}' config.{key} must be an "
                    "environment reference like env:VAR_NAME, never a raw secret."
                )


def validate_package(package: dict) -> dict:
    if not isinstance(package, dict):
        raise InvalidPackage("expected a JSON object.")
    if package.get("kind") != PACKAGE_KIND:
        raise InvalidPackage(f"kind must be '{PACKAGE_KIND}'.")
    if package.get("schema_version") != SCHEMA_VERSION:
        raise InvalidPackage(
            f"unsupported schema_version {package.get('schema_version')!r} "
            f"(this server supports {SCHEMA_VERSION})."
        )
    resources = package.get("resources")
    if not isinstance(resources, dict):
        raise InvalidPackage("missing 'resources' section.")
    tenant_values = resources.get("tenant")
    if not isinstance(tenant_values, dict) or not tenant_values.get("id"):
        raise InvalidPackage("resources.tenant with an id is required.")

    tid = tenant_values["id"]
    settings_row = resources.get("tenant_settings")
    rows_by_section = {
        section: resources.get(section) or []
        for section in ("voice_profiles", "pronunciation_dictionaries",
                        "entity_defs", "compliance_policies", "api_connections",
                        "knowledge_sources", "bots", "voice_bot_settings",
                        "prompts", "workflows", "intents", "test_scenarios",
                        "runtime_context_schemas", "channel_configs")
    }
    if settings_row:
        rows_by_section["tenant_settings"] = [settings_row]
    for section, rows in rows_by_section.items():
        for row in rows:
            if not row.get("id"):
                raise InvalidPackage(f"every row in resources.{section} needs an id.")
            row_tid = row.get("tenant_id", tid)
            if row_tid != tid:
                raise InvalidPackage(
                    f"resources.{section} row '{row['id']}' belongs to tenant "
                    f"'{row_tid}', not the package tenant '{tid}'."
                )
            row["tenant_id"] = row_tid
    bot_ids = {b["id"] for b in resources.get("bots") or []}
    for section in ("voice_bot_settings", "prompts", "workflows", "intents",
                    "test_scenarios", "runtime_context_schemas", "channel_configs"):
        for row in resources.get(section) or []:
            if row.get("bot_id") not in bot_ids:
                raise InvalidPackage(
                    f"resources.{section} row '{row['id']}' references bot "
                    f"'{row.get('bot_id')}' which is not in the package."
                )
    _validate_secret_references(resources)
    return resources


# ── Import ────────────────────────────────────────────────────────────────────


class _Report:
    def __init__(self) -> None:
        self.created: dict[str, int] = {}
        self.updated: dict[str, int] = {}
        self.reused: dict[str, int] = {}
        self.remapped_ids: dict[str, str] = {}
        self.warnings: list[str] = []

    def count(self, bucket: dict, key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def as_dict(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "reused": self.reused,
            "remappedIds": self.remapped_ids,
            "warnings": self.warnings,
        }


def _apply(instance, values: dict) -> None:
    for key, value in values.items():
        setattr(instance, key, value)


def _revive(instance) -> None:
    if getattr(instance, "is_deleted", False):
        instance.is_deleted = False
        instance.deleted_at = None
        instance.deleted_by = None


def _upsert(db: Session, model, raw: dict, *, tenant_id: str, user: User,
            report: _Report, label: str):
    """Id-preserving upsert of a tenant-owned row with collision protection."""
    values = _coerce_values(model, raw)
    row_id = raw["id"]
    existing = db.get(model, row_id)
    if existing is not None:
        if getattr(existing, "tenant_id", tenant_id) != tenant_id:
            raise ImportCollision(
                f"{label} '{row_id}' already exists and belongs to tenant "
                f"'{existing.tenant_id}'."
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


def _check_natural_key(db: Session, model, raw: dict, *columns: str,
                       label: str) -> None:
    """A live row holding the same natural key under a DIFFERENT id is a
    logically-same resource with a different identity — refuse to fork it."""
    conditions = [getattr(model, col) == raw.get(col) for col in columns]
    if hasattr(model, "is_deleted"):
        conditions.append(model.is_deleted.is_(False))
    other = db.scalar(select(model).where(*conditions, model.id != raw["id"]).limit(1))
    if other is not None:
        key = ", ".join(f"{col}={raw.get(col)!r}" for col in columns)
        raise ImportCollision(
            f"{label} with {key} already exists on this environment under a "
            f"different id ('{other.id}' vs package '{raw['id']}')."
        )


def _resolve_shared(db: Session, shared: dict, report: _Report,
                    user: User) -> dict[str, str]:
    """Resolve platform-shared rows: reuse by id, remap by natural key, create
    only when absent. The CONTENT of existing platform rows is never modified;
    the one exception is soft-deleted rows the imported tenant depends on,
    which are revived (with a warning) — the guardrail loader ignores deleted
    profiles, and leaving the assignment dangling would silently strip the
    bot's guardrails. A same-id row that is logically a different resource
    (mismatched code/name) aborts the import with a 409."""
    id_map: dict[str, str] = {}

    def revive_shared(row, label: str) -> None:
        if getattr(row, "is_deleted", False):
            _revive(row)
            report.warnings.append(
                f"{label} '{row.id}' was deleted on this environment — "
                "restored because the imported tenant depends on it."
            )

    for raw in shared.get("guardrails") or []:
        existing = db.get(Guardrail, raw["id"])
        if existing is not None:
            # Same id must mean the same rule: compare the stable machine
            # code when both sides have one, else the (unique) display name.
            same = (
                existing.code == raw.get("code")
                if existing.code and raw.get("code")
                else existing.name == raw.get("name")
            )
            if not same:
                raise ImportCollision(
                    f"guardrail '{raw['id']}' already exists here as "
                    f"'{existing.code or existing.name}' but the package "
                    f"expects '{raw.get('code') or raw.get('name')}'."
                )
            revive_shared(existing, "guardrail")
            report.count(report.reused, "guardrail")
            continue
        # Natural-key match includes soft-deleted rows: code/name are unique
        # even across deleted rows, so creating would collide — revive instead.
        match = db.scalar(select(Guardrail).where(
            (Guardrail.code == raw.get("code")) if raw.get("code")
            else (Guardrail.name == raw.get("name")),
        ).limit(1))
        if match is None and raw.get("code"):
            match = db.scalar(select(Guardrail).where(
                Guardrail.name == raw.get("name"),
            ).limit(1))
        if match is not None:
            revive_shared(match, "guardrail")
            id_map[raw["id"]] = match.id
            report.remapped_ids[raw["id"]] = match.id
            report.count(report.reused, "guardrail")
        else:
            values = _coerce_values(Guardrail, raw)
            db.add(Guardrail(**values, created_by=user.id))
            report.count(report.created, "guardrail")

    def warn_inactive_profile(row) -> None:
        if row.status != "active":
            report.warnings.append(
                f"guardrail profile '{row.id}' ({row.code}) is '{row.status}' "
                "on this environment — activate it for its rules to apply."
            )

    for raw in shared.get("guardrail_profiles") or []:
        rules = raw.get("rules") or []
        existing = db.get(GuardrailProfile, raw["id"])
        if existing is not None:
            if existing.code != raw.get("code"):
                raise ImportCollision(
                    f"guardrail profile '{raw['id']}' exists with code "
                    f"'{existing.code}', package has '{raw.get('code')}'."
                )
            revive_shared(existing, "guardrail profile")
            warn_inactive_profile(existing)
            report.count(report.reused, "guardrail_profile")
            continue
        match = db.scalar(select(GuardrailProfile).where(
            GuardrailProfile.code == raw.get("code"),
        ).limit(1))
        if match is not None:
            revive_shared(match, "guardrail profile")
            warn_inactive_profile(match)
            id_map[raw["id"]] = match.id
            report.remapped_ids[raw["id"]] = match.id
            report.count(report.reused, "guardrail_profile")
            continue
        values = _coerce_values(GuardrailProfile, raw)
        profile = GuardrailProfile(**values, created_by=user.id)
        db.add(profile)
        db.flush()  # rules reference the profile; pending guardrails flush too
        for rule in rules:
            guardrail_id = id_map.get(rule["guardrail_id"], rule["guardrail_id"])
            if db.get(Guardrail, guardrail_id) is None:
                raise InvalidPackage(
                    f"guardrail profile '{profile.id}' rule references "
                    f"guardrail '{guardrail_id}' which is neither in the "
                    "package nor on this environment."
                )
            # Rule-link ids carry no behavior; mint a fresh one on conflict
            # instead of failing the import over a join row.
            rule_id = rule.get("id")
            if not rule_id or db.get(GuardrailProfileRule, rule_id) is not None:
                rule_id = new_id("gpr")
            db.add(GuardrailProfileRule(
                id=rule_id, profile_id=profile.id,
                guardrail_id=guardrail_id, created_by=user.id,
            ))
        report.count(report.created, "guardrail_profile")

    for raw in shared.get("voice_profiles") or []:
        existing = db.get(VoiceProfile, raw["id"])
        if existing is not None:
            report.count(report.reused, "platform_voice_profile")
            continue
        match = db.scalar(select(VoiceProfile).where(
            VoiceProfile.tenant_id.is_(None),
            VoiceProfile.provider == raw.get("provider"),
            VoiceProfile.name == raw.get("name"),
            VoiceProfile.is_deleted.is_(False),
        ).limit(1))
        if match is not None:
            id_map[raw["id"]] = match.id
            report.remapped_ids[raw["id"]] = match.id
            report.count(report.reused, "platform_voice_profile")
        else:
            values = _coerce_values(VoiceProfile, raw)
            db.add(VoiceProfile(**values, created_by=user.id))
            report.count(report.created, "platform_voice_profile")

    return id_map


def _sync_bot_languages(db: Session, bot_id: str, codes: list[str],
                        known_codes: set[str]) -> None:
    missing = [c for c in codes if c not in known_codes]
    if missing:
        raise InvalidPackage(
            f"bot '{bot_id}' uses languages not available on this "
            f"environment: {', '.join(sorted(missing))}."
        )
    existing = {
        row.language_code: row
        for row in db.scalars(select(BotLanguage).where(BotLanguage.bot_id == bot_id))
    }
    wanted = set(codes)
    for code, row in existing.items():
        if code not in wanted:
            db.delete(row)
    for code in wanted - set(existing):
        db.add(BotLanguage(bot_id=bot_id, language_code=code))


def _upsert_readiness(db: Session, bot_id: str, items: list[dict],
                      user: User) -> None:
    existing = {
        row.item_key: row
        for row in db.scalars(select(VoiceBotReadiness).where(
            VoiceBotReadiness.bot_id == bot_id
        ))
    }
    for raw in items:
        values = _coerce_values(VoiceBotReadiness, raw)
        values["bot_id"] = bot_id
        row = existing.get(raw.get("item_key"))
        if row is not None:
            values.pop("id", None)  # checklist identity is (bot, item_key)
            _apply(row, values)
        else:
            db.add(VoiceBotReadiness(**values))


def import_tenant(db: Session, package: dict, user: User) -> tuple[dict, dict]:
    """Upsert the whole package on the caller's session (flush only — the
    caller owns the transaction). Returns ``(report, kb_plane)`` where
    ``kb_plane`` is the package's knowledge_plane section (possibly remapped)
    for the separate PostgreSQL import step."""
    resources = validate_package(package)
    report = _Report()

    id_map = _resolve_shared(db, package.get("shared") or {}, report, user)
    if id_map:
        resources = remap_ids(resources, id_map)

    tenant_values = resources["tenant"]
    tid = tenant_values["id"]

    def check_profile_assignment(owner_label: str, profile_id: str | None) -> None:
        """An assignment must never dangle: the profile is either already on
        this environment or was just created from the package's shared section
        (flushed by _resolve_shared, so db.get sees it)."""
        if profile_id and db.get(GuardrailProfile, profile_id) is None:
            raise InvalidPackage(
                f"{owner_label} is assigned guardrail profile '{profile_id}' "
                "which is neither in the package nor on this environment."
            )

    check_profile_assignment(f"tenant '{tid}'", tenant_values.get("guardrail_profile_id"))

    _check_natural_key(db, Tenant, tenant_values, "domain", label="tenant")
    if tenant_values.get("code"):
        _check_natural_key(db, Tenant, tenant_values, "code", label="tenant")
    existing_tenant = db.get(Tenant, tid)
    values = _coerce_values(Tenant, tenant_values)
    if existing_tenant is not None:
        _apply(existing_tenant, values)
        _revive(existing_tenant)
        existing_tenant.updated_by = user.id
        tenant = existing_tenant
        report.count(report.updated, "tenant")
    else:
        tenant = Tenant(**values, created_by=user.id)
        db.add(tenant)
        report.count(report.created, "tenant")
    db.flush()

    if resources.get("tenant_settings"):
        raw = dict(resources["tenant_settings"])
        existing_settings = db.scalar(
            select(TenantSetting).where(TenantSetting.tenant_id == tid)
        )
        if existing_settings is not None and existing_settings.id != raw.get("id"):
            # tenant_id is unique here: the live row IS this tenant's settings.
            raw["id"] = existing_settings.id
        _upsert(db, TenantSetting, raw, tenant_id=tid, user=user,
                report=report, label="tenant_settings")

    for raw in resources.get("voice_profiles") or []:
        _upsert(db, VoiceProfile, raw, tenant_id=tid, user=user,
                report=report, label="voice_profile")
    for raw in resources.get("pronunciation_dictionaries") or []:
        _upsert(db, PronunciationDictionary, raw, tenant_id=tid, user=user,
                report=report, label="pronunciation_dictionary")
    for raw in resources.get("entity_defs") or []:
        _check_natural_key(db, EntityDef, raw, "tenant_id", "name",
                           label="entity")
        _upsert(db, EntityDef, raw, tenant_id=tid, user=user,
                report=report, label="entity_def")

    for raw in resources.get("compliance_policies") or []:
        wordings = raw.pop("wordings", None) or []
        policy = _upsert(db, CompliancePolicy, raw, tenant_id=tid, user=user,
                         report=report, label="compliance_policy")
        db.flush()
        for wording_raw in wordings:
            existing = db.get(ComplianceWording, wording_raw["id"])
            if existing is not None:
                if existing.policy_id != policy.id:
                    raise ImportCollision(
                        f"compliance wording '{wording_raw['id']}' belongs to "
                        f"policy '{existing.policy_id}'."
                    )
                _apply(existing, _coerce_values(ComplianceWording, wording_raw))
                report.count(report.updated, "compliance_wording")
            else:
                db.add(ComplianceWording(
                    **_coerce_values(ComplianceWording, wording_raw),
                    created_by=user.id,
                ))
                report.count(report.created, "compliance_wording")

    # Tenant-wide connections first: bots and their children may reference them.
    bot_ids = {b["id"] for b in resources.get("bots") or []}
    for raw in resources.get("api_connections") or []:
        if raw.get("bot_id") not in (None, *bot_ids):
            raise InvalidPackage(
                f"api connection '{raw['id']}' references bot "
                f"'{raw.get('bot_id')}' which is not in the package."
            )
        if raw.get("bot_id") is None:
            _upsert(db, ApiConnection, raw, tenant_id=tid, user=user,
                    report=report, label="api_connection")

    db.flush()  # voice profiles / connections must be visible to lookups below
    known_codes = set(db.scalars(select(SupportedLanguage.code)).all())
    imported_bots: list[VoiceBot] = []
    for raw in resources.get("bots") or []:
        raw = dict(raw)
        languages = raw.pop("languages", None) or []
        readiness = raw.pop("readiness", None) or []
        owner_id = raw.get("owner_user_id")
        if owner_id:
            owner = db.get(User, owner_id)
            if owner is None or owner.tenant_id not in (None, tid):
                raw["owner_user_id"] = None
                report.warnings.append(
                    f"bot '{raw['id']}': owner user '{owner_id}' does not exist "
                    "here — owner cleared."
                )
        if raw.get("voice_id") and db.get(VoiceProfile, raw["voice_id"]) is None:
            raise InvalidPackage(
                f"bot '{raw['id']}' references voice profile '{raw['voice_id']}' "
                "which is neither in the package nor on this environment."
            )
        check_profile_assignment(f"bot '{raw['id']}'", raw.get("guardrail_profile_id"))
        bot = _upsert(db, VoiceBot, raw, tenant_id=tid, user=user,
                      report=report, label="bot")
        db.flush()
        _sync_bot_languages(db, bot.id, languages, known_codes)
        _upsert_readiness(db, bot.id, readiness, user)
        imported_bots.append(bot)
    db.flush()

    for raw in resources.get("voice_bot_settings") or []:
        _upsert(db, VoiceBotSetting, raw, tenant_id=tid, user=user,
                report=report, label="voice_bot_settings")
    for raw in resources.get("api_connections") or []:
        if raw.get("bot_id") is not None:
            _upsert(db, ApiConnection, raw, tenant_id=tid, user=user,
                    report=report, label="api_connection")
    for raw in resources.get("knowledge_sources") or []:
        if raw.get("bot_id") not in (None, *bot_ids):
            raise InvalidPackage(
                f"knowledge source '{raw['id']}' references bot "
                f"'{raw.get('bot_id')}' which is not in the package."
            )
        _upsert(db, KnowledgeSource, raw, tenant_id=tid, user=user,
                report=report, label="knowledge_source")
    for raw in resources.get("workflows") or []:
        _upsert(db, Workflow, raw, tenant_id=tid, user=user,
                report=report, label="workflow")

    for raw in resources.get("prompts") or []:
        raw = dict(raw)
        versions = raw.pop("versions", None) or []
        prompt = _upsert(db, Prompt, raw, tenant_id=tid, user=user,
                         report=report, label="prompt")
        db.flush()
        for version_raw in versions:
            existing = db.get(PromptVersion, version_raw["id"])
            if existing is not None:
                if existing.prompt_id != prompt.id:
                    raise ImportCollision(
                        f"prompt version '{version_raw['id']}' belongs to "
                        f"prompt '{existing.prompt_id}'."
                    )
                _apply(existing, _coerce_values(PromptVersion, version_raw))
                report.count(report.updated, "prompt_version")
            else:
                _check_natural_key(db, PromptVersion, version_raw,
                                   "prompt_id", "version", label="prompt version")
                db.add(PromptVersion(**_coerce_values(PromptVersion, version_raw)))
                report.count(report.created, "prompt_version")

    for raw in resources.get("intents") or []:
        _check_natural_key(db, Intent, raw, "bot_id", "name", label="intent")
        _upsert(db, Intent, raw, tenant_id=tid, user=user,
                report=report, label="intent")
    for raw in resources.get("test_scenarios") or []:
        _upsert(db, TestScenario, raw, tenant_id=tid, user=user,
                report=report, label="test_scenario")
    for raw in resources.get("runtime_context_schemas") or []:
        _check_natural_key(db, RuntimeContextSchema, raw, "bot_id",
                           label="runtime context schema")
        _upsert(db, RuntimeContextSchema, raw, tenant_id=tid, user=user,
                report=report, label="runtime_context_schema")
    for raw in resources.get("channel_configs") or []:
        _check_natural_key(db, ChannelConfig, raw, "bot_id", "type",
                           label="channel config")
        _upsert(db, ChannelConfig, raw, tenant_id=tid, user=user,
                report=report, label="channel_config")
    db.flush()

    # Readiness completion reflects LIVE state (channels, indexed knowledge,
    # test runs here), never the source environment's checkmarks.
    for bot in imported_bots:
        db.refresh(bot)
        refresh_readiness(db, bot)
    db.flush()

    kb_plane = package.get("knowledge_plane")
    if id_map and kb_plane:
        kb_plane = remap_ids(kb_plane, id_map)
    return report.as_dict(), kb_plane or {}


async def import_knowledge_plane(plane: dict, *, tenant_id: str,
                                 kb_ids: set[str], user_id: str) -> list[str]:
    """Replace the PostgreSQL documents/chunks of the imported knowledge
    sources with the package's content, preserving document and chunk ids.

    Commits its own session (PostgreSQL cannot join the MySQL transaction).
    Returns the created document ids so the caller can compensate (delete
    them) if the MySQL commit fails afterwards — mirroring bot_clone.
    """
    documents = (plane or {}).get("documents") or []
    if not documents:
        return []
    from sqlalchemy import delete as sa_delete

    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import IngestionJob, KnowledgeChunk, KnowledgeDocument

    for doc in documents:
        if doc.get("kb_id") not in kb_ids:
            raise InvalidPackage(
                f"knowledge document '{doc.get('id')}' references kb "
                f"'{doc.get('kb_id')}' which is not in the package."
            )

    created: list[str] = []
    async with get_pg_sessionmaker()() as session:
        doc_ids = [d["id"] for d in documents]
        foreign = (await session.execute(
            select(KnowledgeDocument.id, KnowledgeDocument.tenant_id).where(
                KnowledgeDocument.id.in_(doc_ids),
                KnowledgeDocument.tenant_id != tenant_id,
            )
        )).all()
        if foreign:
            raise ImportCollision(
                f"knowledge document '{foreign[0][0]}' already exists and "
                f"belongs to tenant '{foreign[0][1]}'."
            )
        # Replace semantics keeps repeated imports duplicate-free.
        plane_kb_ids = {d["kb_id"] for d in documents}
        old_doc_ids = (await session.execute(
            select(KnowledgeDocument.id).where(
                KnowledgeDocument.kb_id.in_(plane_kb_ids),
                KnowledgeDocument.tenant_id == tenant_id,
            )
        )).scalars().all()
        if old_doc_ids:
            await session.execute(sa_delete(IngestionJob).where(
                IngestionJob.document_id.in_(old_doc_ids)))
            await session.execute(sa_delete(KnowledgeChunk).where(
                KnowledgeChunk.document_id.in_(old_doc_ids)))
            await session.execute(sa_delete(KnowledgeDocument).where(
                KnowledgeDocument.id.in_(old_doc_ids)))
        for doc in documents:
            chunks = doc.get("chunks") or []
            session.add(KnowledgeDocument(
                **_coerce_values(KnowledgeDocument, doc), created_by=user_id,
            ))
            created.append(doc["id"])
            await session.flush()
            for chunk in chunks:
                session.add(KnowledgeChunk(
                    **_coerce_values(KnowledgeChunk, chunk), created_by=user_id,
                ))
        await session.commit()
    return created
