"""ORM → API dict serializers.

Shapes mirror src/types/domain.ts exactly (camelCase) so the frontend service
layer stays a thin fetch wrapper. Sensitive fields (password hashes, raw
secrets) are never emitted.
"""

import re
from datetime import date, datetime

from shared.models import (
    AiConfigProfile,
    ApiConnection,
    ApprovedModel,
    AuditLog,
    Country,
    DataRegion,
    Industry,
    ProviderDef,
    ProviderModel,
    ChannelConfig,
    ConversationSession,
    Currency,
    CustomerCollectionContext,
    EntityDef,
    ExchangeRate,
    Guardrail,
    HealthMetric,
    Intent,
    Integration,
    Invoice,
    KnowledgeGap,
    KnowledgeSource,
    PhoneNumber,
    Plan,
    PlatformAlert,
    Prompt,
    ProviderPricing,
    Release,
    Role,
    SipTrunk,
    Subscription,
    SupportedLanguage,
    SystemSetting,
    TenantSetting,
    TestScenario,
    User,
    VoiceBot,
    VoiceCloneAudio,
    VoiceProfile,
    Workflow,
)


def iso(value: datetime | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    return value.isoformat()


def serialize_tenant(t, *, plan: str | None, users: int, bots: int,
                     calls_month: int, minutes_month: float, mrr: float,
                     ai_cost_month: float,
                     default_languages: list[str] | None = None,
                     guardrail_profile: dict | None = None) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "code": t.code or "",
        "domain": t.domain,
        "industry": t.industry or "",
        "region": t.region or "",
        "aiProfileCode": t.ai_profile_code or "",
        "guardrailProfileId": t.guardrail_profile_id or "",
        # Summary of the assigned profile (code/name/status) — present even
        # when the profile was deactivated after assignment, so existing
        # tenants stay fully readable.
        "guardrailProfile": guardrail_profile,
        "defaultLanguages": default_languages or [],
        "plan": plan or "starter",
        "status": t.status,
        "createdAt": iso(t.created_at),
        "users": users,
        "bots": bots,
        "callsMonth": calls_month,
        "minutesMonth": round(minutes_month),
        "mrr": float(mrr),
        "aiCostMonth": round(ai_cost_month),
        "health": t.health,
        "adminEmail": t.admin_email or "",
        "callSummaryEnabled": bool(t.call_summary_enabled),
        "usePreviousCallSummary": bool(t.use_previous_call_summary),
        "website": t.website or "",
        "contactName": t.contact_name or "",
        "contactPhone": t.contact_phone or "",
        "address": t.address or "",
        "country": t.country or "",
    }


# ── Master data ───────────────────────────────────────────────────────────────


def _master_common(row, *, usage: int = 0, names: dict[str, str] | None = None) -> dict:
    names = names or {}
    return {
        "id": row.id,
        "status": row.status,
        "usageCount": usage,
        "createdAt": iso(row.created_at),
        "updatedAt": iso(row.updated_at),
        "createdBy": names.get(row.created_by) or row.created_by or "",
        "updatedBy": names.get(row.updated_by) or row.updated_by or "",
    }


def serialize_industry(row: Industry, *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "icon": row.icon or "",
        "sortOrder": row.sort_order,
        "defaultPromptTemplateId": row.default_prompt_template_id,
        "defaultGuardrailProfileId": row.default_guardrail_profile_id,
        "defaultWorkflowTemplateId": row.default_workflow_template_id,
    }


def serialize_country(row: Country, *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "name": row.name,
        "iso2": row.iso2,
        "iso3": row.iso3,
        "region": row.region,
        "sortOrder": row.sort_order,
    }


def serialize_data_region(row: DataRegion, *, usage: int = 0, names: dict | None = None) -> dict:
    country_ref = row.country_ref
    return {
        **_master_common(row, usage=usage, names=names),
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "countryId": row.country_id,
        # Kept for older API clients; new clients use countryId.
        "countryCode": country_ref.iso2.lower() if country_ref else "",
        "countryIso2": country_ref.iso2 if country_ref else "",
        "countryIso3": country_ref.iso3 if country_ref else "",
        "country": row.country or "",
        "region": row.region or "",
        "cloudProvider": row.cloud_provider or "",
        "storageRegion": row.storage_region or "",
        "databaseRegion": row.database_region or "",
        "recordingRegion": row.recording_region or "",
        "transcriptRegion": row.transcript_region or "",
        "infrastructureReady": row.infrastructure_ready,
        "sortOrder": row.sort_order,
    }


def serialize_ai_profile(row: AiConfigProfile, *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "sttProvider": row.stt_provider,
        "sttModel": row.stt_model,
        "llmProvider": row.llm_provider,
        "llmModel": row.llm_model,
        "ttsProvider": row.tts_provider,
        "ttsModel": row.tts_model,
        "defaultVoice": row.default_voice,
        "embeddingProvider": row.embedding_provider,
        "embeddingModel": row.embedding_model,
        "embeddingDimension": row.embedding_dimension,
        "rerankingModel": row.reranking_model,
        "retrievalTopK": row.retrieval_top_k,
        "retrievalThreshold": row.retrieval_threshold,
        "temperature": row.temperature,
        "maxOutputTokens": row.max_output_tokens,
        "responseTimeoutMs": row.response_timeout_ms,
        "fallbackProviders": row.fallback_providers or [],
        "costCategory": row.cost_category,
        "sortOrder": row.sort_order,
    }


def serialize_provider(row: ProviderDef, *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "kind": row.kind,
        "code": row.code,
        "name": row.name,
        "description": row.description or "",
        "website": row.website or "",
        "requiresApiKey": row.requires_api_key,
        "secretRef": row.secret_ref,  # reference only, never a raw key
        "config": row.config or {},
        "sortOrder": row.sort_order,
    }


def serialize_provider_model(row: ProviderModel, *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "code": row.code,
        "name": row.display_name,
        "displayName": row.display_name,
        "description": row.description,
        "providerCode": row.provider_code,
        "capability": row.capability,
        "languages": row.languages or [],
        "codecs": row.codecs or [],
        "sampleRates": row.sample_rates or [],
        "streaming": row.streaming,
        "paramsSchema": row.params_schema or {},
        "isDefault": row.is_default,
        "sortOrder": row.sort_order,
    }


def serialize_currency(row: "Currency", *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "code": row.code,
        "name": row.name,
        "symbol": row.symbol,
        "decimalPlaces": row.decimal_places,
        "isBase": row.is_base,
        "sortOrder": row.sort_order,
    }


def serialize_exchange_rate(row: "ExchangeRate", *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "name": f"{row.base_code} → {row.target_code}",
        "baseCode": row.base_code,
        "targetCode": row.target_code,
        # String — Numeric(18,8) survives the wire without float rounding.
        "rate": str(row.rate),
        "effectiveFrom": iso(row.effective_from),
        "source": row.source,
        "sortOrder": row.sort_order,
    }


def serialize_provider_pricing(row: "ProviderPricing", *, usage: int = 0, names: dict | None = None) -> dict:
    return {
        **_master_common(row, usage=usage, names=names),
        "name": f"{row.provider_code}/{row.model_code or '—'} · {row.component}",
        "providerCode": row.provider_code,
        "capability": row.capability,
        "modelCode": row.model_code,
        "component": row.component,
        "unit": row.unit,
        "unitPrice": str(row.unit_price),
        "sellingPrice": str(row.selling_price) if row.selling_price is not None else None,
        "currencyCode": row.currency_code,
        "effectiveFrom": iso(row.effective_from),
        "sortOrder": row.sort_order,
    }


def serialize_subscription(s: Subscription, *, tenant_name: str, plan_code: str,
                           minutes_used: float) -> dict:
    return {
        "id": s.id,
        "tenantId": s.tenant_id,
        "tenant": tenant_name,
        "plan": plan_code,
        "seats": s.seats,
        "botLimit": s.bot_limit,
        "minutesIncluded": s.minutes_included,
        "minutesUsed": round(minutes_used),
        "renewsAt": iso(s.renews_at) or "",
        "status": s.status,
        "mrr": float(s.mrr),
    }


def serialize_invoice(i: Invoice, *, tenant_name: str) -> dict:
    return {
        "id": i.id,
        "tenantId": i.tenant_id,
        "tenant": tenant_name,
        "period": i.period,
        "amount": float(i.amount),
        "status": i.status,
        "issuedAt": iso(i.issued_at) or "",
    }


def serialize_plan(p: Plan, *, usage: int = 0, names: dict | None = None) -> dict:
    names = names or {}
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "description": p.description or "",
        "priceMonthly": float(p.price_monthly),
        "priceAnnual": float(p.price_annual or 0),
        "currency": p.currency,
        "botLimit": p.bot_limit,
        "minutesIncluded": p.minutes_included,
        "seatsIncluded": p.seats_included,
        "kbLimit": p.kb_limit,
        "storageGbIncluded": p.storage_gb_included,
        "languagesIncluded": p.languages_included,
        "concurrentCallLimit": p.concurrent_call_limit,
        "monthlyCallLimit": p.monthly_call_limit,
        "monthlyTokenLimit": p.monthly_token_limit,
        "monthlyEmbeddingLimit": p.monthly_embedding_limit,
        "recordingRetentionDays": p.recording_retention_days,
        "transcriptRetentionDays": p.transcript_retention_days,
        "analyticsRetentionDays": p.analytics_retention_days,
        "features": p.features or [],
        "overageRates": p.overage_rates or {},
        "status": p.status,
        "isPublic": p.is_public,
        "isRecommended": p.is_recommended,
        "sortOrder": p.sort_order,
        "usageCount": usage,
        "createdAt": iso(p.created_at),
        "updatedAt": iso(p.updated_at),
        "createdBy": names.get(p.created_by) or p.created_by or "",
        "updatedBy": names.get(p.updated_by) or p.updated_by or "",
    }


def serialize_bot(b: VoiceBot, *, owner_name: str, channels: list[str],
                  calls_today: int, calls_month: int,
                  avg_cost_per_call: float) -> dict:
    return {
        "id": b.id,
        "tenantId": b.tenant_id,
        "name": b.name,
        "useCase": b.use_case or "",
        "description": b.description or "",
        "languages": [bl.language_code for bl in b.languages],
        "status": b.status,
        "version": b.version,
        "liveVersion": b.live_version,
        "owner": owner_name,
        "health": b.health,
        "containment": b.containment,
        "callsToday": calls_today,
        "callsMonth": calls_month,
        # Month-to-date metered AI cost / calls (usage_records rollup) — the
        # static voice_bots.avg_cost_per_call column is demo-seed-only.
        "avgCostPerCall": round(float(avg_cost_per_call), 4),
        "csat": b.csat,
        "channels": channels,
        "voiceId": b.voice_id,
        # Explicit bot-level guardrail profile; "" → inherits the tenant default.
        "guardrailProfileId": b.guardrail_profile_id or "",
        "updatedAt": iso(b.updated_at),
        "publishedAt": iso(b.published_at),
        "readiness": [
            {"id": r.item_key, "label": r.label, "done": r.done, "studioTab": r.studio_tab}
            for r in b.readiness_items
        ],
    }


def serialize_knowledge(k: KnowledgeSource) -> dict:
    return {
        "id": k.id,
        "tenantId": k.tenant_id,
        "botId": k.bot_id,
        "scope": k.scope,
        "type": k.type,
        "name": k.name,
        "detail": k.detail or "",
        "status": k.status,
        "chunks": k.chunks,
        "sizeKb": k.size_kb,
        "lastSync": iso(k.last_sync_at) or "—",
        "quality": k.quality,
        "usage30d": k.usage_30d,
        "createdAt": iso(k.created_at),
        "updatedAt": iso(k.updated_at),
    }


def serialize_knowledge_gap(g: KnowledgeGap) -> dict:
    return {
        "id": g.id,
        "question": g.question,
        "frequency": g.frequency,
        "lastAsked": iso(g.last_asked_at) or "",
        "suggestedSource": g.suggested_source or "",
    }


def serialize_prompt(p: Prompt, *, include_config: bool = True) -> dict:
    return {
        "id": p.id,
        "botId": p.bot_id,
        "type": p.type,
        "name": p.name,
        "description": p.description or "",
        "variables": p.variables or [],
        "state": p.state,
        "activeVersion": p.active_version,
        "publishedVersion": p.published_version,
        "approvedBy": p.approved_by,
        "approvedAt": iso(p.approved_at),
        "publishedAt": iso(p.published_at),
        "versions": [
            {
                "version": v.version,
                "editedBy": v.edited_by or "",
                "editedAt": iso(v.edited_at) or iso(v.created_at),
                "note": v.note or "",
                "variants": v.variants or [],
                "promptMode": v.prompt_mode or "structured",
                "structuredConfig": (v.structured_config or None) if include_config else None,
                "fullPrompt": (v.full_prompt or None) if include_config else None,
                "compiledPrompt": (v.compiled_prompt or None) if include_config else None,
                "modelCompatibility": v.model_compatibility or [],
            }
            for v in p.versions
        ],
    }


def serialize_voice(v: VoiceProfile, *, usage: int = 0) -> dict:
    return {
        "id": v.id,
        "tenantId": v.tenant_id,
        "source": v.source or "platform",
        "cloneMetadata": v.clone_metadata or {},
        "name": v.name,
        "gender": v.gender,
        "languages": v.languages or [],
        "locale": v.locale or "",
        "accent": v.accent or "",
        "styles": v.styles or [],
        "description": v.description or "",
        "latencyMs": v.latency_ms,
        "premium": v.premium,
        "sample": v.sample_text or "",
        "provider": v.provider or "",
        "providerVoiceId": v.provider_voice_id or "",
        "speakingRate": v.speaking_rate,
        "pitch": v.pitch,
        "isDefault": v.is_default,
        "status": v.status,
        "sortOrder": v.sort_order,
        "modelCodes": v.model_codes or [],
        "providerSettings": v.provider_settings or {},
        "usageCount": usage,
        "updatedAt": iso(v.updated_at),
    }


def serialize_clone_audio(a: VoiceCloneAudio) -> dict:
    """Stored source-audio sample of a cloned voice. url is the authenticated
    playback endpoint — the storage path itself is never exposed."""
    return {
        "id": a.id,
        "voiceId": a.voice_id,
        "originalFilename": a.original_filename,
        "mimeType": a.mime_type,
        "sizeBytes": a.size_bytes,
        "durationSec": a.duration_sec,
        "sourceType": a.source_type,
        "provider": a.provider or "",
        "providerVoiceId": a.provider_voice_id or "",
        "status": a.status,
        "createdBy": a.created_by,
        "createdAt": iso(a.created_at),
        "url": f"/api/v1/voice-clones/{a.voice_id}/audio/{a.id}",
    }


def serialize_language(lang: SupportedLanguage, *, usage: int = 0) -> dict:
    return {
        "id": lang.id,
        "code": lang.code,
        "name": lang.name,
        "nativeName": lang.native_name,
        "isoCode": lang.iso_code or "",
        "script": lang.script or "",
        "direction": lang.direction,
        "providerSupport": lang.provider_support or {},
        "isDefault": lang.is_default,
        "enabled": lang.enabled,
        "sortOrder": lang.sort_order,
        "usageCount": usage,
        "updatedAt": iso(lang.updated_at),
    }


def serialize_intent(i: Intent) -> dict:
    return {
        "id": i.id,
        "botId": i.bot_id,
        "name": i.name,
        "code": i.code or "",
        "category": i.category or "",
        "description": i.description or "",
        "samples": i.samples or [],
        "languages": i.languages or [],
        "confidenceThreshold": i.confidence_threshold,
        "avgConfidence30d": i.avg_confidence_30d,
        "route": i.route or "",
        "entities": i.entities or [],
        "optionalEntities": i.optional_entities or [],
        "workflowId": i.workflow_id,
        "apiConnectionId": i.api_connection_id,
        "kbIds": i.kb_ids or [],
        "priority": i.priority,
        "fallbackBehavior": i.fallback_behavior or "",
        "handoffEnabled": i.handoff_enabled,
        "status": i.status,
        "version": i.version,
        "testPass": i.test_pass,
        "testTotal": i.test_total,
        "updatedAt": iso(i.updated_at),
    }


def serialize_entity(e: EntityDef) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "code": e.code or "",
        "description": e.description or "",
        "kind": e.kind,
        "dataType": e.data_type,
        "languages": e.languages or [],
        "synonyms": e.synonyms or {},
        "allowedValues": e.allowed_values or [],
        "regexPattern": e.regex_pattern or "",
        "validationRules": e.validation_rules or {},
        "normalizationRules": e.normalization_rules or {},
        "maskingEnabled": e.masking_enabled,
        "requireConfirmation": e.require_confirmation,
        "retentionDays": e.retention_days,
        "example": e.example or "",
        "pii": e.pii,
        "status": e.status,
        "usedBy": e.used_by or [],
        "updatedAt": iso(e.updated_at),
    }


def serialize_api_connection(a: ApiConnection) -> dict:
    return {
        "id": a.id,
        "botId": a.bot_id,
        "name": a.name,
        "description": a.description or "",
        "method": a.method,
        "url": a.url,
        "authType": a.auth_type,
        "secretRef": a.secret_ref,
        "headers": a.headers or {},
        "queryParams": a.query_params or {},
        "pathParams": a.path_params or {},
        "bodyTemplate": a.body_template,
        "requestSchema": a.request_schema,
        "responseSchema": a.response_schema,
        "successCondition": a.success_condition or "",
        "successMessage": a.success_message or "",
        "failureMessage": a.failure_message or "",
        "errorMapping": a.error_mapping or {},
        "sensitiveMasks": a.sensitive_masks or [],
        "allowedIntents": a.allowed_intents or [],
        "allowedWorkflows": a.allowed_workflows or [],
        "isStateChanging": a.is_state_changing,
        "requireConfirmation": a.require_confirmation,
        "timeoutMs": a.timeout_ms,
        "retries": a.retries,
        "responseMapping": a.response_mapping or [],
        "status": a.status,
        "lastTestedAt": iso(a.last_tested_at),
        "lastLatencyMs": a.last_latency_ms,
        "version": a.version,
        "updatedAt": iso(a.updated_at),
    }


def serialize_workflow(w: Workflow, *, updated_by_name: str) -> dict:
    return {
        "id": w.id,
        "botId": w.bot_id,
        "name": w.name,
        "version": w.version,
        "status": w.status,
        "nodes": w.nodes or [],
        "edges": w.edges or [],
        "issues": w.issues or [],
        "updatedAt": iso(w.updated_at),
        "updatedBy": updated_by_name,
    }


_SECRETISH_KEY = re.compile(r"(key|secret|token|password|credential)", re.IGNORECASE)


def mask_channel_config(config: dict | None) -> dict | None:
    """Never return secret material. `env:VAR` references are not secrets (they
    name an environment variable) and pass through; any secret-looking value
    that is NOT a reference is masked defensively — validation rejects raw
    secrets on write, so this only guards legacy/hand-edited rows."""
    if config is None:
        return None
    masked = {}
    for key, value in config.items():
        if (_SECRETISH_KEY.search(key) and isinstance(value, str) and value
                and not value.startswith("env:")):
            masked[key] = "••••••••"
        else:
            masked[key] = value
    return masked


def serialize_channel(c: ChannelConfig, *, binding: dict | None = None) -> dict:
    return {
        "id": c.id,
        "type": c.type,
        "botId": c.bot_id,
        "status": c.status,
        "enabled": bool(c.enabled),
        "detail": c.detail or "",
        "workflow": c.workflow_name or "—",
        "lastTest": c.last_test,
        "config": mask_channel_config(c.config),
        "updatedAt": iso(c.updated_at),
        "binding": binding,
    }


def serialize_scenario(s: TestScenario) -> dict:
    return {
        "id": s.id,
        "botId": s.bot_id,
        "name": s.name,
        "suite": s.suite or "",
        "steps": s.steps,
        "lastRun": s.last_run,
    }


def serialize_release(r: Release) -> dict:
    return {
        "id": r.id,
        "botId": r.bot_id,
        "version": r.version,
        "stage": r.stage,
        "notes": r.notes or "",
        "requestedBy": r.requested_by or "",
        "approvedBy": r.approved_by,
        "scheduledFor": iso(r.scheduled_for),
        "publishedAt": iso(r.published_at),
        "checklist": r.checklist or [],
        "diff": r.diff or [],
    }


def serialize_conversation(c: ConversationSession, *, bot_name: str,
                           transcript: list | None = None,
                           recording: dict | None = None,
                           cost_breakdown: dict | None = None,
                           summary: dict | None = None) -> dict:
    """One conversation for the API.

    ``costUsd`` is the single authoritative total for BOTH the list and the
    detail view — a cache of the sum of this conversation's usage events (see
    shared.billing.conversation_cost), never recomputed in the client. It is
    always in the base currency (USD); display conversion happens at render
    time from the configured exchange rate, so the stored cost does not move
    when a viewer changes their display currency.

    ``cost`` carries the auditable per-component breakdown and is present on
    the detail view only — the list would otherwise run one query per row.
    """
    return {
        "id": c.id,
        "botId": c.bot_id,
        "bot": bot_name,
        "channel": c.channel,
        "caller": c.caller_masked or "•••",
        "startedAt": iso(c.started_at),
        "durationSec": c.duration_sec,
        "sentiment": c.sentiment,
        "intents": c.intents or [],
        "contained": c.contained,
        "escalationReason": c.escalation_reason,
        "csat": c.csat,
        "costUsd": float(c.cost_usd),
        # Rate view of the same stored total over the call's real length —
        # derived HERE so the client renders it rather than computing it.
        # Null (not 0) when the call never connected: a rate over zero
        # minutes is meaningless, and 0 would read as "free".
        "costPerMinuteUsd": (
            float(c.cost_usd) * 60.0 / c.duration_sec if c.duration_sec else None
        ),
        "cost": cost_breakdown,
        "language": c.language or "en-US",
        "qaScore": c.qa_score,
        "flagged": c.flagged,
        # Call outcome captured by the runtime conversation policy.
        "disposition": c.disposition,
        # Which published prompt version this call actually ran on.
        "promptId": c.prompt_id,
        "promptVersion": c.prompt_version,
        "transcript": transcript or [],
        # Present only when the audio file is actually available on disk.
        "recording": recording,
        # Post-call intelligence (AI summary / outcome / Next Best Action) —
        # detail view only, null while nothing has been generated.
        "summary": summary,
    }


def serialize_conversation_memory(m) -> dict:
    """Post-call intelligence for one conversation (camelCase, PII-free).

    Mirrors src/types/domain.ts ConversationAiSummary. The structured memory
    JSON is already validated/bounded at persistence time.
    """
    memory = m.memory or {}
    return {
        "status": m.status,
        "callOutcome": m.call_outcome,
        "summary": m.summary,
        "customerIntent": memory.get("customer_intent") or None,
        "customerSentiment": memory.get("customer_sentiment") or None,
        "customerCommitments": [
            {
                "type": c.get("type"),
                "description": c.get("description"),
                "amount": c.get("amount"),
                "currency": c.get("currency"),
                "dueDate": c.get("due_date"),
                "status": c.get("status"),
            }
            for c in (memory.get("customer_commitments") or [])
        ],
        "objections": memory.get("objections") or [],
        "importantFacts": memory.get("important_facts") or [],
        "resolvedItems": memory.get("resolved_items") or [],
        "unresolvedItems": memory.get("unresolved_items") or [],
        "missingSlots": memory.get("missing_slots") or [],
        "nextBestAction": (
            {
                "action": (m.next_best_action or {}).get("action") or m.next_action,
                "reason": (m.next_best_action or {}).get("reason"),
                "priority": (m.next_best_action or {}).get("priority"),
                "recommendedAt": (m.next_best_action or {}).get("recommended_at"),
            }
            if (m.next_best_action or m.next_action)
            else None
        ),
        "followUpRequired": bool(m.follow_up_required),
        "followUpAt": iso(m.follow_up_at) if m.follow_up_at else None,
        "confidence": float(m.confidence) if m.confidence is not None else None,
        "generatedAt": iso(m.generated_at) if m.generated_at else None,
        "error": (m.error or None) if m.status == "failed" else None,
    }


def serialize_customer_context(cc: CustomerCollectionContext) -> dict:
    """Customer collection context, always masked.

    The full phone and loan account number are write-only: this serializer is
    the ONLY read path and never emits them. Nullable business values stay
    null when unknown so clients can distinguish missing from false/zero.
    """
    from shared.customer_context import mask_tail, phone_tail

    tail = phone_tail(cc.phone)
    money = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "id": cc.id,
        "tenantId": cc.tenant_id,
        "botId": cc.bot_id,
        "customerRef": cc.customer_ref,
        "phoneMasked": mask_tail(tail, keep=4, prefix="XXXXXX"),
        "customerName": cc.customer_name,
        "dcsName": cc.dcs_name,
        "lenderName": cc.lender_name,
        "loanAccountMasked": mask_tail(cc.loan_account_number),
        "preferredLanguage": cc.preferred_language,
        "overdueAmount": money(cc.overdue_amount),
        "totalOutstanding": money(cc.total_outstanding),
        "minimumPayable": money(cc.minimum_payable),
        "penalCharges": money(cc.penal_charges),
        "daysOverdue": cc.days_overdue,
        "dueDate": iso(cc.due_date),
        "previousPromiseDate": iso(cc.previous_promise_date),
        "partialPaymentAllowed": cc.partial_payment_allowed,
        "paymentMethods": cc.payment_methods,
        "securePaymentLinkAvailable": cc.secure_payment_link_available,
        "activeOffers": cc.active_offers,
        "offerTerms": cc.offer_terms,
        "creditReportingStatus": cc.credit_reporting_status,
        "callbackNumber": mask_tail(phone_tail(cc.callback_number),
                                    keep=4, prefix="XXXXXX"),
        "grievanceContact": cc.grievance_contact,
        "paymentStatus": cc.payment_status,
        "customerVerified": cc.customer_verified,
        "recordingNoticeRequired": cc.recording_notice_required,
        "complaintPending": cc.complaint_pending,
        "accountDisputed": cc.account_disputed,
        "callbackRequested": cc.callback_requested,
        "callbackRequestedAt": iso(cc.callback_requested_at),
        "lastCallId": cc.last_call_id,
        "lastDisposition": cc.last_disposition,
        "isFinalTranscript": cc.is_final_transcript,
        "interruptionDetected": cc.interruption_detected,
        "updatedAt": iso(cc.updated_at),
    }


def serialize_alert(a: PlatformAlert) -> dict:
    return {
        "id": a.id,
        "severity": a.severity,
        "title": a.title,
        "source": a.source or "",
        "time": iso(a.occurred_at) or iso(a.created_at),
        "status": a.status,
        "scope": a.scope,
    }


def serialize_audit(a: AuditLog, *, tenant_name: str | None) -> dict:
    return {
        "id": a.id,
        "actor": a.actor_name or "System",
        "actorRole": a.actor_role or "system",
        "action": a.action,
        "target": a.target_label or a.entity_id or "",
        "tenant": tenant_name,
        "time": iso(a.created_at),
        "ip": a.ip_address or "—",
        "entityType": a.entity_type,
        "entityId": a.entity_id,
    }


def serialize_team_member(u: User, *, bots_owned: int) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "role": u.role.name,
        "roleCode": u.role.code,
        "status": u.status,
        "lastActive": iso(u.last_active_at) or "—",
        "botsOwned": bots_owned,
        "mfa": u.mfa_enabled,
    }


def serialize_role(r: Role, *, members: int) -> dict:
    return {
        "id": r.id,
        "code": r.code,
        "name": r.name,
        "description": r.description or "",
        "scope": r.scope,
        "permissions": [p.code for p in r.permissions],
        "permissionCount": len(r.permissions),
        "members": members,
    }


def serialize_integration(i: Integration, *, status: str, connected_at) -> dict:
    return {
        "id": i.id,
        "name": i.name,
        "category": i.category or "",
        "description": i.description or "",
        "status": status,
        "connectedAt": iso(connected_at),
    }


def serialize_model(m: ApprovedModel) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "provider": m.provider or "",
        "purpose": m.purpose,
        "status": m.status,
        "tenantsUsing": m.tenants_using,
        "costPer1k": float(m.cost_per_1k),
        "latencyP50": m.latency_p50,
    }


def serialize_guardrail(g: Guardrail) -> dict:
    return {
        "id": g.id,
        "code": g.code or "",
        "name": g.name,
        "category": g.category or "",
        "description": g.description or "",
        "enforcement": g.enforcement,
        "enabled": g.enabled,
        "isMandatory": bool(g.is_mandatory),
        "triggers30d": g.triggers_30d,
    }


def serialize_guardrail_profile(p, guardrails: list[Guardrail], *, usage: int = 0) -> dict:
    """`guardrails` are the profile's linked rule rows (resolved by the caller)."""
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "description": p.description or "",
        "status": p.status,
        "version": p.version,
        "usageCount": usage,
        "guardrailIds": [g.id for g in guardrails],
        "guardrails": [serialize_guardrail(g) for g in guardrails],
        "createdAt": iso(p.created_at),
        "updatedAt": iso(p.updated_at),
        "createdBy": p.created_by or "",
        "updatedBy": p.updated_by or "",
    }


def serialize_guardrail_trigger(t) -> dict:
    return {
        "id": t.id,
        "tenantId": t.tenant_id or "",
        "botId": t.bot_id or "",
        "sessionId": t.session_id or "",
        "guardrailId": t.guardrail_id or "",
        "guardrailCode": t.guardrail_code,
        "ruleName": t.rule_name or "",
        "action": t.action,
        "stage": t.stage,
        "detail": t.detail or "",
        "profileId": t.profile_id or "",
        "profileVersion": t.profile_version,
        "policyCode": t.policy_code or "",
        "policyVersion": t.policy_version,
        "outcome": t.outcome or "",
        "channel": t.channel or "",
        "createdAt": iso(t.created_at),
    }


def serialize_phone_number(p: PhoneNumber, *, tenant_name: str | None,
                           bot_name: str | None) -> dict:
    return {
        "id": p.id,
        "number": p.number,
        "country": p.country or "",
        "tenant": tenant_name,
        "bot": bot_name,
        "provider": p.provider or "",
        "status": p.status,
        "isActive": bool(p.is_active),
        "monthlyCost": float(p.monthly_cost),
    }


def serialize_sip_trunk(t: SipTrunk) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "provider": t.provider or "",
        "region": t.region or "",
        "capacityLines": t.capacity_lines,
        "activeCalls": t.active_calls,
        "failurePct": t.failure_pct,
        "status": t.status,
    }


def serialize_health_metric(h: HealthMetric) -> dict:
    return {
        "name": h.name,
        "status": h.status,
        "value": h.value or "",
        "target": h.target or "",
        "spark": h.spark or [],
    }


def serialize_tenant_settings(s: TenantSetting) -> dict:
    return {
        "tenantId": s.tenant_id,
        "displayName": s.display_name,
        "timezone": s.timezone,
        "defaultLanguages": s.default_languages or [],
        "branding": s.branding or {},
        "businessHours": s.business_hours or {},
        "holidays": s.holidays or [],
        "notifications": s.notifications or [],
        "security": s.security or {},
        "retentionDays": s.retention_days,
    }


def serialize_user_public(u: User) -> dict:
    """Login/me payload — no password hash, ever."""
    return {
        "id": u.id,
        "name": u.name,
        "firstName": u.first_name or (u.name.split(" ")[0] if u.name else ""),
        "lastName": u.last_name or (" ".join(u.name.split(" ")[1:]) if u.name and " " in u.name else ""),
        "email": u.email,
        "phone": u.phone or "",
        "avatarUrl": u.avatar_url or "",
        "locale": u.locale or "",
        "timezone": u.timezone or "",
        "role": u.role.code,
        "roleName": u.role.name,
        "tenantId": u.tenant_id,
        "permissions": [p.code for p in u.role.permissions],
        "status": u.status,
        "lastLoginAt": iso(u.last_login_at),
        "passwordChangedAt": iso(u.password_changed_at),
    }
