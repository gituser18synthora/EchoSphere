/* ============================================================
   Typed service layer — real backend.
   Every function calls the EchoSphere API (FastAPI + MySQL/MongoDB).
   Signatures are the contract the pages rely on; shapes mirror
   src/types/domain.ts. No mock data remains here.
   ============================================================ */

import type {
  AnalyticsBundle, ApiConnection, ApiTestResult, ApprovedModel, AuditEvent,
  ChannelConfig, Conversation, DocumentStatus, DocumentUploadResult, EntityDef,
  EntityExtraction, Guardrail, HealthMetric, Intent, IntentTestResult,
  Integration, Invoice, KnowledgeGap, KnowledgeSource, OnboardingOptions,
  PhoneNumber, PlatformAlert, Prompt, PromptCompileResult, PromptTestResult,
  Release, RoleInfo, SearchTestResult, SessionUserInfo, SipTrunk,
  StructuredPromptConfig, Subscription, TeamMember, Tenant, TenantProfile,
  TenantSettings, TestScenario, UploadConfig, VoiceBot, VoiceCatalog,
  VoiceProfile, VoiceSessionInfo, VoiceSettings, Workflow,
} from "@/types/domain";
import { http, type Paged } from "./http";

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
  status?: string; seats?: number;
}) => http.post<Tenant & { adminUser?: { email: string; temporaryPassword?: string } }>("/tenants", body);
export const updateTenant = (id: string, body: Partial<Pick<Tenant, "name" | "code" | "status" | "health" | "industry" | "region" | "adminEmail"> & { planCode: string; aiProfileCode: string }>) =>
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
export const saveVoiceSettings = (botId: string, body: Partial<VoiceSettings>) =>
  http.put<VoiceSettings>(`/bots/${botId}/voice-settings`, body);

/* ---------- Knowledge ---------- */
export const listKnowledge = async (botId?: string, tenantId?: string): Promise<KnowledgeSource[]> => {
  const params = new URLSearchParams({ pageSize: "200" });
  if (botId) params.set("botId", botId);
  if (tenantId) params.set("tenantId", tenantId);
  return (await http.getPaged<KnowledgeSource>(`/knowledge?${params}`)).items;
};
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
export const searchTest = (body: { query: string; kbIds?: string[]; botId?: string; topK?: number }): Promise<SearchTestResult> =>
  http.post("/knowledge/search-test", body);

/* ---------- Voice runtime ---------- */
export const createVoiceSession = (botId: string, channel = "browser"): Promise<VoiceSessionInfo> =>
  http.post("/voice-sessions", { botId, channel });
export const getVoiceCatalog = (): Promise<VoiceCatalog> =>
  http.get("/providers/voice-catalog");

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
export const listLanguages = (includeDisabled = false): Promise<{ id: string; code: string; name: string; nativeName?: string | null; direction?: string; enabled: boolean }[]> =>
  http.get(`/languages${includeDisabled ? "?includeDisabled=true" : ""}`);

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
export const saveChannel = (botId: string, type: string, body: { status?: string; detail?: string; workflowName?: string; runTest?: boolean }) =>
  http.put<ChannelConfig>(`/bots/${botId}/channels/${type}`, body);
export const listChannelsSummary = (): Promise<{ type: string; live: number; testing: number; failed: number; configured: number }[]> =>
  http.get("/channels/summary");
export const listScenarios = (botId: string): Promise<TestScenario[]> => http.get(`/bots/${botId}/scenarios`);
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
export const getConversation = (id: string): Promise<Conversation> => http.get(`/conversations/${id}`);
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
  topTenantsByCalls: { label: string; value: number }[];
  aiCostByProvider: { label: string; value: number }[];
}

/* ---------- API test console ---------- */
export const testApiConnection = (id: string, testValues?: Record<string, string>): Promise<ApiTestResult> =>
  http.post(`/api-connections/${id}/test`, testValues ? { testValues } : {});

/* ---------- Master data (Platform Configuration, Super Admin) ---------- */
export type MasterType =
  | "industries" | "data-regions" | "plans" | "ai-profiles"
  | "providers" | "languages" | "voices";

export const listMaster = <T = Record<string, unknown>>(
  mtype: MasterType,
  opts?: { search?: string; sortBy?: string; sortDir?: "asc" | "desc"; page?: number; pageSize?: number; kind?: string; includeInactive?: boolean },
): Promise<Paged<T>> => {
  const params = new URLSearchParams({ pageSize: String(opts?.pageSize ?? 50) });
  if (opts?.page) params.set("page", String(opts.page));
  if (opts?.search) params.set("search", opts.search);
  if (opts?.sortBy) params.set("sortBy", opts.sortBy);
  if (opts?.sortDir) params.set("sortDir", opts.sortDir);
  if (opts?.kind) params.set("kind", opts.kind);
  if (opts?.includeInactive === false) params.set("includeInactive", "false");
  return http.getPaged<T>(`/master/${mtype}?${params}`);
};
export const createMaster = <T = Record<string, unknown>>(mtype: MasterType, body: Record<string, unknown>): Promise<T> =>
  http.post(`/master/${mtype}`, body);
export const updateMaster = <T = Record<string, unknown>>(mtype: MasterType, id: string, body: Record<string, unknown>): Promise<T> =>
  http.patch(`/master/${mtype}/${id}`, body);
export const setMasterStatus = <T = Record<string, unknown>>(mtype: MasterType, id: string, status: "active" | "inactive" | "archived"): Promise<T> =>
  http.post(`/master/${mtype}/${id}/status`, { status });
export const deleteMaster = (mtype: MasterType, id: string): Promise<{ archived: boolean; id: string }> =>
  http.delete(`/master/${mtype}/${id}`);
export const getMasterAudit = (mtype: MasterType, id: string): Promise<{ id: string; actor: string; action: string; previousValue: unknown; newValue: unknown; time: string }[]> =>
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
export const resetUserPassword = (userId: string): Promise<{ reset: boolean; temporaryPassword: string }> =>
  http.post(`/users/${userId}/reset-password`);

/* ---------- Knowledge upload config ---------- */
export const getUploadConfig = (): Promise<UploadConfig> => http.get("/knowledge/upload-config");

/* ---------- Prompts: create / builder / lifecycle / test ---------- */
export const createPrompt = (botId: string, body: {
  type: string; name: string; description?: string; variables?: string[];
  variants?: { language: string; content: string }[];
  structuredConfig?: StructuredPromptConfig; note?: string;
}): Promise<Prompt> => http.post(`/bots/${botId}/prompts`, body);
export const savePromptVersion = (promptId: string, body: {
  note?: string; variants?: { language: string; content: string }[];
  structuredConfig?: StructuredPromptConfig; submitForApproval?: boolean;
}): Promise<Prompt> => http.post(`/prompts/${promptId}/versions`, body);
export const compilePromptPreview = (structuredConfig: StructuredPromptConfig): Promise<PromptCompileResult> =>
  http.post("/prompts/compile-preview", { structuredConfig });
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
   (CSV export jobs, recording playback…) — see TODO_BACKEND.md.
   Real operations must never route through this. */
export const simulateAction = (label: string): Promise<{ ok: true; label: string }> =>
  new Promise((resolve) => setTimeout(() => resolve({ ok: true, label }), 350));
