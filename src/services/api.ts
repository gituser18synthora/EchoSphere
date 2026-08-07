/* ============================================================
   Typed service layer — real backend.
   Every function calls the EchoSphere API (FastAPI + MySQL/MongoDB).
   Signatures are the contract the pages rely on; shapes mirror
   src/types/domain.ts. No mock data remains here.
   ============================================================ */

import type {
  AnalyticsBundle, ApiConnection, ApiTestResult, ApprovedModel, AuditEvent,
  ChannelConfig, ChannelProviderConfig, Conversation, CustomerContext,
  CustomerContextCallState, CustomerContextInput,
  DocumentStatus, DocumentUploadResult, EntityDef,
  EntityExtraction, Guardrail, HealthMetric, Intent, IntentTestResult,
  Integration, Invoice, KnowledgeDetail, KnowledgeGap, KnowledgeSource, OnboardingOptions,
  PhoneNumber, PlatformAlert, Prompt, PromptCompileResult, PromptRenderResult, PromptTestResult,
  Release, RoleInfo, RuntimeContextConfig, RuntimeContextField, RuntimeContextRecord,
  RuntimeContextValidateResult, SearchTestResult, SessionUserInfo, SimulateTrace, SipTrunk,
  StructuredPromptConfig, Subscription, TeamMember, Tenant, TenantProfile,
  TenantSettings, TestScenario, UploadConfig, VoiceBot, VoiceCatalog,
  VoiceProfile, VoiceSessionInfo, VoiceSettings, Workflow,
  ReviewDocument, ReviewDocumentDetail, ReviewChunk, ReviewChunkDetail,
  ReviewFacets, ReviewKnowledgeBase, RetrievalTestResult,
  ModelLanguagesInfo, ProviderInfo, ProviderModelInfo, ProviderSettings,
  ProviderTestResult, TtsPreviewResult, ValidateConfigResult, VoiceCapability,
  VoiceCloneConfig, VoiceOption,
} from "@/types/domain";
import { http, requestWithMeta, type Paged } from "./http";
import { downloadFile } from "./fileDownload";
export {
  downloadReport,
  filenameFromDisposition,
  type ReportExportFilters,
  type ReportExportFormat,
  type ReportType,
} from "./reportDownload";

/* ---------- Auth ---------- */
export const login = (email: string, password: string) =>
  http.post<{ token: string; user: SessionUserInfo }>("/auth/login", { email, password });
export const logout = () => http.post<{ signedOut: boolean }>("/auth/logout");
export const me = () => http.get<SessionUserInfo>("/auth/me");

/* ---------- Tenants (Super Admin) ---------- */
export const listTenants = async (): Promise<Tenant[]> =>
  (await http.getPaged<Tenant>("/tenants?pageSize=200")).items;
export const getTenant = (id: string): Promise<Tenant> => http.get(`/tenants/${id}`);
export const createTenant = (body: {
  name: string; code?: string; domain: string; industry?: string; region?: string;
  aiProfileCode?: string; planCode: string; adminEmail: string; adminName?: string;
  status?: string; seats?: number; defaultLanguages?: string[];
  callSummaryEnabled?: boolean; usePreviousCallSummary?: boolean;
}) => http.post<Tenant & { adminUser?: { email: string; temporaryPassword?: string } }>("/tenants", body);
export const updateTenant = (id: string, body: Partial<Pick<Tenant, "name" | "code" | "status" | "health" | "industry" | "region" | "adminEmail" | "defaultLanguages" | "callSummaryEnabled" | "usePreviousCallSummary"> & { planCode: string; aiProfileCode: string }>) =>
  http.patch<Tenant>(`/tenants/${id}`, body);
export const archiveTenant = (id: string) => http.delete<{ archived: boolean }>(`/tenants/${id}`);

export const listSubscriptions = async (): Promise<Subscription[]> =>
  (await http.getPaged<Subscription>("/subscriptions?pageSize=200")).items;
export const listInvoices = async (): Promise<Invoice[]> =>
  (await http.getPaged<Invoice>("/invoices?pageSize=200")).items;

/* ---------- Bots ---------- */
export const listBots = async (tenantId?: string): Promise<VoiceBot[]> =>
  (await http.getPaged<VoiceBot>(`/bots?pageSize=200${tenantId ? `&tenantId=${tenantId}` : ""}`)).items;
export const getBot = (id: string): Promise<VoiceBot> => http.get(`/bots/${id}`);
export const createBot = (body: { name: string; useCase?: string; description?: string; languages?: string[] }) =>
  http.post<VoiceBot>("/bots", body);
export const updateBot = (
  id: string,
  body: Partial<{ name: string; useCase: string; description: string; status: string; languages: string[]; voiceId: string | null; readiness: Record<string, boolean> }>,
) => http.patch<VoiceBot>(`/bots/${id}`, body);
export const archiveBot = (id: string) => http.delete<{ archived: boolean }>(`/bots/${id}`);

export const getVoiceSettings = (botId: string): Promise<VoiceSettings> =>
  http.get(`/bots/${botId}/voice-settings`);
/** PUT voice settings; the catalog may accept the config with warnings (meta.warnings). */
export const saveVoiceSettings = async (
  botId: string, body: Partial<VoiceSettings>,
): Promise<{ settings: VoiceSettings; warnings: string[] }> => {
  const { data, meta } = await requestWithMeta<VoiceSettings>("PUT", `/bots/${botId}/voice-settings`, body);
  return { settings: data, warnings: meta?.warnings ?? [] };
};

/* ---------- Knowledge ---------- */
export const listKnowledge = async (botId?: string, tenantId?: string): Promise<KnowledgeSource[]> => {
  const params = new URLSearchParams({ pageSize: "200" });
  if (botId) params.set("botId", botId);
  if (tenantId) params.set("tenantId", tenantId);
  return (await http.getPaged<KnowledgeSource>(`/knowledge?${params}`)).items;
};
/** Server-filtered, paginated knowledge list (admin views). */
export const listKnowledgePaged = (opts?: {
  tenantId?: string; search?: string; status?: string; type?: string;
  scope?: string; page?: number; pageSize?: number;
}): Promise<Paged<KnowledgeSource>> => {
  const params = new URLSearchParams({ pageSize: String(opts?.pageSize ?? 25) });
  if (opts?.page) params.set("page", String(opts.page));
  if (opts?.tenantId) params.set("tenantId", opts.tenantId);
  if (opts?.search) params.set("search", opts.search);
  if (opts?.status) params.set("status", opts.status);
  if (opts?.type) params.set("type", opts.type);
  if (opts?.scope) params.set("scope", opts.scope);
  return http.getPaged<KnowledgeSource>(`/knowledge?${params}`);
};
export const getKnowledgeDetail = (id: string): Promise<KnowledgeDetail> =>
  http.get(`/knowledge/${id}`);
export const createKnowledge = (body: { name: string; type: string; detail?: string; scope?: string; botId?: string; sizeKb?: number }) =>
  http.post<KnowledgeSource>("/knowledge", body);
export const resyncKnowledge = (id: string) =>
  http.patch<KnowledgeSource>(`/knowledge/${id}`, { resync: true });
export const archiveKnowledge = (id: string) => http.delete<{ archived: boolean }>(`/knowledge/${id}`);
export const listKnowledgeGaps = (botId?: string): Promise<KnowledgeGap[]> =>
  http.get(`/knowledge-gaps${botId ? `?botId=${botId}` : ""}`);

/* ---------- Knowledge documents (ingestion pipeline) ---------- */
export const uploadKnowledgeDocument = (sourceId: string, file: File): Promise<DocumentUploadResult> => {
  const form = new FormData();
  form.append("file", file);
  return http.postForm(`/knowledge/${sourceId}/documents`, form);
};
export const listKnowledgeDocuments = (sourceId: string): Promise<DocumentStatus[]> =>
  http.get(`/knowledge/${sourceId}/documents`);
export const getDocumentStatus = (documentId: string): Promise<DocumentStatus> =>
  http.get(`/knowledge/documents/${documentId}/status`);
export const retryDocument = (documentId: string): Promise<DocumentStatus> =>
  http.post(`/knowledge/documents/${documentId}/retry`);
export const cancelDocument = (documentId: string): Promise<DocumentStatus> =>
  http.post(`/knowledge/documents/${documentId}/cancel`);
export const reindexDocument = (documentId: string): Promise<DocumentStatus> =>
  http.post(`/knowledge/documents/${documentId}/reindex`);
export const deleteDocument = (documentId: string): Promise<{ archived: boolean; id: string }> =>
  http.delete(`/knowledge/documents/${documentId}`);
export const searchTest = (body: {
  query: string; kbIds?: string[]; botId?: string; topK?: number; minScore?: number;
}): Promise<SearchTestResult> =>
  http.post("/knowledge/search-test", body);

/* ---------- Knowledge Chunk Review (Super Admin) ---------- */
const REVIEW = "/admin/knowledge/review";

/** Append only defined, non-empty filter values to a query string. */
function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export interface DocumentListParams {
  page?: number; pageSize?: number; search?: string; sortBy?: string; sortDir?: "asc" | "desc";
  tenantId?: string; kbId?: string; fileType?: string; status?: string; ingestionStatus?: string;
  language?: string; uploadedFrom?: string; uploadedTo?: string; failedOnly?: boolean; includeArchived?: boolean;
}
export interface ChunkListParams {
  page?: number; pageSize?: number; search?: string; sortBy?: string; sortDir?: "asc" | "desc";
  tenantId?: string; documentId?: string; kbId?: string; status?: string; language?: string;
  pageNumber?: number; section?: string; createdFrom?: string; createdTo?: string;
  minTokens?: number; maxTokens?: number; hasKeywords?: boolean; hasMetadata?: boolean; flaggedOnly?: boolean;
}

export const reviewFacets = (): Promise<ReviewFacets> => http.get(`${REVIEW}/facets`);
export const reviewKnowledgeBases = (): Promise<ReviewKnowledgeBase[]> => http.get(`${REVIEW}/knowledge-bases`);

export const reviewDocuments = (p: DocumentListParams): Promise<Paged<ReviewDocument>> =>
  http.getPaged<ReviewDocument>(`${REVIEW}/documents${qs({ ...p })}`);
export const getReviewDocument = (id: string): Promise<ReviewDocumentDetail> =>
  http.get(`${REVIEW}/documents/${id}`);
export const retryReviewDocument = (id: string): Promise<DocumentStatus> =>
  http.post(`${REVIEW}/documents/${id}/retry`);
export const reindexReviewDocument = (id: string): Promise<DocumentStatus> =>
  http.post(`${REVIEW}/documents/${id}/reindex`);
export const archiveReviewDocument = (id: string): Promise<{ archived: boolean; id: string }> =>
  http.post(`${REVIEW}/documents/${id}/archive`);

/** Authorized original-file download — streams the blob and saves it client-side. */
export const downloadReviewDocument = async (id: string, fileName: string): Promise<void> => {
  await downloadFile({
    url: `/api/v1${REVIEW}/documents/${encodeURIComponent(id)}/download`,
    fallbackFilename: fileName,
  });
};

export const reviewChunks = (p: ChunkListParams): Promise<Paged<ReviewChunk>> =>
  http.getPaged<ReviewChunk>(`${REVIEW}/chunks${qs({ ...p })}`);
export const getReviewChunk = (id: string): Promise<ReviewChunkDetail> =>
  http.get(`${REVIEW}/chunks/${id}`);
export const setChunkStatus = (id: string, status: "active" | "archived"): Promise<{ chunkId: string; status: string }> =>
  http.patch(`${REVIEW}/chunks/${id}/status`, { status });
export const flagChunk = (id: string, flagged: boolean, reason?: string): Promise<{ chunkId: string; flagged: boolean }> =>
  http.post(`${REVIEW}/chunks/${id}/flag`, { flagged, reason });
export const reviewRetrievalTest = (body: {
  query: string; kbIds?: string[]; documentId?: string; topK?: number; minScore?: number;
}): Promise<RetrievalTestResult> => http.post(`${REVIEW}/retrieval-test`, body);

/* ---------- Voice runtime ---------- */
export const createVoiceSession = (
  botId: string,
  channel = "browser",
  opts?: { variables?: Record<string, string>; customerContextId?: string },
): Promise<VoiceSessionInfo> =>
  http.post("/voice-sessions", {
    botId,
    channel,
    ...(opts?.variables ? { variables: opts.variables } : {}),
    ...(opts?.customerContextId ? { customerContextId: opts.customerContextId } : {}),
  });
export const getVoiceCatalog = (): Promise<VoiceCatalog> =>
  http.get("/providers/voice-catalog");

/* ---------- Customer collection context ---------- */
export const listCustomerContexts = async (botId: string): Promise<CustomerContext[]> =>
  (await http.getPaged<CustomerContext>(`/bots/${botId}/customer-contexts`)).items;
export const lookupCustomerContext = (botId: string, phone: string): Promise<CustomerContext> =>
  http.get(`/bots/${botId}/customer-contexts/lookup?phone=${encodeURIComponent(phone)}`);
export const getCustomerContext = (id: string): Promise<CustomerContext> =>
  http.get(`/customer-contexts/${id}`);
export const createCustomerContext = (botId: string, body: CustomerContextInput): Promise<CustomerContext> =>
  http.post(`/bots/${botId}/customer-contexts`, body);
export const updateCustomerContext = (id: string, body: CustomerContextInput): Promise<CustomerContext> =>
  http.patch(`/customer-contexts/${id}`, body);
export const updateCustomerContextCallState = (
  id: string, body: CustomerContextCallState,
): Promise<CustomerContext> => http.patch(`/customer-contexts/${id}/call-state`, body);
export const deleteCustomerContext = (id: string): Promise<{ deleted: boolean }> =>
  http.delete(`/customer-contexts/${id}`);

/* ---------- Runtime context / user details (per bot) ---------- */
export const getRuntimeContext = (botId: string): Promise<RuntimeContextConfig> =>
  http.get(`/bots/${botId}/runtime-context`);
export const saveRuntimeContext = (botId: string, body: {
  name?: string; sourceMode: "api" | "manual"; apiConnectionId?: string | null;
  responsePath?: string | null; fields: RuntimeContextField[]; allowAdditional: boolean;
  testPayload?: Record<string, unknown> | null; missingValuePolicy?: string | null;
  domainPolicy: "generic" | "collections";
}): Promise<RuntimeContextConfig> => http.put(`/bots/${botId}/runtime-context`, body);
/** Validate a payload against the schema (or unsaved `fields`) and return the
    effective, source-tagged, masked context a live call would see. */
export const validateRuntimeContext = (botId: string, body: {
  payload: Record<string, unknown>; fields?: RuntimeContextField[]; allowAdditional?: boolean;
}): Promise<RuntimeContextValidateResult> =>
  http.post(`/bots/${botId}/runtime-context/validate`, body);
export const listContextRecords = (botId: string, params?: {
  page?: number; pageSize?: number; search?: string;
}): Promise<Paged<RuntimeContextRecord>> => {
  const sp = new URLSearchParams({ pageSize: String(params?.pageSize ?? 25) });
  if (params?.page) sp.set("page", String(params.page));
  if (params?.search) sp.set("search", params.search);
  return http.getPaged<RuntimeContextRecord>(`/bots/${botId}/runtime-context/records?${sp}`);
};
export const createContextRecord = (botId: string, body: {
  customerRef?: string; phone?: string; data: Record<string, unknown>;
}): Promise<RuntimeContextRecord> => http.post(`/bots/${botId}/runtime-context/records`, body);
export const updateContextRecord = (recordId: string, body: {
  customerRef?: string; phone?: string; data: Record<string, unknown>;
}): Promise<RuntimeContextRecord> => http.patch(`/runtime-context-records/${recordId}`, body);
export const deleteContextRecord = (recordId: string): Promise<{ deleted: boolean }> =>
  http.delete(`/runtime-context-records/${recordId}`);

/* ---------- Provider catalog (database-driven voice configuration) ---------- */
export const getProviderCatalog = (
  capability?: VoiceCapability,
): Promise<Partial<Record<VoiceCapability, ProviderInfo[]>>> =>
  http.get(`/providers/catalog${capability ? `?capability=${capability}` : ""}`);
export const listProviderModels = (capability: VoiceCapability, code: string): Promise<ProviderModelInfo[]> =>
  http.get(`/providers/${capability}/${encodeURIComponent(code)}/models`);
export const getModelLanguages = (
  capability: VoiceCapability, code: string, model: string,
): Promise<ModelLanguagesInfo> =>
  http.get(`/providers/${capability}/${encodeURIComponent(code)}/models/${encodeURIComponent(model)}/languages`);
export const listProviderVoices = (
  code: string, filters?: { model?: string; language?: string; gender?: string },
): Promise<VoiceOption[]> => {
  const params = new URLSearchParams();
  if (filters?.model) params.set("model", filters.model);
  if (filters?.language) params.set("language", filters.language);
  if (filters?.gender) params.set("gender", filters.gender);
  const query = params.toString();
  return http.get(`/providers/tts/${encodeURIComponent(code)}/voices${query ? `?${query}` : ""}`);
};
export const validateVoiceConfig = (botId: string, config: Record<string, unknown>): Promise<ValidateConfigResult> =>
  http.post("/providers/validate-config", { botId, config });
export const testProviderConnection = (body: {
  capability: VoiceCapability; provider: string; model?: string; voice?: string; language?: string;
}): Promise<ProviderTestResult> => http.post("/providers/test", body);
export const generateTtsPreview = (body: {
  provider: string; model: string; voice: string; language: string; text: string; params?: ProviderSettings;
  /* Delivery tuning — applied server-side with the same mapping as live calls. */
  speed?: number; pauseMs?: number; energy?: number;
}): Promise<TtsPreviewResult> => http.post("/providers/tts-preview", body);

/* ---------- Tenant voice cloning ---------- */
export const getVoiceCloneConfig = (): Promise<VoiceCloneConfig> =>
  http.get("/voice-clones/config");
export const listVoiceClones = (): Promise<VoiceProfile[]> => http.get("/voice-clones");
/** Multipart: provider, name, files[] plus provider-specific clone options. */
export const createVoiceClone = (form: FormData): Promise<VoiceProfile> =>
  http.postForm("/voice-clones", form);
export const updateVoiceClone = (
  id: string,
  body: { name?: string; description?: string; gender?: string; locale?: string; sampleText?: string },
): Promise<VoiceProfile> => http.patch(`/voice-clones/${id}`, body);
export const setVoiceCloneStatus = (id: string, status: "active" | "inactive" | "archived"): Promise<VoiceProfile> =>
  http.post(`/voice-clones/${id}/status`, { status });
export const deleteVoiceClone = (id: string): Promise<{ deleted: boolean; providerDeleted: boolean }> =>
  http.delete(`/voice-clones/${id}`);

/* ---------- Prompts / Voice ---------- */
export const listPrompts = (botId: string): Promise<Prompt[]> => http.get(`/bots/${botId}/prompts`);
export const addPromptVersion = (promptId: string, body: { note: string; variants: { language: string; content: string }[] }) =>
  http.post<Prompt>(`/prompts/${promptId}/versions`, body);
export const updatePrompt = (promptId: string, body: { state?: string; activeVersion?: number; name?: string }) =>
  http.patch<Prompt>(`/prompts/${promptId}`, body);
export const listVoices = (filters?: { provider?: string; language?: string; locale?: string; gender?: string; search?: string; includeInactive?: boolean }): Promise<VoiceProfile[]> => {
  const params = new URLSearchParams();
  if (filters?.provider) params.set("provider", filters.provider);
  if (filters?.language) params.set("language", filters.language);
  if (filters?.locale) params.set("locale", filters.locale);
  if (filters?.gender) params.set("gender", filters.gender);
  if (filters?.search) params.set("search", filters.search);
  if (filters?.includeInactive) params.set("includeInactive", "true");
  const qs = params.toString();
  return http.get(`/voices${qs ? `?${qs}` : ""}`);
};
export const listLanguages = (
  includeDisabled = false,
  tenantId?: string,
): Promise<{ id: string; code: string; name: string; nativeName?: string | null; direction?: string; isDefault?: boolean; enabled: boolean }[]> => {
  const params = new URLSearchParams();
  if (includeDisabled) params.set("includeDisabled", "true");
  if (tenantId) params.set("tenantId", tenantId);
  const query = params.toString();
  return http.get(`/languages${query ? `?${query}` : ""}`);
};

/* ---------- Intents / Entities / APIs / Workflows ---------- */
export const listIntents = (botId: string): Promise<Intent[]> => http.get(`/bots/${botId}/intents`);
export const updateIntent = (intentId: string, body: Partial<Intent>) =>
  http.patch<Intent>(`/intents/${intentId}`, body);
export const listEntities = (tenantId?: string): Promise<EntityDef[]> =>
  http.get(`/entities${tenantId ? `?tenantId=${tenantId}` : ""}`);
export const listApis = (botId?: string): Promise<ApiConnection[]> =>
  http.get(`/api-connections${botId ? `?botId=${botId}` : ""}`);
export const getWorkflow = (botId: string): Promise<Workflow> => http.get(`/bots/${botId}/workflow`);
export const listWorkflows = (): Promise<Workflow[]> => http.get("/workflows");
export const saveWorkflow = (botId: string, body: Partial<Pick<Workflow, "name" | "nodes" | "edges" | "issues" | "status">>) =>
  http.put<Workflow>(`/bots/${botId}/workflow`, body);

/* ---------- Channels / Testing / Releases ---------- */
export const listChannels = (botId: string): Promise<ChannelConfig[]> => http.get(`/bots/${botId}/channels`);
export const getChannel = (botId: string, type: string): Promise<ChannelConfig> =>
  http.get(`/bots/${botId}/channels/${type}`);
/** Create or update a channel's provider configuration. Status is server-derived. */
export const saveChannel = (botId: string, type: string, body: { config: ChannelProviderConfig; workflowName?: string }) =>
  http.put<ChannelConfig>(`/bots/${botId}/channels/${type}`, body);
/** Real connection test — returns the channel with lastTest.checks populated. */
export const testChannel = (botId: string, type: string): Promise<ChannelConfig> =>
  http.post(`/bots/${botId}/channels/${type}/test`);
export const activateChannel = (botId: string, type: string): Promise<ChannelConfig> =>
  http.post(`/bots/${botId}/channels/${type}/activate`);
export const deactivateChannel = (botId: string, type: string): Promise<ChannelConfig> =>
  http.post(`/bots/${botId}/channels/${type}/deactivate`);
export const archiveChannel = (botId: string, type: string): Promise<{ archived: boolean }> =>
  http.delete(`/bots/${botId}/channels/${type}`);
export const listChannelsSummary = (): Promise<{ type: string; live: number; testing: number; failed: number; configured: number }[]> =>
  http.get("/channels/summary");
export const listScenarios = (botId: string): Promise<TestScenario[]> => http.get(`/bots/${botId}/scenarios`);

/** One text turn through the REAL runtime stack (TurnRouter + WorkflowEngine). */
export interface ChatTestResult {
  sessionId: string;
  route: string;
  action: string | null;
  matchedIntent: string | null;
  confidence: number;
  reason: string;
  reply: string;
  done: boolean;
  language: string;
  latencyMs: number;
  at: string;
  activeWorkflow: string | null;
  workflow: {
    name: string;
    source: "definition" | "builtin" | "missing";
    status: string;
    workflowId: string | null;
    nodeTrace: string[];
    slots: Record<string, unknown>;
    done: boolean;
  } | null;
}
export const testBotChat = (
  botId: string,
  message: string,
  sessionId?: string,
  messages: { role: "user" | "assistant"; content: string }[] = [],
  language?: string,
): Promise<ChatTestResult> =>
  http.post(`/bots/${botId}/testing/chat`, {
    message,
    messages,
    ...(sessionId ? { sessionId } : {}),
    ...(language ? { language } : {}),
  });
/** One complete runtime turn (context → prompt → routing → policy → tools →
    workflow/LLM) with the full trace — the Testing Studio simulator. */
export const simulateTurn = (botId: string, body: {
  message: string; messages?: { role: "user" | "assistant"; content: string }[];
  promptId?: string; promptVersion?: number;
  contextSource?: "saved" | "manual" | "api_mock" | "none";
  contextPayload?: Record<string, unknown>; language?: string;
  isFinal?: boolean; interrupted?: boolean;
  mockToolResults?: Record<string, unknown>; sessionId?: string;
}): Promise<SimulateTrace> => http.post(`/bots/${botId}/testing/simulate`, body);
export const runSuite = (botId: string): Promise<{ passed: number; failed: number; total: number; at: string }> =>
  http.post(`/bots/${botId}/scenarios/run`);
export const listReleases = (botId: string): Promise<Release[]> => http.get(`/bots/${botId}/releases`);
export const createRelease = (botId: string, body: { version: string; notes?: string; diff?: { area: string; change: string; kind: string }[] }) =>
  http.post<Release>(`/bots/${botId}/releases`, body);
export const updateReleaseStage = (releaseId: string, stage: string) =>
  http.patch<Release>(`/releases/${releaseId}`, { stage });

/* ---------- Conversations ---------- */
export const listConversations = async (filters?: { botId?: string; channel?: string; sentiment?: string; flagged?: boolean; search?: string }): Promise<Conversation[]> => {
  const params = new URLSearchParams({ pageSize: "100" });
  if (filters?.botId) params.set("botId", filters.botId);
  if (filters?.channel) params.set("channel", filters.channel);
  if (filters?.sentiment) params.set("sentiment", filters.sentiment);
  if (filters?.flagged !== undefined) params.set("flagged", String(filters.flagged));
  if (filters?.search) params.set("search", filters.search);
  return (await http.getPaged<Conversation>(`/conversations?${params}`)).items;
};
/** `currency` selects the display currency for the cost breakdown only; the
    authoritative `costUsd` total is always returned in the base currency. */
export const getConversation = (id: string, currency?: string): Promise<Conversation> =>
  http.get(`/conversations/${id}${currency ? `?currency=${encodeURIComponent(currency)}` : ""}`);
export const flagConversation = (id: string, flagged: boolean) =>
  http.patch<Conversation>(`/conversations/${id}`, { flagged });

/* ---------- Platform ---------- */
export const listAlerts = (): Promise<PlatformAlert[]> => http.get("/alerts");
export const updateAlert = (id: string, status: "open" | "acknowledged" | "resolved") =>
  http.patch<PlatformAlert>(`/alerts/${id}`, { status });
export const listAudit = async (): Promise<AuditEvent[]> =>
  (await http.getPaged<AuditEvent>("/audit?pageSize=100")).items;
export const listModels = (): Promise<ApprovedModel[]> => http.get("/models");
export const updateModelStatus = (id: string, status: string) => http.patch<ApprovedModel>(`/models/${id}`, { status });
export const listGuardrails = (): Promise<Guardrail[]> => http.get("/guardrails");
export const updateGuardrail = (id: string, body: { enabled?: boolean; enforcement?: string }) =>
  http.patch<Guardrail>(`/guardrails/${id}`, body);
export const listPhoneNumbers = (): Promise<PhoneNumber[]> => http.get("/phone-numbers");
export const updatePhoneNumber = (
  id: string,
  body: Partial<{ number: string; country: string; provider: string; monthlyCost: number; status: string }>,
): Promise<PhoneNumber> => http.patch(`/phone-numbers/${id}`, body);
export const setPhoneNumberActive = (id: string, active: boolean): Promise<PhoneNumber> =>
  http.post(`/phone-numbers/${id}/${active ? "activate" : "deactivate"}`, {});
export const listSipTrunks = (): Promise<SipTrunk[]> => http.get("/sip-trunks");
export const getPlatformHealth = (): Promise<HealthMetric[]> => http.get("/health-metrics");
export const listTemplates = (kind: string): Promise<Record<string, unknown>[]> =>
  http.get(`/templates?kind=${encodeURIComponent(kind)}`);

/* ---------- Team / Users / Roles ---------- */
export const listTeam = async (tenantId?: string): Promise<TeamMember[]> =>
  (await http.getPaged<TeamMember>(`/users?scope=tenant&pageSize=200${tenantId ? `&tenantId=${tenantId}` : ""}`)).items;
export const listPlatformUsers = async (): Promise<TeamMember[]> =>
  (await http.getPaged<TeamMember>("/users?scope=platform&pageSize=200")).items;
export const inviteUser = (body: { name: string; email: string; roleCode: string }) =>
  http.post<TeamMember & { temporaryPassword?: string }>("/users", body);
export const updateUser = (id: string, body: { name?: string; roleCode?: string; status?: string }) =>
  http.patch<TeamMember>(`/users/${id}`, body);
export const archiveUser = (id: string) => http.delete<{ archived: boolean }>(`/users/${id}`);
export const listRoles = (): Promise<RoleInfo[]> => http.get("/roles");

/* ---------- Integrations ---------- */
export const listIntegrations = (): Promise<Integration[]> => http.get("/integrations");
export const connectIntegration = (id: string) => http.post<Integration>(`/integrations/${id}/connect`, {});
export const disconnectIntegration = (id: string) => http.post<Integration>(`/integrations/${id}/disconnect`);

/* ---------- Settings ---------- */
export const getTenantSettings = (tenantId?: string): Promise<TenantSettings> =>
  http.get(`/tenant/settings${tenantId ? `?tenantId=${tenantId}` : ""}`);
export const saveTenantSettings = (body: Partial<TenantSettings>, tenantId?: string) =>
  http.put<TenantSettings>(`/tenant/settings${tenantId ? `?tenantId=${tenantId}` : ""}`, body);

/* ---------- Analytics ---------- */
export const getTenantAnalytics = (days = 30, botId?: string, tenantId?: string): Promise<AnalyticsBundle> =>
  http.get(`/analytics/tenant?days=${days}${botId ? `&botId=${botId}` : ""}${tenantId ? `&tenantId=${tenantId}` : ""}`);
export const getPlatformAnalytics = (days = 30) => http.get<PlatformAnalytics>(`/analytics/platform?days=${days}`);
export const getAdminDashboard = () =>
  http.get<{ kpis: AnalyticsBundle["kpis"]; activeTenants: number; liveBots: number }>("/dashboard/admin");

export interface PlatformAnalytics {
  labels: string[];
  callVol: number[];
  revenue: number[];
  aiCost: number[];
  callsSeries: { t: string; calls: number }[];
  revVsCost: { t: string; revenue: number; aiCost: number }[];
  planMix: { label: string; value: number }[];
  mrrByPlan: { label: string; value: number }[];
  topTenantsByCalls: { label: string; value: number }[];
  aiCostByProvider: { label: string; value: number }[];
}

/* ---------- Usage & currency (backend-authoritative costing) ---------- */

export interface UsageQuantities {
  requests: number;
  inputTokens: number;
  outputTokens: number;
  cachedTokens: number;
  totalTokens: number;
  characters: number;
  audioSeconds: number;
  costUsd: number;
  missingPriceEvents: number;
}

export interface UsageProviderRow extends UsageQuantities {
  capability: string;
  provider: string;
  model: string;
}

export interface UsageSummary {
  tenantId: string;
  period: { start: string; end: string; days: number };
  baseCurrency: string;
  totalCostUsd: number;
  /** Backend-computed conversions with the rates currently in force. */
  totalCostConverted: Record<string, number>;
  missingPriceEvents: number;
  capabilities: Record<string, UsageQuantities>;
  byProviderModel: UsageProviderRow[];
}

export interface PlatformUsage {
  period: { start: string; end: string; days: number };
  baseCurrency: string;
  totalCostUsd: number;
  totalCostConverted: Record<string, number>;
  missingPriceEvents: number;
  byTenant: ({ tenantId: string; tenant: string } & UsageQuantities)[];
  byCapability: Record<string, UsageQuantities>;
  byProviderModel: UsageProviderRow[];
}

export const getUsageSummary = (days = 30, tenantId?: string, botId?: string): Promise<UsageSummary> =>
  http.get(`/usage/summary?days=${days}${tenantId ? `&tenantId=${tenantId}` : ""}${botId ? `&botId=${botId}` : ""}`);
export const getPlatformUsage = (days = 30): Promise<PlatformUsage> =>
  http.get(`/usage/platform?days=${days}`);
export const getCurrencyRates = (): Promise<import("./money").CurrencyRates> =>
  http.get("/currency/rates");

/* ---------- API test console ---------- */
export const testApiConnection = (id: string, testValues?: Record<string, string>): Promise<ApiTestResult> =>
  http.post(`/api-connections/${id}/test`, testValues ? { testValues } : {});

/* ---------- Master data (Platform Configuration, Super Admin) ---------- */
export type MasterType =
  | "industries" | "countries" | "data-regions" | "plans" | "ai-profiles"
  | "providers" | "provider-models" | "languages" | "voices"
  | "currencies" | "exchange-rates" | "provider-pricing";

export const listMaster = <T = Record<string, unknown>>(
  mtype: MasterType,
  opts?: {
    search?: string; sortBy?: string; sortDir?: "asc" | "desc"; page?: number; pageSize?: number;
    kind?: string; includeInactive?: boolean;
    /** Voices + provider-models server-side filters. */
    provider?: string; gender?: string; status?: string; language?: string;
    /** Provider-models-only filter (stt | tts | llm | embedding). */
    capability?: string;
  },
): Promise<Paged<T>> => {
  const params = new URLSearchParams({ pageSize: String(opts?.pageSize ?? 50) });
  if (opts?.page) params.set("page", String(opts.page));
  if (opts?.search) params.set("search", opts.search);
  if (opts?.sortBy) params.set("sortBy", opts.sortBy);
  if (opts?.sortDir) params.set("sortDir", opts.sortDir);
  if (opts?.kind) params.set("kind", opts.kind);
  if (opts?.provider) params.set("provider", opts.provider);
  if (opts?.gender) params.set("gender", opts.gender);
  if (opts?.status) params.set("status", opts.status);
  if (opts?.language) params.set("language", opts.language);
  if (opts?.capability) params.set("capability", opts.capability);
  if (opts?.includeInactive === false) params.set("includeInactive", "false");
  return http.getPaged<T>(`/master/${mtype}?${params}`);
};
export const createMaster = <T = Record<string, unknown>>(mtype: MasterType, body: Record<string, unknown>): Promise<T> =>
  http.post(`/master/${mtype}`, body);
export const updateMaster = <T = Record<string, unknown>>(mtype: MasterType, id: string | number, body: Record<string, unknown>): Promise<T> =>
  http.patch(`/master/${mtype}/${id}`, body);
export const setMasterStatus = <T = Record<string, unknown>>(mtype: MasterType, id: string | number, status: "active" | "inactive" | "archived"): Promise<T> =>
  http.post(`/master/${mtype}/${id}/status`, { status });
export const deleteMaster = (mtype: MasterType, id: string | number): Promise<{ archived: boolean; id: string | number }> =>
  http.delete(`/master/${mtype}/${id}`);
export const getMasterAudit = (mtype: MasterType, id: string | number): Promise<{ id: string; actor: string; action: string; previousValue: unknown; newValue: unknown; time: string }[]> =>
  http.get(`/master/${mtype}/${id}/audit`);
export const duplicatePlan = (id: string) => http.post<Record<string, unknown>>(`/master/plans/${id}/duplicate`);
export const listPlanTenants = (id: string): Promise<{ id: string; name: string; domain: string; subscriptionStatus: string; mrr: number }[]> =>
  http.get(`/master/plans/${id}/tenants`);
export const getOnboardingOptions = (): Promise<OnboardingOptions> => http.get("/onboarding/options");

/* ---------- Tenant profile ---------- */
export const getTenantProfile = (tenantId?: string): Promise<TenantProfile> =>
  http.get(`/tenant/profile${tenantId ? `?tenantId=${tenantId}` : ""}`);
export const saveTenantProfile = (body: Partial<Pick<TenantProfile,
  "displayName" | "website" | "contactName" | "contactEmail" | "contactPhone" |
  "address" | "country" | "timezone" | "defaultLanguages" | "branding" |
  "supportEmail" | "supportPhone" | "workingHours">>, tenantId?: string): Promise<TenantProfile> =>
  http.put(`/tenant/profile${tenantId ? `?tenantId=${tenantId}` : ""}`, body);

/* ---------- Own profile & password ---------- */
export const updateMyProfile = (body: Partial<{ firstName: string; lastName: string; phone: string; avatarUrl: string; locale: string; timezone: string }>): Promise<SessionUserInfo> =>
  http.patch("/users/me", body);
export const changeMyPassword = (body: { currentPassword: string; newPassword: string; confirmPassword: string }): Promise<{ changed: boolean; token: string; message: string }> =>
  http.post("/users/me/password", body);
/** Admin password reset. Without a body the API issues a one-time temporary
    password; with newPassword/confirmPassword the admin sets it directly.
    Either way the target's existing sessions are invalidated. */
export const resetUserPassword = (
  userId: string,
  body?: { newPassword: string; confirmPassword: string },
): Promise<{ reset: boolean; sessionsInvalidated: boolean; temporaryPassword?: string }> =>
  http.post(`/users/${userId}/reset-password`, body);

/* ---------- Knowledge upload config ---------- */
export const getUploadConfig = (): Promise<UploadConfig> => http.get("/knowledge/upload-config");

/* ---------- Prompts: create / builder / lifecycle / test ---------- */
export const createPrompt = (botId: string, body: {
  type: string; promptMode?: "structured" | "full"; name: string; description?: string;
  variables?: string[]; variants?: { language: string; content: string }[];
  structuredConfig?: StructuredPromptConfig; fullPrompt?: string; note?: string;
}): Promise<Prompt> => http.post(`/bots/${botId}/prompts`, body);
export const savePromptVersion = (promptId: string, body: {
  note?: string; promptMode?: "structured" | "full";
  variants?: { language: string; content: string }[];
  structuredConfig?: StructuredPromptConfig; fullPrompt?: string; submitForApproval?: boolean;
}): Promise<Prompt> => http.post(`/prompts/${promptId}/versions`, body);
/** Stateless compile of either authoring mode; with testContext the response
    also carries the rendered prompt + missing-variable report. */
export const compilePromptPreview = (body: {
  promptMode: "structured" | "full"; structuredConfig?: StructuredPromptConfig;
  fullPrompt?: string; testContext?: Record<string, unknown>;
}): Promise<PromptCompileResult> => http.post("/prompts/compile-preview", body);
export const renderPromptPreview = (promptId: string, body: {
  version?: number; testContext: Record<string, unknown>;
}): Promise<PromptRenderResult> => http.post(`/prompts/${promptId}/render-preview`, body);
export const duplicatePrompt = (promptId: string): Promise<Prompt> =>
  http.post(`/prompts/${promptId}/duplicate`);
export const deletePrompt = (promptId: string): Promise<{ archived: boolean }> =>
  http.delete(`/prompts/${promptId}`);
export const testPrompt = (promptId: string, body: { message: string; language?: string; version?: number; useKnowledge?: boolean }): Promise<PromptTestResult> =>
  http.post(`/prompts/${promptId}/test`, body);

/* ---------- Intents: full CRUD + test ---------- */
export const createIntent = (botId: string, body: Partial<Intent> & { name: string }): Promise<Intent> =>
  http.post(`/bots/${botId}/intents`, body);
export const duplicateIntent = (intentId: string): Promise<Intent> =>
  http.post(`/intents/${intentId}/duplicate`);
export const deleteIntent = (intentId: string): Promise<{ archived: boolean }> =>
  http.delete(`/intents/${intentId}`);
export const testIntents = (botId: string, utterance: string, language = "en-US"): Promise<IntentTestResult> =>
  http.post(`/bots/${botId}/intents/test`, { utterance, language });

/* ---------- Entities: full CRUD + test ---------- */
export const createEntity = (body: Partial<EntityDef> & { name: string }): Promise<EntityDef> =>
  http.post("/entities", body);
export const updateEntity = (entityId: string, body: Partial<EntityDef>): Promise<EntityDef> =>
  http.patch(`/entities/${entityId}`, body);
export const duplicateEntity = (entityId: string): Promise<EntityDef> =>
  http.post(`/entities/${entityId}/duplicate`);
export const deleteEntity = (entityId: string): Promise<{ archived: boolean }> =>
  http.delete(`/entities/${entityId}`);
export const testEntity = (entityId: string, text: string): Promise<EntityExtraction> =>
  http.post(`/entities/${entityId}/test`, { text });

/* ---------- API connections: full CRUD ---------- */
export const createApi = (body: Partial<ApiConnection> & { name: string; url: string }): Promise<ApiConnection> =>
  http.post("/api-connections", body);
export const updateApi = (id: string, body: Partial<ApiConnection>): Promise<ApiConnection> =>
  http.patch(`/api-connections/${id}`, body);
export const duplicateApi = (id: string): Promise<ApiConnection> =>
  http.post(`/api-connections/${id}/duplicate`);
export const deleteApi = (id: string): Promise<{ archived: boolean }> =>
  http.delete(`/api-connections/${id}`);

/* ---------- UI-only action stub ----------
   Used solely by flag-gated capabilities that have no backend yet
   (recording playback and similar deferred actions) — see TODO_BACKEND.md.
   Real operations must never route through this. */
export const simulateAction = (label: string): Promise<{ ok: true; label: string }> =>
  new Promise((resolve) => setTimeout(() => resolve({ ok: true, label }), 350));
