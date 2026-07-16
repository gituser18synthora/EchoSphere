/* ============================================================
   Typed service layer — real backend.
   Every function calls the EchoSphere API (FastAPI + MySQL/MongoDB).
   Signatures are the contract the pages rely on; shapes mirror
   src/types/domain.ts. No mock data remains here.
   ============================================================ */

import type {
  AnalyticsBundle, ApiConnection, ApprovedModel, AuditEvent, ChannelConfig,
  Conversation, EntityDef, Guardrail, HealthMetric, Intent, Integration,
  Invoice, KnowledgeGap, KnowledgeSource, PhoneNumber, PlatformAlert, Prompt,
  Release, RoleInfo, SessionUserInfo, SipTrunk, Subscription, TeamMember,
  Tenant, TenantSettings, TestScenario, VoiceBot, VoiceProfile, VoiceSettings,
  Workflow,
} from "@/types/domain";
import { http } from "./http";

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
  name: string; domain: string; industry?: string; region?: string;
  planCode: string; adminEmail: string; adminName?: string; status?: string; seats?: number;
}) => http.post<Tenant & { adminUser?: { email: string; temporaryPassword?: string } }>("/tenants", body);
export const updateTenant = (id: string, body: Partial<Pick<Tenant, "name" | "status" | "health" | "industry" | "region" | "adminEmail">>) =>
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

/* ---------- Prompts / Voice ---------- */
export const listPrompts = (botId: string): Promise<Prompt[]> => http.get(`/bots/${botId}/prompts`);
export const addPromptVersion = (promptId: string, body: { note: string; variants: { language: string; content: string }[] }) =>
  http.post<Prompt>(`/prompts/${promptId}/versions`, body);
export const updatePrompt = (promptId: string, body: { state?: string; activeVersion?: number; name?: string }) =>
  http.patch<Prompt>(`/prompts/${promptId}`, body);
export const listVoices = (): Promise<VoiceProfile[]> => http.get("/voices");
export const listLanguages = (): Promise<{ id: string; code: string; name: string; enabled: boolean }[]> =>
  http.get("/languages");

/* ---------- Intents / Entities / APIs / Workflows ---------- */
export const listIntents = (botId: string): Promise<Intent[]> => http.get(`/bots/${botId}/intents`);
export const updateIntent = (intentId: string, body: Partial<{ samples: string[]; status: string; route: string; confidenceThreshold: number }>) =>
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
export const testApiConnection = (id: string): Promise<{ ok: boolean; latencyMs: number; status: number; body: string }> =>
  http.post(`/api-connections/${id}/test`);

/* ---------- UI-only action stub ----------
   Used solely by flag-gated capabilities that have no backend yet
   (CSV export jobs, recording playback…) — see TODO_BACKEND.md.
   Real operations must never route through this. */
export const simulateAction = (label: string): Promise<{ ok: true; label: string }> =>
  new Promise((resolve) => setTimeout(() => resolve({ ok: true, label }), 350));
