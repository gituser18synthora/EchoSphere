/* ============================================================
   AUREXION EchoSphere — Domain model
   Every API-facing shape in the app is typed here. The mock
   service layer (src/services) implements these contracts; the
   real backend replaces the implementation, not the types.
   ============================================================ */

export type Role = "super_admin" | "tenant_admin";

export type Severity = "good" | "warning" | "serious" | "critical" | "neutral";

export type BotStatus =
  | "draft"
  | "in_review"
  | "approved"
  | "published"
  | "rolled_back"
  | "archived";

export type ReleaseStage = "draft" | "review" | "approved" | "published" | "rolled_back";

export interface Kpi {
  label: string;
  value: string;
  delta?: number; // percent vs previous period
  deltaLabel?: string;
  spark?: number[];
  intent?: "up-good" | "down-good"; // which direction is good
}

/* ---------- Tenancy ---------- */

export type TenantStatus = "active" | "trial" | "suspended" | "provisioning";
export type PlanTier = "starter" | "growth" | "enterprise";

export interface Tenant {
  id: string;
  name: string;
  domain: string;
  industry: string;
  region: string;
  plan: PlanTier;
  status: TenantStatus;
  createdAt: string;
  users: number;
  bots: number;
  callsMonth: number;
  minutesMonth: number;
  mrr: number;
  aiCostMonth: number;
  health: Severity;
  adminEmail: string;
}

export interface Subscription {
  tenantId: string;
  tenant: string;
  plan: PlanTier;
  seats: number;
  botLimit: number;
  minutesIncluded: number;
  minutesUsed: number;
  renewsAt: string;
  status: "active" | "past_due" | "cancelled" | "trial";
  mrr: number;
}

export interface Invoice {
  id: string;
  tenantId: string;
  tenant: string;
  period: string;
  amount: number;
  status: "paid" | "open" | "past_due" | "void";
  issuedAt: string;
}

/* ---------- VoiceBots ---------- */

export interface VoiceBot {
  id: string;
  tenantId: string;
  name: string;
  useCase: string;
  description: string;
  languages: string[];
  status: BotStatus;
  version: string;
  liveVersion?: string;
  owner: string;
  health: Severity;
  containment: number; // % resolved without human
  callsToday: number;
  callsMonth: number;
  avgCostPerCall: number;
  csat: number;
  channels: ChannelType[];
  voiceId?: string;
  updatedAt: string;
  publishedAt?: string;
  readiness: ReadinessItem[];
}

export interface ReadinessItem {
  id: string;
  label: string;
  done: boolean;
  studioTab: string; // deep link target
}

/* ---------- Knowledge ---------- */

export type KnowledgeType = "document" | "url" | "faq" | "connector";
export type IndexStatus = "indexed" | "indexing" | "failed" | "pending" | "stale";

export interface KnowledgeSource {
  id: string;
  botId?: string;
  scope: "bot" | "tenant" | "global";
  type: KnowledgeType;
  name: string;
  detail: string; // filename, url, connector name
  status: IndexStatus;
  chunks: number;
  sizeKb: number;
  lastSync: string;
  quality: number; // 0-100 index health
  usage30d: number; // retrieval hits
}

export interface KnowledgeGap {
  id: string;
  question: string;
  frequency: number;
  lastAsked: string;
  suggestedSource: string;
}

/* ---------- Prompts ---------- */

export type PromptType = "greeting" | "fallback" | "escalation" | "closing" | "reprompt" | "hold";
export type ApprovalState = "draft" | "pending_approval" | "approved";

export interface PromptVariant {
  language: string;
  content: string;
}

export interface PromptVersion {
  version: number;
  editedBy: string;
  editedAt: string;
  note: string;
  variants: PromptVariant[];
}

export interface Prompt {
  id: string;
  botId: string;
  type: PromptType;
  name: string;
  variables: string[];
  state: ApprovalState;
  activeVersion: number;
  versions: PromptVersion[];
}

/* ---------- Voice ---------- */

export interface VoiceProfile {
  id: string;
  name: string;
  gender: "female" | "male" | "neutral";
  languages: string[];
  accent: string;
  styles: string[];
  latencyMs: number;
  premium: boolean;
  sample: string; // sample sentence
}

export interface VoiceTuning {
  speed: number; // 0.5–2
  pauseMs: number;
  empathy: number; // 0–100
  energy: number; // 0–100
}

/* ---------- Intents & Entities ---------- */

export interface Intent {
  id: string;
  botId: string;
  name: string;
  description: string;
  samples: string[];
  confidenceThreshold: number;
  avgConfidence30d: number;
  route: string; // workflow / handover target
  entities: string[];
  status: "active" | "needs_samples" | "disabled";
  version: number;
  testPass: number;
  testTotal: number;
}

export interface EntityDef {
  id: string;
  name: string;
  kind: "system" | "custom" | "regex";
  example: string;
  pii: boolean;
  usedBy: string[];
}

/* ---------- APIs ---------- */

export interface ApiConnection {
  id: string;
  botId?: string;
  name: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  url: string;
  authType: "none" | "api_key" | "oauth2" | "bearer";
  secretRef?: string; // masked reference, never a raw secret
  timeoutMs: number;
  retries: number;
  responseMapping: { from: string; to: string }[];
  status: "healthy" | "degraded" | "failing" | "untested";
  lastTestedAt?: string;
  lastLatencyMs?: number;
  version: number;
}

/* ---------- Workflows ---------- */

export type NodeKind =
  | "start"
  | "message"
  | "intent"
  | "condition"
  | "api"
  | "knowledge"
  | "handover"
  | "end";

export interface WorkflowNode {
  id: string;
  kind: NodeKind;
  label: string;
  sub?: string;
  x: number;
  y: number;
}

export interface WorkflowEdge {
  id: string;
  from: string;
  to: string;
  label?: string;
}

export interface Workflow {
  id: string;
  botId: string;
  name: string;
  version: number;
  status: ApprovalState;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  issues: { nodeId: string; level: "warning" | "error"; message: string }[];
  updatedAt: string;
  updatedBy: string;
}

/* ---------- Channels ---------- */

export type ChannelType = "voice" | "whatsapp" | "web" | "mobile";

export interface ChannelConfig {
  type: ChannelType;
  botId: string;
  status: "live" | "configured" | "testing" | "failed" | "not_configured";
  detail: string; // number, url, sdk key ref
  workflow: string;
  lastTest?: { at: string; ok: boolean; message: string };
}

/* ---------- Testing ---------- */

export interface TraceStep {
  turn: number;
  speaker: "user" | "bot";
  text: string;
  intent?: string;
  confidence?: number;
  chunksUsed?: string[];
  apiCalls?: { name: string; ms: number; ok: boolean }[];
  promptVersion?: string;
  latencyMs?: number;
  costUsd?: number;
}

export interface TestScenario {
  id: string;
  botId: string;
  name: string;
  suite: string;
  steps: number;
  lastRun?: { at: string; pass: boolean; failedStep?: number; reason?: string };
}

/* ---------- Releases / Publish ---------- */

export interface ChecklistItem {
  id: string;
  label: string;
  ok: boolean;
  detail?: string;
}

export interface Release {
  id: string;
  botId: string;
  version: string;
  stage: ReleaseStage;
  notes: string;
  requestedBy: string;
  approvedBy?: string;
  scheduledFor?: string;
  publishedAt?: string;
  checklist: ChecklistItem[];
  diff: { area: string; change: string; kind: "added" | "changed" | "removed" }[];
}

/* ---------- Conversations ---------- */

export type SentimentLabel = "positive" | "neutral" | "negative";

export interface Conversation {
  id: string;
  botId: string;
  bot: string;
  channel: ChannelType;
  caller: string; // masked
  startedAt: string;
  durationSec: number;
  sentiment: SentimentLabel;
  intents: string[];
  contained: boolean;
  escalationReason?: string;
  csat?: number;
  costUsd: number;
  language: string;
  qaScore?: number;
  flagged: boolean;
  transcript: TraceStep[];
}

/* ---------- Monitoring, security, misc ---------- */

export interface PlatformAlert {
  id: string;
  severity: Severity;
  title: string;
  source: string;
  time: string;
  status: "open" | "acknowledged" | "resolved";
  scope: "platform" | "ai" | "telephony" | "tenant";
}

export interface AuditEvent {
  id: string;
  actor: string;
  actorRole: Role;
  action: string;
  target: string;
  tenant?: string;
  time: string;
  ip: string;
}

export interface TeamMember {
  id: string;
  name: string;
  email: string;
  role: string;
  status: "active" | "invited" | "deactivated";
  lastActive: string;
  botsOwned: number;
}

export interface Integration {
  id: string;
  name: string;
  category: string;
  description: string;
  status: "connected" | "available" | "error";
  connectedAt?: string;
}

export interface ApprovedModel {
  id: string;
  name: string;
  provider: string;
  purpose: "conversation" | "embedding" | "classification" | "summarization";
  status: "approved" | "testing" | "deprecated";
  tenantsUsing: number;
  costPer1k: number;
  latencyP50: number;
}

export interface Guardrail {
  id: string;
  name: string;
  category: string;
  description: string;
  enforcement: "block" | "flag" | "redact";
  enabled: boolean;
  triggers30d: number;
}

export interface PhoneNumber {
  id: string;
  number: string;
  country: string;
  tenant?: string;
  bot?: string;
  provider: string;
  status: "assigned" | "available" | "porting" | "error";
  monthlyCost: number;
}

export interface HealthMetric {
  name: string;
  status: Severity;
  value: string;
  target: string;
  spark: number[];
}

/* ---------- Analytics series ---------- */

export interface SeriesPoint {
  t: string; // label (day, hour)
  [series: string]: string | number;
}

export interface AnalyticsBundle {
  kpis: Kpi[];
  callsSeries: SeriesPoint[]; // t, calls, contained
  containmentSeries: SeriesPoint[]; // t, rate
  sentimentSplit: { label: string; value: number }[];
  languageMix: { label: string; value: number }[];
  topIntents: { label: string; value: number; trend: number }[];
  knowledgeUsage: { label: string; value: number }[];
  costSeries: SeriesPoint[]; // t, llm, tts, stt, telephony
  recommendations: { id: string; title: string; detail: string; impact: "high" | "medium" | "low"; link: string }[];
}
