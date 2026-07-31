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
export type ProviderSettingValue = string | number | boolean | number[];
export type ProviderSettings = Record<string, ProviderSettingValue>;

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
  type: "number" | "integer" | "boolean" | "enum" | "string" | "int_list";
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
  structuredConfig?: StructuredPromptConfig | null;
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
  industries: { code: string; name: string; icon: string }[];
  dataRegions: { code: string; name: string; infrastructureReady: boolean }[];
  plans: { code: string; name: string; description: string; priceMonthly: number; minutesIncluded: number; botLimit: number; seatsIncluded: number; isRecommended: boolean }[];
  aiProfiles: { code: string; name: string; description: string; costCategory: string }[];
  languages: { code: string; name: string; nativeName: string; direction: string }[];
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
  /** Wall-clock time of the turn (ISO), present for live voice calls. */
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
  costUsd: number;
  language: string;
  qaScore?: number;
  flagged: boolean;
  transcript: TraceStep[];
  /** Present (non-null) only when the call's audio file is available. */
  recording?: ConversationRecording | null;
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
