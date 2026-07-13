/* ============================================================
   Typed service layer.
   Every function returns a Promise of a domain type, simulating
   network latency. Swapping to the real backend means replacing
   the bodies with fetch calls — the signatures are the contract.
   Endpoints that don't exist yet are listed in TODO_BACKEND.md
   and gated behind feature flags (src/services/flags.ts).
   ============================================================ */

import type {
  AnalyticsBundle, ApiConnection, ApprovedModel, AuditEvent, ChannelConfig,
  Conversation, EntityDef, Guardrail, HealthMetric, Intent, Integration,
  Invoice, KnowledgeGap, KnowledgeSource, PhoneNumber, PlatformAlert, Prompt,
  Release, Subscription, TeamMember, Tenant, TestScenario, VoiceBot,
  VoiceProfile, Workflow,
} from "@/types/domain";
import * as db from "./mockData";

const LATENCY: [number, number] = [220, 650];

function delay<T>(data: T, fail = false): Promise<T> {
  const ms = LATENCY[0] + Math.random() * (LATENCY[1] - LATENCY[0]);
  return new Promise((resolve, reject) =>
    setTimeout(() => (fail ? reject(new Error("Service unavailable")) : resolve(data)), ms),
  );
}

/* Deep-clone so screens can't mutate fixtures directly */
const clone = <T>(x: T): T => JSON.parse(JSON.stringify(x)) as T;

/* ---------- Tenants (Super Admin) ---------- */
export const listTenants = (): Promise<Tenant[]> => delay(clone(db.tenants));
export const getTenant = (id: string): Promise<Tenant | undefined> =>
  delay(clone(db.tenants.find((t) => t.id === id)));
export const listSubscriptions = (): Promise<Subscription[]> => delay(clone(db.subscriptions));
export const listInvoices = (): Promise<Invoice[]> => delay(clone(db.invoices));

/* ---------- Bots ---------- */
export const listBots = (): Promise<VoiceBot[]> => delay(clone(db.bots));
export const getBot = (id: string): Promise<VoiceBot | undefined> =>
  delay(clone(db.bots.find((b) => b.id === id)));

/* ---------- Knowledge ---------- */
export const listKnowledge = (botId?: string): Promise<KnowledgeSource[]> =>
  delay(clone(botId ? db.knowledgeSources.filter((k) => k.botId === botId || k.scope !== "bot") : db.knowledgeSources));
export const listKnowledgeGaps = (): Promise<KnowledgeGap[]> => delay(clone(db.knowledgeGaps));

/* ---------- Prompts / Voice ---------- */
export const listPrompts = (botId: string): Promise<Prompt[]> =>
  delay(clone(db.prompts.filter((p) => p.botId === botId)));
export const listVoices = (): Promise<VoiceProfile[]> => delay(clone(db.voices));

/* ---------- Intents / Entities / APIs / Workflows ---------- */
export const listIntents = (botId: string): Promise<Intent[]> =>
  delay(clone(db.intents.filter((i) => i.botId === botId)));
export const listEntities = (): Promise<EntityDef[]> => delay(clone(db.entities));
export const listApis = (botId?: string): Promise<ApiConnection[]> =>
  delay(clone(botId ? db.apiConnections.filter((a) => a.botId === botId) : db.apiConnections));
export const getWorkflow = (_botId: string): Promise<Workflow> => delay(clone(db.workflow));

/* ---------- Channels / Testing / Releases ---------- */
export const listChannels = (botId: string): Promise<ChannelConfig[]> =>
  delay(clone(db.channels.filter((c) => c.botId === botId)));
export const listScenarios = (botId: string): Promise<TestScenario[]> =>
  delay(clone(db.scenarios.filter((s) => s.botId === botId)));
export const listReleases = (botId: string): Promise<Release[]> =>
  delay(clone(db.releases.filter((r) => r.botId === botId)));

/* ---------- Conversations ---------- */
export const listConversations = (): Promise<Conversation[]> => delay(clone(db.conversations));
export const getConversation = (id: string): Promise<Conversation | undefined> =>
  delay(clone(db.conversations.find((c) => c.id === id)));

/* ---------- Platform ---------- */
export const listAlerts = (): Promise<PlatformAlert[]> => delay(clone(db.alerts));
export const listAudit = (): Promise<AuditEvent[]> => delay(clone(db.auditEvents));
export const listModels = (): Promise<ApprovedModel[]> => delay(clone(db.approvedModels));
export const listGuardrails = (): Promise<Guardrail[]> => delay(clone(db.guardrails));
export const listPhoneNumbers = (): Promise<PhoneNumber[]> => delay(clone(db.phoneNumbers));
export const getPlatformHealth = (): Promise<HealthMetric[]> => delay(clone(db.platformHealth));

/* ---------- Team / Integrations ---------- */
export const listTeam = (): Promise<TeamMember[]> => delay(clone(db.teamMembers));
export const listIntegrations = (): Promise<Integration[]> => delay(clone(db.integrations));

/* ---------- Analytics ---------- */
export const getTenantAnalytics = (days = 30): Promise<AnalyticsBundle> =>
  delay(db.tenantAnalytics(days));
export const getPlatformAnalytics = (days = 30) => delay(db.platformAnalytics(days));

/* ---------- Mutations (simulated) ----------
   These resolve optimistically; the real implementations must be
   idempotent and audited server-side. */
export const simulateAction = (label: string): Promise<{ ok: true; label: string }> =>
  delay({ ok: true, label });

export const testApiConnection = (id: string): Promise<{ ok: boolean; latencyMs: number; status: number; body: string }> => {
  const conn = db.apiConnections.find((a) => a.id === id);
  const failing = conn?.status === "failing";
  return delay({
    ok: !failing,
    latencyMs: failing ? 4000 : Math.round(150 + Math.random() * 400),
    status: failing ? 504 : 200,
    body: failing
      ? '{"error":"upstream timeout after 4000ms"}'
      : '{"slots":[{"start":"2026-07-09T10:15:00","provider":{"name":"Dr. Reyes"}}]}',
  });
};
