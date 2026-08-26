"""Deep-clone of a VoiceBot's configuration into a new Draft bot.

What is cloned (fresh ids everywhere, JSON deep-copied — clones never share
mutable structure or reference the source bot's id):

- the bot row itself (name gets a tenant-unique "(copy)" suffix; status Draft;
  live version, publish timestamp and call metrics reset),
- enabled languages, the readiness checklist structure (completion re-derived
  by the caller from the CLONED configuration),
- voice / STT / TTS / LLM / delivery settings,
- prompts with their full version history,
- intents (30-day confidence and test counters reset),
- bot-owned API connections (test state reset; tenant-wide connections stay
  shared and are referenced, not duplicated),
- workflows (node/edge/issue JSON with every bot-owned id remapped),
- test scenario DEFINITIONS (last_run results are execution data — reset),
- bot-scoped knowledge sources (the PostgreSQL document/chunk copy is a
  separate step — see :func:`copy_knowledge_plane`),
- the runtime context schema (stored per-customer records are customer data
  and are never cloned).

What is never cloned: conversations/recordings, customer & runtime-context
records, analytics/usage/billing rows, releases, audit history, phone-number
assignments, channel configurations (the clone must not be callable), and
workflow checkpoint/session state.

Shared tenant/global resources (voice profiles, tenant/global knowledge,
entities, guardrail profiles, tenant-wide API connections) are preserved as
associations — the clone points at the same rows the source does.
"""

import copy

from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.orm import Session

from shared.ids import new_id
from shared.models import (
    ApiConnection,
    BotLanguage,
    Intent,
    KnowledgeSource,
    Prompt,
    PromptVersion,
    RuntimeContextSchema,
    TestScenario,
    User,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    Workflow,
)

# Identity, timestamps and soft-delete state always belong to the new row.
_NEVER_COPIED = {
    "id", "created_at", "updated_at", "created_by", "updated_by",
    "is_deleted", "deleted_at", "deleted_by",
}


def _cloned_values(src, overrides: dict) -> dict:
    """Every mapped column of ``src`` deep-copied (JSON columns must never
    share structure with the source row), minus identity/audit columns."""
    values = {}
    for attr in sa_inspect(type(src)).mapper.column_attrs:
        if attr.key in _NEVER_COPIED or attr.key in overrides:
            continue
        values[attr.key] = copy.deepcopy(getattr(src, attr.key))
    values.update(overrides)
    return values


def clone_row(model, src, **overrides):
    return model(**_cloned_values(src, overrides))


def remap_ids(value, id_map: dict[str, str]):
    """Deep-copy ``value`` replacing every string equal to a cloned record's
    old id with its new id. Ids are globally unique prefixed tokens, so exact
    string equality is a safe match — this catches scalar reference columns,
    id lists (intent kb_ids, API allow-lists) and ids buried anywhere inside
    workflow node/edge config JSON."""
    if isinstance(value, str):
        return id_map.get(value, value)
    if isinstance(value, list):
        return [remap_ids(v, id_map) for v in value]
    if isinstance(value, dict):
        return {k: remap_ids(v, id_map) for k, v in value.items()}
    return value


def unique_copy_name(db: Session, tenant_id: str, source_name: str,
                     *, max_length: int = 200) -> str:
    """"Name (copy)", then "Name (copy 2)", … — unique among the tenant's
    non-deleted bots, trimmed so the suffix always fits the column."""
    n = 1
    while True:
        suffix = " (copy)" if n == 1 else f" (copy {n})"
        candidate = source_name[: max_length - len(suffix)].rstrip() + suffix
        taken = db.scalar(
            select(VoiceBot.id)
            .where(
                VoiceBot.tenant_id == tenant_id,
                VoiceBot.name == candidate,
                VoiceBot.is_deleted.is_(False),
            )
            .limit(1)
        )
        if not taken:
            return candidate
        n += 1


def clone_bot_deep(
    db: Session, src: VoiceBot, user: User
) -> tuple[VoiceBot, dict[str, str], dict[str, int]]:
    """Create the configuration clone on the caller's session (flush only —
    the caller owns the transaction and the rollback).

    Returns ``(clone, kb_id_map, summary)`` where ``kb_id_map`` maps each
    cloned bot-scoped knowledge source's old id → new id (input for the
    PostgreSQL chunk copy) and ``summary`` is audit-friendly clone counts.
    """
    id_map: dict[str, str] = {}

    clone = clone_row(
        VoiceBot, src,
        id=new_id("bot"),
        name=unique_copy_name(db, src.tenant_id, src.name),
        status="draft",
        version="v0.1.0",
        live_version=None,
        published_at=None,
        health="neutral",
        containment=0.0,
        avg_cost_per_call=0,
        csat=0.0,
        # A super admin clones on the tenant's behalf; the owner must stay a
        # member of the bot's tenant.
        owner_user_id=user.id if user.tenant_id == src.tenant_id else src.owner_user_id,
        created_by=user.id,
    )
    db.add(clone)
    id_map[src.id] = clone.id
    db.flush()  # every child row references the bot id

    for lang in db.scalars(select(BotLanguage).where(BotLanguage.bot_id == src.id)):
        db.add(BotLanguage(bot_id=clone.id, language_code=lang.language_code))

    # Checklist structure follows the source; completion is re-derived by the
    # caller (shared.readiness) from the CLONED configuration, never copied.
    for item in src.readiness_items:
        db.add(
            VoiceBotReadiness(
                id=new_id("rd"), bot_id=clone.id, item_key=item.item_key,
                label=item.label, done=False, studio_tab=item.studio_tab,
                sort_order=item.sort_order,
            )
        )

    settings_row = db.scalar(
        select(VoiceBotSetting).where(VoiceBotSetting.bot_id == src.id)
    )
    if settings_row is not None:
        db.add(clone_row(
            VoiceBotSetting, settings_row,
            id=new_id("vbs"), bot_id=clone.id, created_by=user.id,
        ))

    api_clones = []
    for row in db.scalars(select(ApiConnection).where(
        ApiConnection.bot_id == src.id, ApiConnection.is_deleted.is_(False)
    )):
        dup = clone_row(
            ApiConnection, row,
            id=new_id("api"), bot_id=clone.id,
            # Same name on purpose: workflow api-nodes may address the
            # connection by name, and resolution is bot-scoped.
            status="untested", last_tested_at=None, last_latency_ms=0,
            created_by=user.id,
        )
        id_map[row.id] = dup.id
        api_clones.append(dup)
        db.add(dup)

    kb_id_map: dict[str, str] = {}
    for row in db.scalars(select(KnowledgeSource).where(
        KnowledgeSource.bot_id == src.id, KnowledgeSource.is_deleted.is_(False)
    )):
        dup = clone_row(
            KnowledgeSource, row,
            id=new_id("ks"), bot_id=clone.id, usage_30d=0, created_by=user.id,
        )
        id_map[row.id] = dup.id
        kb_id_map[row.id] = dup.id
        db.add(dup)

    wf_clones = []
    for row in db.scalars(select(Workflow).where(
        Workflow.bot_id == src.id, Workflow.is_deleted.is_(False)
    )):
        dup = clone_row(Workflow, row, id=new_id("wf"), bot_id=clone.id,
                        created_by=user.id)
        id_map[row.id] = dup.id
        wf_clones.append(dup)
        db.add(dup)

    prompts = db.scalars(select(Prompt).where(
        Prompt.bot_id == src.id, Prompt.is_deleted.is_(False)
    )).all()
    for p in prompts:
        dup = clone_row(Prompt, p, id=new_id("pr"), bot_id=clone.id,
                        created_by=user.id)
        id_map[p.id] = dup.id
        db.add(dup)
        for v in p.versions:
            db.add(clone_row(PromptVersion, v, id=new_id("prv"), prompt_id=dup.id))

    intent_clones = []
    for row in db.scalars(select(Intent).where(
        Intent.bot_id == src.id, Intent.is_deleted.is_(False)
    )):
        dup = clone_row(
            Intent, row,
            id=new_id("in"), bot_id=clone.id,
            avg_confidence_30d=0.0, test_pass=0, test_total=0,
            created_by=user.id,
        )
        id_map[row.id] = dup.id
        intent_clones.append(dup)
        db.add(dup)

    scenario_count = 0
    for row in db.scalars(select(TestScenario).where(
        TestScenario.bot_id == src.id, TestScenario.is_deleted.is_(False)
    )):
        db.add(clone_row(TestScenario, row, id=new_id("ts"), bot_id=clone.id,
                         last_run=None, created_by=user.id))
        scenario_count += 1

    schema_clone = None
    schema_row = db.scalar(select(RuntimeContextSchema).where(
        RuntimeContextSchema.bot_id == src.id,
        RuntimeContextSchema.is_deleted.is_(False),
    ))
    if schema_row is not None:
        schema_clone = clone_row(
            RuntimeContextSchema, schema_row,
            id=new_id("rcs"), bot_id=clone.id, created_by=user.id,
        )
        db.add(schema_clone)

    # Second pass — now that every clone id is known, point cross-references
    # among the clones at the new records. Anything NOT in id_map (tenant or
    # global shared resources) keeps its original id: association preserved.
    for dup in intent_clones:
        dup.workflow_id = remap_ids(dup.workflow_id, id_map)
        dup.api_connection_id = remap_ids(dup.api_connection_id, id_map)
        dup.kb_ids = remap_ids(dup.kb_ids, id_map)
    for dup in api_clones:
        dup.allowed_intents = remap_ids(dup.allowed_intents, id_map)
        dup.allowed_workflows = remap_ids(dup.allowed_workflows, id_map)
    for dup in wf_clones:
        dup.nodes = remap_ids(dup.nodes, id_map)
        dup.edges = remap_ids(dup.edges, id_map)
        dup.issues = remap_ids(dup.issues, id_map)
    if schema_clone is not None:
        schema_clone.api_connection_id = remap_ids(
            schema_clone.api_connection_id, id_map
        )

    db.flush()

    summary = {
        "languages": len(clone.languages),
        "prompts": len(prompts),
        "intents": len(intent_clones),
        "apiConnections": len(api_clones),
        "workflows": len(wf_clones),
        "knowledgeSources": len(kb_id_map),
        "testScenarios": scenario_count,
    }
    return clone, kb_id_map, summary


async def copy_knowledge_plane(
    kb_id_map: dict[str, str], *, user_id: str
) -> list[str]:
    """Copy the PostgreSQL documents and chunks of each cloned bot-scoped
    knowledge source under its new kb id.

    Embeddings are copied verbatim — cloning never calls an embedding (or any
    other) provider. Original files on disk are shared via storage_path:
    document deletion only soft-deletes rows, never removes files.

    Returns the new document ids so the caller can compensate (delete them)
    if the MySQL commit fails after this copy has been committed.
    """
    if not kb_id_map:
        return []
    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

    created: list[str] = []
    async with get_pg_sessionmaker()() as session:
        for old_kb, new_kb in kb_id_map.items():
            docs = (await session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.kb_id == old_kb,
                    KnowledgeDocument.is_deleted.is_(False),
                )
            )).scalars().all()
            for doc in docs:
                new_doc_id = new_id("kdoc")
                session.add(clone_row(
                    KnowledgeDocument, doc,
                    id=new_doc_id, kb_id=new_kb, created_by=user_id,
                ))
                created.append(new_doc_id)
                await session.flush()  # chunk rows reference the document
                chunks = (await session.execute(
                    select(KnowledgeChunk).where(
                        KnowledgeChunk.document_id == doc.id,
                        KnowledgeChunk.is_deleted.is_(False),
                    )
                )).scalars().all()
                for chunk in chunks:
                    session.add(clone_row(
                        KnowledgeChunk, chunk,
                        id=new_id("chk"), kb_id=new_kb,
                        document_id=new_doc_id, created_by=user_id,
                    ))
        await session.commit()
    return created


async def delete_knowledge_documents(document_ids: list[str]) -> None:
    """Compensation for :func:`copy_knowledge_plane` — hard-deletes the copied
    documents (chunks follow via FK cascade / explicit delete)."""
    if not document_ids:
        return
    from sqlalchemy import delete as sa_delete

    from shared.db.postgres import get_pg_sessionmaker
    from shared.knowledge.models import KnowledgeChunk, KnowledgeDocument

    async with get_pg_sessionmaker()() as session:
        await session.execute(sa_delete(KnowledgeChunk).where(
            KnowledgeChunk.document_id.in_(document_ids)
        ))
        await session.execute(sa_delete(KnowledgeDocument).where(
            KnowledgeDocument.id.in_(document_ids)
        ))
        await session.commit()
