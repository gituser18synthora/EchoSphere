/* ============================================================
   AUREXION EchoSphere — Domain model
   Every API-facing shape in the app is typed here. The mock
   service layer (src/services) implements these contracts; the
   real backend replaces the implementation, not the types.
   ============================================================ */

export type Role = "super_admin" | "tenant_admin" | "tenant_user";

/* ---------- Session / RBAC ---------- */

export interface SessionUserInfo {
  id: string;
  name: string;
  firstName?: string;
  lastName?: string;
  email: string;
  phone?: string;
  avatarUrl?: string;
  locale?: string;
  timezone?: string;
  role: Role;
  roleName: string;
  tenantId: string | null;
  tenantName?: string | null;
  permissions: string[];
  status: string;
  lastLoginAt?: string | null;
  passwordChangedAt?: string | null;
}

export interface RoleInfo {
  id: string;
  code: string;
  name: string;
  description: string;
  scope: "platform" | "tenant";
  permissions: string[];
  permissionCount: number;
  members: number;
}

export interface SipTrunk {
  id: string;
  name: string;
  provider: string;
  region: string;
  capacityLines: number;
  activeCalls: number;
  failurePct: number;
  status: string;
}

/* Provider-specific settings object, validated server-side against the model's paramsSchema. */
export type ProviderSettingValue = string | number | boolean | number[] | string[] | ProviderSettings;
export interface ProviderSettings {
  [key: string]: ProviderSettingValue;
}

/** Per-language voice override; legacy entries may still be plain voice-id strings. */
export interface LanguageVoiceOverride {
  provider: string;
  model: string;
  voice: string;
  params?: ProviderSettings;
}

export interface AudioTransportSettings {
  codec: string;
  sampleRate: number;
}

export interface AudioSettings {
  browser?: AudioTransportSettings;
  telephony?: AudioTransportSettings;
}

/** Sparse platform -> tenant -> bot natural-conversation overrides. */
export interface HumanSpeechSettings {
  enabled?: boolean;
  thinking_fillers?: boolean;
  acknowledgements?: boolean;
  backchannels?: boolean;
  prosody_variation?: boolean;
  gender_agreement?: boolean;
  micro_pauses?: boolean;
  self_correction?: boolean;
  thinking_filler_probability?: number;
  acknowledgement_probability?: number;
  tool_ack_probability?: number;
  backchannel_probability?: number;
  micro_pause_probability?: number;
  self_correction_probability?: number;
  min_long_turn_for_backchannel_ms?: number;
  min_gap_between_backchannels_ms?: number;
  max_backchannels_per_call?: number;
}

export type HumanSpeechEffectiveSettings = Required<HumanSpeechSettings>;
export type HumanSpeechSettingKey = keyof HumanSpeechSettings;
export type HumanSpeechSettingSource = "platform" | "tenant" | "bot";
export type HumanSpeechSources = Record<HumanSpeechSettingKey, HumanSpeechSettingSource>;

export interface VoiceSettings {
  botId: string;
  voiceId: string | null;
  speed: number;
  pauseMs: number;
  empathy: number;
  energy: number;
  /** Per-locale overrides + reserved key "default" holding the default locale string. */
  languageVoiceMap: Record<string, string | LanguageVoiceOverride>;
  /* Runtime engine overrides — null/empty means "use the platform default" */
  sttProvider: string | null;
  sttModel: string | null;
  /** Platform locale code, or "" for auto-detect. */
  sttLanguage: string | null;
  sttSettings: ProviderSettings;
  ttsProvider: string | null;
  ttsModel: string | null;
  ttsVoice: string | null;
  ttsSettings: ProviderSettings;
  llmProvider: string | null;
  llmModel: string | null;
  llmSettings: ProviderSettings;
  fallbackProvider: string | null;
  fallbackModel: string | null;
  fallbackVoice: string | null;
  audioSettings: AudioSettings;
  /**
   * Goal Engine configuration (role, domain, goals, allowedTopics,
   * restrictedTopics, identity, slots, outOfScope, …). Empty object means
   * the runtime derives a safe default from the published prompt, intents
   * and domain policy.
   */
  goalPolicy: Record<string, unknown>;
  /** Sparse bot overrides; empty means inherit tenant/platform. */
  humanSpeech: HumanSpeechSettings;
  humanSpeechEffective: HumanSpeechEffectiveSettings;
  humanSpeechSources: HumanSpeechSources;
  humanSpeechInherited: HumanSpeechEffectiveSettings;
  humanSpeechInheritedSources: HumanSpeechSources;
}

/* ---------- Provider catalog (database-driven) ---------- */

export type VoiceCapability = "stt" | "tts" | "llm" | "embedding";

export interface ProviderInfo {
  code: string;
  name: string;
  capability: VoiceCapability;
  description: string;
  requiresApiKey: boolean;
  hasCredentials: boolean;
  /** TTS providers with a public voice-cloning API (e.g. ElevenLabs IVC). */
  supportsCloning?: boolean;
}

export interface ParamSpec {
  type: "number" | "integer" | "boolean" | "enum" | "string" | "int_list" | "string_list";
  /** Specialized control ("dictionary") rendered outside the generic fields. */
  widget?: string;
  /** Display grouping hint (e.g. "pronunciation"). */
  section?: string;
  min?: number;
  max?: number;
  step?: number;
  default?: ProviderSettingValue;
  /** Enum choices; numeric values stay numbers on the wire (e.g. Eleven v3 stability 0.0/0.5/1.0). */
  values?: (string | number)[];
  /** Optional display names per enum value, keyed by String(value) (e.g. {"0.5": "Natural"}). */
  labels?: Record<string, string>;
  label: string;
  help?: string;
  advanced?: boolean;
  fixed?: boolean;
  optional?: boolean;
  max_items?: number;
  max_length?: number;
}

export interface ProviderModelInfo {
  code: string;
  displayName: string;
  /** Concise operator-facing summary (quality/latency/streaming traits). */
  description?: string | null;
  provider: string;
  capability: VoiceCapability;
  /** Provider-native language codes; [] = language-agnostic. */
  languages: string[];
  codecs: string[];
  sampleRates: number[];
  streaming: boolean;
  paramsSchema: Record<string, ParamSpec>;
  /** TTS only: [min, max] of the model's own speed control (Sarvam `pace`,
      ElevenLabs `speed`), which Delivery tuning's speaking speed maps onto.
      null/absent means the model has no speed control (e.g. eleven_v3). */
  speedRange?: [number, number] | null;
  isDefault: boolean;
}

export interface ModelLanguagesInfo {
  /** Platform locale codes, already intersected with enabled platform languages. */
  languages: { code: string; name: string; nativeName: string | null }[];
  supportsAutoDetect: boolean;
  languageAgnostic: boolean;
}

export interface VoiceOption {
  id: string;
  name: string;
  gender: string;
  provider: string;
  providerVoiceId: string | null;
  /** [] = any language. */
  languages: string[];
  modelCodes: string[];
  locale: string | null;
  premium: boolean;
  isDefault: boolean;
  status: string;
  providerSettings: Record<string, unknown>;
  sampleText: string | null;
  /** "cloned" = tenant-created voice clone; "platform" = curated catalog voice. */
  source?: "platform" | "cloned";
}

export interface ValidateConfigResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
}

export interface ProviderTestResult {
  ok: boolean;
  latencyMs?: number;
  error?: string;
  message?: string;
}

export interface TtsPreviewResult {
  audioBase64: string;
  mimeType: string;
  sampleRate: number;
  ttfaMs: number;
  totalMs: number;
  provider: string;
  voice: string;
}

/* ---------- Voice runtime ---------- */

export interface VoiceSessionInfo {
  sessionId: string;
  botId: string;
  channel: string;
  wsPath: string;
  workerPort: number;
  expiresInSeconds: number;
}

export interface VoiceCatalog {
  providers: { stt: string[]; tts: string[]; llm: string[] };
  defaults: {
    stt: { provider: string; model: string };
    tts: { provider: string; model: string; voice: string };
    llm: { provider: string; model: string };
  };
  telephonyProviders: string[];
}

export interface TenantSettings {
  tenantId: string;
  displayName: string | null;
  timezone: string;
  defaultLanguages: string[];
  branding: { assistantName?: string; accent?: string };
  businessHours: Record<string, { open: string; close: string; closed?: boolean }>;
  holidays: { name: string; date: string }[];
  notifications: { id: string; label: string; enabled: boolean }[];
  security: { sso?: boolean; mfa?: boolean };
  retentionDays: number;
  /** Sparse tenant overrides; empty means inherit platform. */
  humanSpeech: HumanSpeechSettings;
  humanSpeechEffective: HumanSpeechEffectiveSettings;
  humanSpeechSources: HumanSpeechSources;
  humanSpeechInherited: HumanSpeechEffectiveSettings;
  humanSpeechInheritedSources: HumanSpeechSources;
}

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
  code?: string;
  domain: string;
  industry: string;
  region: string;
  aiProfileCode?: string;
  /** Assigned guardrail profile id ("" → platform-mandatory rules only). */
  guardrailProfileId: string;
  /** Summary of the assigned profile — readable even after deactivation. */
  guardrailProfile: GuardrailProfileSummary | null;
  defaultLanguages: string[];
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
  /** Generate the AI call summary / outcome / NBA after each call. */
  callSummaryEnabled: boolean;
  /** Inject the customer's previous call summary into new calls. */
  usePreviousCallSummary: boolean;
  website?: string;
  contactName?: string;
  contactPhone?: string;
  address?: string;
  country?: string;
}

/* ---------- Tenant profile (field-level permissions) ---------- */

export interface TenantProfile {
  tenantId: string;
  name: string;
  displayName: string;
  code: string;
  domain: string;
  industry: string;
  website: string;
  contactName: string;
  contactEmail: string;
  contactPhone: string;
  address: string;
  country: string;
  timezone: string;
  defaultLanguages: string[];
  branding: Record<string, string>;
  supportEmail: string;
  supportPhone: string;
  workingHours: Record<string, { open: string; close: string; closed?: boolean }>;
  /* Read-only for Tenant Admin (Super Admin controlled) */
  dataRegion: string;
  dataRegionName: string;
  dataRegionInfrastructureReady: boolean;
  plan: string;
  planName: string;
  subscriptionStatus: string;
  status: string;
  aiProfileCode: string;
}

export interface Subscription {
  id: string;
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
  /** Explicit bot-level guardrail profile id; "" → inherits the tenant default. */
  guardrailProfileId: string;
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
  tenantId?: string | null;
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
  createdAt?: string | null;
  updatedAt?: string | null;
}

export interface KnowledgeGap {
  id: string;
  question: string;
  frequency: number;
  lastAsked: string;
  suggestedSource: string;
}

/** Complete Knowledge Base detail (admin/tenant View action). */
export interface KnowledgeDetail {
  id: string;
  name: string;
  description: string;
  type: KnowledgeType;
  scope: string;
  status: IndexStatus;
  tenantId: string | null;
  tenantName: string | null;
  botId: string | null;
  botName: string | null;
  chunks: number;
  sizeKb: number;
  quality: number;
  usage30d: number;
  lastSync: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  createdBy: string | null;
  stats: {
    documentCount: number;
    readyDocuments: number;
    failedDocuments: number;
    activeChunks: number;
    embeddedChunks: number;
    embeddingModels: string[];
    lastError: string | null;
  };
  documents: DocumentStatus[];
}

/* ---------- Knowledge documents (ingestion pipeline) ---------- */

export type DocumentState = "pending" | "processing" | "ready" | "failed" | "cancelled" | "archived";

export interface DocumentStatus {
  documentId: string;
  kbId: string;
  fileName: string;
  status: DocumentState;
  stage: string;
  progress: number; // 0-100
  attempts: number;
  failureReason: string | null;
  chunkCount: number;
  pageCount: number;
  queuedAt: string | null;
  startedAt: string | null;
  finishedAt: string | null;
}

export interface DocumentUploadResult {
  documentId: string;
  jobId: string;
  kbId: string;
  duplicate: boolean;
  status: string;
}

export interface SearchTestSource {
  kbId: string;
  documentId: string;
  chunkId: string;
  chunkIndex: number;
  pageNumber: number | null;
  section: string | null;
  rank: number | null;
  score: number; // final fused score, normalized to 0..1
  vectorScore: number | null;
  keywordScore: number | null; // raw BM25 (ts_rank_cd)
  rerankScore: number | null;
  passedGate: boolean;
  text: string;
  documentName: string | null;
  meta: Record<string, unknown> | null;
}

/** Stage counts/timings from the retrieval pipeline (test console). */
export interface SearchDiagnostics {
  kbCount: number;
  queryLength: number;
  embedder?: string;
  embedError?: string | null;
  fusionMethod?: string;
  semanticWeight?: number;
  bm25Weight?: number;
  minScore?: number;
  minKeywordRank?: number;
  denseCandidates: number;
  keywordCandidates: number;
  mergedCandidates: number;
  afterDedupe?: number;
  afterGate?: number;
  reranked?: number;
  returned: number;
  timingsMs?: Record<string, number>;
  zeroResultReason: string | null;
}

export interface SearchTestResult {
  usedKnowledgeBase: boolean;
  answerable: boolean;
  confidence: number;
  query: string;
  kbIds: string[];
  durationMs: number;
  skippedReason: string | null;
  diagnostics: SearchDiagnostics | null;
  sources: SearchTestSource[];
}

/* ---------- Knowledge Chunk Review (Super Admin) ---------- */

export interface ChunkWarnings {
  shortChunk: boolean;
  emptyChunk: boolean;
  missingPage: boolean;
  missingSection: boolean;
  ocr: boolean;
  table: boolean;
  fromImage: boolean;
  promptInjection: boolean;
  flaggedForReview: boolean;
}

export interface ReviewDocument {
  documentId: string;
  tenantId: string | null;
  tenantName: string | null;
  tenantCode: string | null;
  kbId: string;
  kbName: string | null;
  fileName: string;
  fileExt: string;
  fileType: string;
  mimeType: string;
  sizeBytes: number;
  docType: string | null;
  language: string | null;
  status: DocumentState;
  uploadStatus: string;
  ingestionStatus: string;
  ingestionStage: string | null;
  ingestionProgress: number;
  attempts: number;
  failureReason: string | null;
  pageCount: number;
  chunkCount: number;
  embeddingModel: string | null;
  embeddingDimension: number | null;
  isDeleted: boolean;
  uploadedBy: string | null;
  uploadedByName: string | null;
  uploadedAt: string | null;
  processingCompletedAt: string | null;
  updatedAt: string | null;
}

export interface DocumentQuality {
  totalChunks: number;
  activeChunks: number;
  archivedChunks: number;
  minTokens: number | null;
  maxTokens: number | null;
  avgTokens: number | null;
  chunksMissingPage: number;
  chunksMissingSection: number;
  shortChunks: number;
  ocrChunks: number;
  tableChunks: number;
  promptInjectionChunks: number;
  flaggedChunks: number;
}

export interface ReviewDocumentDetail extends ReviewDocument {
  quality: DocumentQuality;
  hasOriginalFile: boolean;
}

export interface ReviewChunk {
  chunkId: string;
  documentId: string;
  kbId: string;
  kbName: string | null;
  tenantId: string | null;
  chunkIndex: number;
  pageNumber: number | null;
  section: string | null;
  topic: string | null;
  chunkType: string | null;
  language: string | null;
  keywords: string[];
  tokenCount: number | null;
  charCount: number;
  status: "active" | "archived";
  contentPreview: string;
  content: string;
  hasMetadata: boolean;
  embeddingModel: string | null;
  embeddingDimension: number | null;
  embeddingGenerated: boolean;
  createdAt: string | null;
  updatedAt: string | null;
  warnings: ChunkWarnings;
}

export interface ChunkNeighbor {
  chunkId: string;
  chunkIndex: number;
  pageNumber: number | null;
  section: string | null;
  content: string;
  status: string;
}

export interface ChunkQuality extends ChunkWarnings {
  tokenCount: number | null;
  charCount: number;
  overlapWithPrevChars: number;
  duplicate: boolean;
  duplicateCount: number;
  piiKinds: string[];
  pii: boolean;
  promptInjectionPatterns: string[];
  reviewFlag: { flagged?: boolean; reason?: string | null; by?: string | null } | null;
}

export interface ReviewChunkDetail extends ReviewChunk {
  metadata: Record<string, unknown>;
  contentHash: string;
  /** Ownership context for the detail drawer. */
  tenantName: string | null;
  fileName: string | null;
  quality: ChunkQuality;
  prev: ChunkNeighbor | null;
  current: ChunkNeighbor;
  next: ChunkNeighbor | null;
}

export interface ReviewKnowledgeBase {
  id: string;
  name: string;
  tenantId: string | null;
  scope: string;
  status: string;
  chunks: number;
}

export interface ReviewFacets {
  tenants: { id: string; name: string; code: string | null }[];
  fileTypes: string[];
  languages: string[];
  uploadStatuses: string[];
  ingestionStatuses: string[];
  chunkStatuses: string[];
}

export interface RetrievalTestHit {
  rank: number;
  chunkId: string;
  documentId: string;
  documentName: string | null;
  kbId: string;
  pageNumber: number | null;
  section: string | null;
  score: number;
  vectorScore: number;
  keywordScore: number | null;
  passedThreshold: boolean;
  text: string;
}

export interface RetrievalTestResult {
  query: string;
  kbIds: string[];
  tenantId: string | null;
  topK: number;
  threshold: number;
  confidence: number;
  answerable: boolean;
  durationMs: number;
  results: RetrievalTestHit[];
}

/* ---------- Prompts ---------- */

export type PromptType = "system" | "greeting" | "fallback" | "escalation" | "closing" | "reprompt" | "hold";
export type ApprovalState = "draft" | "pending_approval" | "approved" | "rejected" | "published" | "archived";

export interface PromptVariant {
  language: string;
  content: string;
}

/* Structured prompt configuration — compiled deterministically on the backend. */
export interface StructuredPromptConfig {
  identity?: { botName?: string; organizationName?: string; role?: string; sector?: string; responsibility?: string; allowedScope?: string };
  conversationStart?: { initialGreeting?: string; inboundGreeting?: string; outboundGreeting?: string; afterHoursGreeting?: string; recordingConsent?: string; languageSelection?: string; identityVerification?: string; reasonForCall?: boolean };
  behavior?: { tone?: string; formality?: string; style?: string; responseLength?: string; empathy?: string; confirmBeforeActions?: boolean; useCustomerName?: boolean; pronunciation?: string; numberReading?: string; dateReading?: string; currencyReading?: string };
  knowledge?: { useKb?: boolean; whenToUse?: string; noAnswerBehavior?: string; citeSources?: boolean; confidenceThreshold?: number; askClarification?: boolean; transferOnNoAnswer?: boolean };
  tools?: { allowedTools?: string[]; rules?: { tool: string; when?: string; requiredInfo?: string; confirmBefore?: boolean; onSuccess?: string; onFailure?: string; stateChanging?: boolean }[] };
  recovery?: { firstClarification?: string; secondClarification?: string; maxClarificationAttempts?: number; repeatRequest?: string; rephraseStrategy?: string; fallbackMessage?: string; handoffThreshold?: number; silenceRetryCount?: number; lowSttConfidenceBehavior?: string };
  safety?: { disallowed?: string[]; piiMasking?: boolean; authenticationRules?: string; financialAdvice?: boolean; medicalAdvice?: boolean; legalAdvice?: boolean; neverReveal?: string; escalationConditions?: string };
  handoff?: { onExplicitRequest?: boolean; onRepeatedConfusion?: boolean; onNegativeSentiment?: boolean; onHighRisk?: boolean; onFailedVerification?: boolean; onFailedApi?: boolean; onNoKbAnswer?: boolean; onComplaint?: boolean; workingHoursBehavior?: string; queueUnavailableBehavior?: string };
  closing?: { confirmResolution?: boolean; summarizeActions?: boolean; mentionReference?: boolean; askAnythingElse?: boolean; closingMessage?: string; surveyInvitation?: string; hangupDelaySeconds?: number; unresolvedClosing?: string; transferredClosing?: string };
  special?: Record<string, string>;
  advanced?: { instructions?: string };
}

export interface PromptVersion {
  version: number;
  editedBy: string;
  editedAt: string;
  note: string;
  variants: PromptVariant[];
  promptMode: "structured" | "full";
  structuredConfig?: StructuredPromptConfig | null;
  fullPrompt?: string | null;
  compiledPrompt?: string | null;
  modelCompatibility?: string[];
}

export interface Prompt {
  id: string;
  botId: string;
  type: PromptType;
  name: string;
  description?: string;
  variables: string[];
  state: ApprovalState;
  activeVersion: number;
  publishedVersion?: number | null;
  approvedBy?: string | null;
  approvedAt?: string | null;
  publishedAt?: string | null;
  versions: PromptVersion[];
}

export interface PromptCompileResult {
  compiled: string;
  valid: boolean;
  errors: { field: string; message: string }[];
  characterCount: number;
  tokenEstimate: number;
  variables?: string[];
  /** Present only when a testContext was sent and the compile was valid. */
  render?: PromptRenderResult;
}

/* A compiled prompt rendered against sample runtime-context values. */
export interface PromptRenderResult {
  rendered: string;
  variables: string[];
  missing: string[];
  unusedTestKeys: string[];
  promptVersion?: number;
  promptMode?: string;
}

export interface PromptTestResult {
  promptVersion: number;
  language: string;
  route: string;
  matchedIntent: string | null;
  intentConfidence: number;
  usedKnowledgeBase: boolean;
  sources: { documentName: string; score: number; text: string }[];
  response: string;
  latencyMs: number;
  tokens: { input: number; output: number };
  provider: string;
  error: string | null;
}

/* ---------- Runtime context (per-bot user details) ---------- */

export type RuntimeContextFieldType =
  "string" | "number" | "integer" | "boolean" | "date" | "object" | "array";

export interface RuntimeContextField {
  key: string;
  label?: string;
  type: RuntimeContextFieldType;
  required?: boolean;
  sensitive?: boolean;
  /** Trailing characters left visible when a sensitive value is masked. */
  maskKeep?: number;
  description?: string;
  example?: string;
}

export interface RuntimeContextConfig {
  id: string | null;
  botId: string;
  name: string;
  sourceMode: "api" | "manual";
  apiConnectionId: string | null;
  responsePath: string | null;
  fields: RuntimeContextField[];
  allowAdditional: boolean;
  testPayload: Record<string, unknown> | null;
  missingValuePolicy: string | null;
  domainPolicy: "generic" | "collections";
  status: string;
  configured: boolean;
}

/** One effective context value with provenance — sensitive values arrive masked. */
export interface RuntimeContextValue {
  key: string;
  value: unknown;
  source: string;
  sensitive: boolean;
}

export interface RuntimeContextValidateResult {
  valid: boolean;
  errors: { field: string; message: string }[];
  effective: RuntimeContextValue[];
  missingRequired: string[];
  declaredMissing: string[];
  promptSection: string;
}

export interface RuntimeContextRecord {
  id: string;
  botId: string;
  customerRef: string | null;
  phoneMasked: string | null;
  data: Record<string, unknown>;
  callState: Record<string, unknown>;
  updatedAt: string | null;
}

/* One simulated runtime turn — the full trace the Testing Studio renders.
   Loosely typed where the backend is loose (entities, tool payloads). */
export interface SimulateTrace {
  rawTranscript: string;
  isFinal: boolean;
  interrupted: boolean;
  botVersion: string;
  finalTranscript: string | null;
  heldForFinal?: boolean;
  note?: string;
  runtimeContext?: {
    values: RuntimeContextValue[];
    errors: { field: string; message: string }[];
    missingRequired: string[];
    domainPolicy: string;
  };
  promptId?: string | null;
  promptVersion?: number | null;
  promptMode?: string | null;
  promptState?: string | null;
  voiceIdentity?: { name: string; gender: string };
  renderedPrompt?: string;
  route?: string | null;
  action?: string | null;
  intent?: {
    intent: string | null;
    signal: string | null;
    confidence: number;
    entities: Record<string, unknown>;
    requires_tool: boolean;
    tool: string | null;
    interrupts_flow: boolean;
    below_threshold: boolean;
    source: string;
    latency_ms: number;
  };
  signal?: string | null;
  routerDecision?: { route: string; reason: string; confidence: number };
  policy?: {
    phase: string;
    blockers: string[];
    forceLlm: boolean;
    handoff: boolean;
    closeAfterReply: boolean;
    disposition: string | null;
  };
  tool?: {
    request: Record<string, unknown>;
    response: unknown;
    ok: boolean;
    status: number | null;
    error: string | null;
    mocked: boolean;
    latencyMs: number | null;
  } | null;
  workflow?: {
    name: string;
    status: string;
    nodeTrace: string[];
    slots: Record<string, unknown>;
    offScript?: boolean;
    done: boolean;
  } | null;
  response?: string | null;
  language?: string;
  sessionId?: string;
  provider?: string;
  latencyMs: number;
  paymentVerification?: string | null;
  dispositionAfterTurn?: string | null;
}

/* ---------- Voice ---------- */

export interface VoiceCloneSampleMeta {
  fileName: string;
  sizeBytes: number;
  sourceType?: "live_recording" | "file_upload";
  durationSec?: number | null;
  audioId?: string;
}

export interface VoiceCloneMetadata {
  kind?: string;
  requiresVerification?: boolean;
  removeBackgroundNoise?: boolean;
  samples?: VoiceCloneSampleMeta[];
}

/** A stored source-audio sample a cloned voice was built from. */
export interface VoiceCloneSourceAudio {
  id: string;
  voiceId: string;
  originalFilename: string;
  mimeType: string;
  sizeBytes: number;
  durationSec: number | null;
  sourceType: "live_recording" | "file_upload";
  provider: string;
  providerVoiceId: string;
  status: string;
  createdBy?: string | null;
  createdAt?: string | null;
  /** Authenticated playback endpoint (fetch with the JWT, play as blob). */
  url: string;
}

/** Provider-specific clone option rendered dynamically by the Clone Voice UI. */
export interface VoiceCloneParamSpec {
  name: string;
  type: "string" | "boolean";
  label: string;
  help?: string;
  maxLength?: number;
  default?: string | boolean;
  optional?: boolean;
}

export interface VoiceCloneProviderInfo {
  code: string;
  name: string;
  supportsCloning: boolean;
  hasCredentials: boolean;
  cloneParams: VoiceCloneParamSpec[];
  /** Set when cloning is unavailable (e.g. Sarvam has no public cloning API). */
  reason: string | null;
}

export interface VoiceCloneRecordingConfig {
  minSeconds: number;
  recommendedMinSeconds: number;
  recommendedMaxSeconds: number;
  maxSeconds: number;
}

export interface VoiceCloneConfig {
  providers: VoiceCloneProviderInfo[];
  allowedExtensions: string[];
  accept: string;
  maxFiles: number;
  maxFileMb: number;
  maxTotalMb: number;
  recording?: VoiceCloneRecordingConfig;
}

export interface VoiceProfile {
  id: string;
  tenantId?: string | null;
  source?: "platform" | "cloned";
  cloneMetadata?: VoiceCloneMetadata;
  name: string;
  gender: "female" | "male" | "neutral";
  languages: string[];
  locale?: string;
  accent: string;
  styles: string[];
  description?: string;
  latencyMs: number;
  premium: boolean;
  sample: string; // sample sentence
  provider?: string;
  providerVoiceId?: string;
  speakingRate?: number;
  pitch?: number;
  isDefault?: boolean;
  status?: string;
  modelCodes?: string[];
  providerSettings?: Record<string, unknown>;
  usageCount?: number;
  updatedAt?: string;
  /** Stored source samples (cloned voices only; empty for clones created
   *  before source-audio retention). */
  sourceAudio?: VoiceCloneSourceAudio[];
}

export interface VoiceTuning {
  speed: number; // 0.5–2
  pauseMs: number;
  empathy: number; // 0–100
  energy: number; // 0–100
}

/* ---------- Pronunciation dictionaries (Sarvam dict_id) ---------- */

/** Word → "speak as" mappings keyed by platform locale code. */
export type PronunciationMap = Record<string, Record<string, string>>;

export interface PronunciationDictionary {
  id: string;
  provider: string;
  /** Provider-assigned id (e.g. "p_5cb7faa6") — the value stored as dict_id. */
  dictId: string;
  name: string;
  description: string | null;
  languageWordCounts: Record<string, number>;
  createdAt: string | null;
  updatedAt: string | null;
  /** Present on the detail endpoint only — live provider mappings. */
  pronunciations?: PronunciationMap;
}

/* ---------- Intents & Entities ---------- */

export interface Intent {
  id: string;
  botId: string;
  name: string;
  code?: string;
  category?: string;
  description: string;
  samples: string[];
  languages?: string[];
  confidenceThreshold: number;
  avgConfidence30d: number;
  route: string; // workflow / handover target
  entities: string[];
  optionalEntities?: string[];
  workflowId?: string | null;
  apiConnectionId?: string | null;
  kbIds?: string[];
  priority?: number;
  fallbackBehavior?: string;
  handoffEnabled?: boolean;
  status: "active" | "needs_samples" | "disabled" | "archived";
  version: number;
  testPass: number;
  testTotal: number;
  updatedAt?: string;
}

export interface IntentTestResult {
  utterance: string;
  language: string;
  route: string;
  action: string | null;
  matchedIntent: string | null;
  confidence: number;
  reason: string;
  consideredKb: boolean;
  workflowId: string | null;
  apiConnectionId: string | null;
  fallbackBehavior: string;
  entities: EntityExtraction[];
}

export interface EntityExtraction {
  name: string;
  matched: boolean;
  value: string | null;
  maskedValue: string | null;
  sensitive: boolean;
  method: string;
}

export interface EntityDef {
  id: string;
  name: string;
  code?: string;
  description?: string;
  kind: "system" | "custom" | "regex" | "api";
  dataType?: string;
  languages?: string[];
  synonyms?: Record<string, string[]>;
  allowedValues?: string[];
  regexPattern?: string;
  validationRules?: Record<string, unknown>;
  normalizationRules?: Record<string, unknown>;
  maskingEnabled?: boolean;
  requireConfirmation?: boolean;
  retentionDays?: number | null;
  example: string;
  pii: boolean;
  status?: string;
  usedBy: string[];
  updatedAt?: string;
}

/* ---------- APIs ---------- */

export interface ApiConnection {
  id: string;
  botId?: string;
  name: string;
  description?: string;
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  url: string;
  authType: "none" | "api_key" | "oauth2" | "bearer" | "basic";
  secretRef?: string; // masked reference, never a raw secret
  headers?: Record<string, string>;
  queryParams?: Record<string, string>;
  pathParams?: Record<string, string>;
  bodyTemplate?: Record<string, unknown> | null;
  requestSchema?: Record<string, unknown> | null;
  responseSchema?: Record<string, unknown> | null;
  successCondition?: string;
  successMessage?: string;
  failureMessage?: string;
  errorMapping?: Record<string, string>;
  sensitiveMasks?: string[];
  allowedIntents?: string[];
  allowedWorkflows?: string[];
  isStateChanging?: boolean;
  requireConfirmation?: boolean;
  timeoutMs: number;
  retries: number;
  responseMapping: { from: string; to: string }[];
  status: "healthy" | "degraded" | "failing" | "untested" | "disabled";
  lastTestedAt?: string;
  lastLatencyMs?: number;
  version: number;
  updatedAt?: string;
}

export interface ApiTestResult {
  ok: boolean;
  latencyMs: number;
  status: number;
  contentType?: string;
  body: string;
  truncated?: boolean;
  error?: string | null;
  redirectedTo?: string | null;
  headersSent?: Record<string, string>;
  userMessage?: string | null;
}

/* ---------- Master data (Platform Configuration) ---------- */

export interface MasterCommon {
  id: string | number;
  status: string;
  usageCount: number;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
  updatedBy: string;
}

export interface IndustryMaster extends MasterCommon {
  code: string;
  name: string;
  description: string;
  icon: string;
  sortOrder: number;
  /** Recommended default guardrail profile — a suggestion, never a lock. */
  defaultGuardrailProfileId: string | null;
}

export interface CountryMaster extends MasterCommon {
  id: number;
  name: string;
  iso2: string;
  iso3: string;
  region: "Asia";
  sortOrder: number;
}

export interface DataRegionMaster extends MasterCommon {
  code: string;
  name: string;
  description: string;
  countryId: number | null;
  countryCode: string;
  countryIso2: string;
  countryIso3: string;
  country: string;
  region: string;
  cloudProvider: string;
  storageRegion: string;
  databaseRegion: string;
  recordingRegion: string;
  transcriptRegion: string;
  infrastructureReady: boolean;
  sortOrder: number;
}

export interface PlanMaster extends MasterCommon {
  code: string;
  name: string;
  description: string;
  priceMonthly: number;
  priceAnnual: number;
  currency: string;
  botLimit: number;
  minutesIncluded: number;
  seatsIncluded: number;
  kbLimit: number;
  storageGbIncluded: number;
  languagesIncluded: number;
  concurrentCallLimit: number;
  monthlyCallLimit: number;
  monthlyTokenLimit: number;
  monthlyEmbeddingLimit: number;
  recordingRetentionDays: number;
  transcriptRetentionDays: number;
  analyticsRetentionDays: number;
  features: unknown;
  overageRates: Record<string, number>;
  isPublic: boolean;
  isRecommended: boolean;
  sortOrder: number;
}

export interface AiProfileMaster extends MasterCommon {
  code: string;
  name: string;
  description: string;
  sttProvider: string | null;
  sttModel: string | null;
  llmProvider: string | null;
  llmModel: string | null;
  ttsProvider: string | null;
  ttsModel: string | null;
  defaultVoice: string | null;
  embeddingProvider: string | null;
  embeddingModel: string | null;
  embeddingDimension: number | null;
  rerankingModel: string | null;
  retrievalTopK: number;
  retrievalThreshold: number;
  temperature: number;
  maxOutputTokens: number;
  responseTimeoutMs: number;
  fallbackProviders: unknown[];
  costCategory: string;
  sortOrder: number;
}

export interface ProviderMaster extends MasterCommon {
  kind: "voice" | "stt" | "tts" | "llm" | "embedding";
  code: string;
  name: string;
  description: string;
  website: string;
  requiresApiKey: boolean;
  secretRef: string | null;
  config: Record<string, unknown>;
  sortOrder: number;
}

export interface ProviderModelMaster extends MasterCommon {
  code: string;
  name: string;
  displayName: string;
  description?: string | null;
  providerCode: string;
  capability: VoiceCapability;
  languages: string[];
  codecs: string[];
  sampleRates: number[];
  streaming: boolean;
  paramsSchema: Record<string, ParamSpec>;
  isDefault: boolean;
  sortOrder: number;
}

export interface CurrencyMaster extends MasterCommon {
  code: string;
  name: string;
  symbol: string;
  decimalPlaces: number;
  isBase: boolean;
  sortOrder: number;
}

export interface ExchangeRateMaster extends MasterCommon {
  name: string; // "USD → INR"
  baseCode: string;
  targetCode: string;
  /** Decimal string — preserves the full Numeric(18,8) precision. */
  rate: string;
  effectiveFrom: string | null;
  source: string;
  sortOrder: number;
}

export interface ProviderPricingMaster extends MasterCommon {
  name: string;
  providerCode: string;
  capability: string;
  modelCode: string;
  component: string;
  unit: string;
  /** Decimal string — preserves the full Numeric(18,10) precision. */
  unitPrice: string;
  currencyCode: string;
  effectiveFrom: string | null;
  sortOrder: number;
}

export interface LanguageMaster {
  id: string;
  code: string;
  name: string;
  nativeName: string | null;
  isoCode: string;
  script: string;
  direction: "ltr" | "rtl";
  providerSupport: { stt?: string[]; tts?: string[]; llm?: string[] };
  isDefault: boolean;
  enabled: boolean;
  sortOrder: number;
  usageCount: number;
  updatedAt: string;
}

export interface OnboardingOptions {
  industries: { code: string; name: string; icon: string; defaultGuardrailProfileId: string }[];
  dataRegions: { code: string; name: string; infrastructureReady: boolean }[];
  plans: { code: string; name: string; description: string; priceMonthly: number; minutesIncluded: number; botLimit: number; seatsIncluded: number; isRecommended: boolean }[];
  aiProfiles: { code: string; name: string; description: string; costCategory: string }[];
  languages: { code: string; name: string; nativeName: string; direction: string }[];
  guardrailProfiles: { id: string; code: string; name: string; description: string }[];
}

export interface UploadConfig {
  allowedExtensions: string[];
  maxFileMb: number;
  accept: string;
}

/* ---------- Workflows ---------- */

export type NodeKind =
  | "start"
  | "message"
  | "ask"
  | "intent"
  | "condition"
  | "api"
  | "knowledge"
  | "handover"
  | "end";

/** Per-kind execution settings interpreted by the runtime workflow engine:
    message/end/handover `text`; ask `question`/`variable`/`entityType`;
    intent `prompt`; condition `variable`/`operator`/`value`; api `name`/
    `onFailure`; knowledge `query`/`fallbackText`. */
export type WorkflowNodeConfig = Record<string, unknown>;

export interface WorkflowNode {
  id: string;
  kind: NodeKind;
  label: string;
  sub?: string;
  x: number;
  y: number;
  config?: WorkflowNodeConfig;
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

export type ChannelType = "voice" | "whatsapp" | "web" | "mobile" | "sms";

export interface ChannelTestCheck {
  name: string;
  ok: boolean;
  message: string;
}

/** What the channel routes to (resolved server-side from the bot). */
export interface ChannelBinding {
  tenantId: string;
  botId: string;
  botName: string;
  botStatus: string;
  publishedVersion: string | null;
  systemPromptPublished: boolean;
  knowledgeBases: number;
  language: string;
  voiceId: string | null;
  sttProvider: string;
  ttsProvider: string;
  llmProvider: string;
}

/** Provider-specific fields; secret fields hold env: references, never raw secrets. */
export type ChannelProviderConfig = Record<string, string | string[] | undefined>;

export interface ChannelConfig {
  id: string | null; // null until the slot is configured
  type: ChannelType;
  botId: string;
  status: "live" | "configured" | "testing" | "failed" | "not_configured";
  enabled: boolean;
  detail: string; // server-derived summary (number, origin, sender id…)
  workflow: string;
  lastTest?: { at: string; ok: boolean; message: string; checks?: ChannelTestCheck[] } | null;
  config: ChannelProviderConfig | null;
  updatedAt?: string | null;
  binding?: ChannelBinding | null;
}

/* ---------- Testing ---------- */

export interface TraceStep {
  turn: number;
  speaker: "user" | "bot";
  text: string;
  intent?: string;
  confidence?: number;
  /** Runtime routing decision for this turn (workflow/knowledge/chat/…). */
  route?: string;
  /** Saved-workflow execution detail (from the runtime engine). */
  workflowName?: string;
  workflowNodes?: string[];
  workflowSlots?: Record<string, unknown>;
  workflowDone?: boolean;
  chunksUsed?: string[];
  apiCalls?: { name: string; ms: number; ok: boolean }[];
  promptVersion?: string;
  latencyMs?: number;
  costUsd?: number;
  /** Wall-clock time of the turn (ISO, up to microsecond precision). */
  at?: string;
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
  /** Authoritative metered total in the base currency (USD), computed by the
      backend from this call's usage events. The same value backs the list row,
      the recording row and the breakdown — clients never recompute it. */
  costUsd: number;
  /** costUsd as a per-minute rate over the call's stored duration, derived by
      the backend. Null when the call has no duration (never connected). */
  costPerMinuteUsd?: number | null;
  language: string;
  qaScore?: number;
  flagged: boolean;
  /** Call outcome captured by the runtime conversation policy
      (promise_to_pay, payment_claimed, wrong_number, account_disputed,
      callback_requested, complaint_recorded, escalated, no_commitment…). */
  disposition?: string | null;
  transcript: TraceStep[];
  /** Present (non-null) only when the call's audio file is available. */
  recording?: ConversationRecording | null;
  /** Auditable per-component costing; detail endpoint only. */
  cost?: ConversationCost | null;
  /** Post-call AI intelligence (summary / outcome / Next Best Action);
      detail endpoint only, null while nothing has been generated. */
  summary?: ConversationAiSummary | null;
}

export interface ConversationCommitment {
  type?: string | null;
  description?: string | null;
  amount?: number | null;
  currency?: string | null;
  /** Absolute ISO date resolved from the customer's spoken expression. */
  dueDate?: string | null;
  status?: string | null;
}

export interface ConversationNextBestAction {
  action?: string | null;
  reason?: string | null;
  priority?: string | null;
  recommendedAt?: string | null;
}

export interface ConversationAiSummary {
  /** queued | processing | completed | failed (failed carries a deterministic fallback). */
  status: string;
  callOutcome?: string | null;
  summary?: string | null;
  customerIntent?: string | null;
  customerSentiment?: string | null;
  customerCommitments: ConversationCommitment[];
  objections: string[];
  importantFacts: string[];
  resolvedItems: string[];
  unresolvedItems: string[];
  missingSlots: string[];
  nextBestAction?: ConversationNextBestAction | null;
  followUpRequired: boolean;
  followUpAt?: string | null;
  confidence?: number | null;
  generatedAt?: string | null;
  error?: string | null;
}

/** One priced component of one usage event, as the backend costed it. */
export interface ConversationCostLine {
  capability: string;
  capabilityLabel: string;
  provider: string;
  model: string;
  voice?: string | null;
  component: string;
  componentLabel: string;
  /** Decimal strings — never parsed into a float for arithmetic. */
  quantity: string;
  unit: string;
  unitPrice: string;
  /** Currency the RATE is quoted in (Sarvam publishes INR rates). */
  rateCurrency: string;
  /** USD → rateCurrency rate applied when the cost was charged. */
  fxRate?: string | null;
  costUsd: string;
  priced: boolean;
  note?: string | null;
}

export interface ConversationCost {
  sessionId?: string | null;
  baseCurrency: string;
  totalUsd: string;
  displayCurrency: string;
  displayTotal?: string | null;
  /** Stored USD → displayCurrency rate used for the shown amount. */
  displayRate?: string | null;
  byCapability: Record<string, { label: string; costUsd: string }>;
  lines: ConversationCostLine[];
  /** "capability:provider:component" entries with usage but no configured price. */
  unpriced: string[];
  eventCount: number;
  highCost: boolean;
  highCostThresholdUsd: string;
  /** Cached conversation total, for comparison against the recomputed sum. */
  storedTotalUsd: string;
  /** False when the cached total and the recomputed sum disagree. */
  reconciled: boolean;
}

export interface ConversationRecording {
  url: string;
  mimeType: string;
  durationSec: number;
  sizeBytes: number;
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
  actorRole: Role | string;
  action: string;
  target: string;
  tenant?: string | null;
  time: string;
  ip: string;
  entityType?: string | null;
  entityId?: string | null;
}

export interface TeamMember {
  id: string;
  name: string;
  email: string;
  role: string;
  roleCode?: string;
  status: "active" | "invited" | "deactivated";
  lastActive: string;
  botsOwned: number;
  mfa?: boolean;
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
  /** Stable machine key the runtime enforcement dispatches on. */
  code: string;
  name: string;
  category: string;
  description: string;
  enforcement: "block" | "flag" | "redact";
  enabled: boolean;
  /** Platform-mandatory: applies to every tenant, cannot be disabled. */
  isMandatory: boolean;
  triggers30d: number;
}

export interface GuardrailProfileSummary {
  id: string;
  code: string;
  name: string;
  status: string;
  version: number;
}

export interface GuardrailProfile {
  id: string;
  code: string;
  name: string;
  description: string;
  status: string;
  version: number;
  usageCount: number;
  guardrailIds: string[];
  guardrails: Guardrail[];
  createdAt: string;
  updatedAt: string;
  createdBy: string;
  updatedBy: string;
}

export interface EffectiveGuardrailRule {
  guardrailId: string;
  code: string;
  name: string;
  category: string;
  action: "block" | "flag" | "redact";
  mandatory: boolean;
}

export interface EffectiveGuardrails {
  tenantId: string;
  profile: GuardrailProfileSummary | null;
  rules: EffectiveGuardrailRule[];
  degraded: boolean;
}

export interface CompliancePolicySummary {
  code: string;
  version: number;
  name: string;
  regulator: string;
  jurisdiction: string;
  timezone: string;
  callingWindows: { days?: number[]; start: string; end: string }[];
}

export interface BotEffectiveGuardrails {
  botId: string;
  tenantId: string;
  /** True when the bot has no explicit profile and follows the tenant default. */
  inherited: boolean;
  profile: GuardrailProfileSummary | null;
  tenantDefaultProfile: GuardrailProfileSummary | null;
  rules: EffectiveGuardrailRule[];
  compliancePolicies: CompliancePolicySummary[];
  degraded: boolean;
}

export interface GuardrailTrigger {
  id: string;
  tenantId: string;
  botId: string;
  sessionId: string;
  guardrailId: string;
  guardrailCode: string;
  ruleName: string;
  action: "block" | "flag" | "redact" | "escalate";
  stage: "input" | "output" | "tool" | "transcript" | "log";
  detail: string;
  profileId: string;
  profileVersion: number | null;
  channel: string;
  createdAt: string;
}

export interface PhoneNumber {
  id: string;
  number: string;
  country: string;
  tenant?: string;
  bot?: string;
  provider: string;
  status: "assigned" | "available" | "porting" | "error";
  /** Admin gate: inactive numbers keep existing routing but reject new assignments. */
  isActive: boolean;
  monthlyCost: number;
}

export interface HealthMetric {
  name: string;
  status: Severity;
  value: string;
  /** Host:port actually probed — surfaces a service bound somewhere else. */
  target: string;
  spark: number[];
  /** Monitoring tab this service belongs to ("platform" | "ai" | "telephony"). */
  group: string;
  /** Probe outcome, e.g. "http://127.0.0.1:9002/health → 200". */
  detail: string;
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

/* ---------- Customer collection context ---------- */

/** Per-customer account/collection data a collection bot runs against.
    Always masked by the API: full phone / loan account numbers are
    write-only and never round-trip. Null means UNKNOWN (distinct from
    false/zero). */
export interface CustomerContext {
  id: string;
  tenantId: string;
  botId: string;
  customerRef?: string | null;
  phoneMasked?: string | null;
  customerName?: string | null;
  dcsName?: string | null;
  lenderName?: string | null;
  loanAccountMasked?: string | null;
  preferredLanguage?: string | null;
  overdueAmount?: number | null;
  totalOutstanding?: number | null;
  minimumPayable?: number | null;
  penalCharges?: number | null;
  daysOverdue?: number | null;
  dueDate?: string | null;
  previousPromiseDate?: string | null;
  partialPaymentAllowed?: boolean | null;
  paymentMethods?: string[] | null;
  securePaymentLinkAvailable?: boolean | null;
  activeOffers?: { label?: string; terms?: string }[] | null;
  offerTerms?: string | null;
  creditReportingStatus?: string | null;
  callbackNumber?: string | null;
  grievanceContact?: string | null;
  paymentStatus: "pending" | "partial" | "completed" | "disputed" | "unknown";
  customerVerified: boolean;
  recordingNoticeRequired: boolean;
  complaintPending: boolean;
  accountDisputed: boolean;
  callbackRequested: boolean;
  callbackRequestedAt?: string | null;
  lastCallId?: string | null;
  lastDisposition?: string | null;
  isFinalTranscript: boolean;
  interruptionDetected: boolean;
  updatedAt?: string | null;
}

/** Writable fields when creating/updating a customer context (full values
    for phone/loan account are accepted on write, returned masked). */
export type CustomerContextInput = Partial<
  Omit<
    CustomerContext,
    | "id" | "tenantId" | "botId" | "phoneMasked" | "loanAccountMasked"
    | "callbackRequested" | "callbackRequestedAt" | "lastCallId"
    | "lastDisposition" | "isFinalTranscript" | "interruptionDetected"
    | "updatedAt"
  > & { phone: string; loanAccountNumber: string }
>;

/** Runtime-owned call-state flags updatable via the call-state endpoint. */
export interface CustomerContextCallState {
  customerVerified?: boolean;
  accountDisputed?: boolean;
  complaintPending?: boolean;
  paymentStatus?: CustomerContext["paymentStatus"];
  callbackRequested?: boolean;
  callbackRequestedAt?: string;
  lastCallId?: string;
  lastDisposition?: string;
  isFinalTranscript?: boolean;
  interruptionDetected?: boolean;
}
