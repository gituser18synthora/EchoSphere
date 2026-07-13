/* Mock fixtures for AUREXION EchoSphere.
   All data here is fake and lives only in the mock service layer.
   Deterministic generators keep charts stable between reloads. */

import type {
  AnalyticsBundle, ApiConnection, ApprovedModel, AuditEvent, ChannelConfig,
  Conversation, EntityDef, Guardrail, HealthMetric, Intent, Integration,
  Invoice, KnowledgeGap, KnowledgeSource, PhoneNumber, PlatformAlert, Prompt,
  Release, SeriesPoint, Subscription, TeamMember, Tenant, TestScenario,
  TraceStep, VoiceBot, VoiceProfile, Workflow,
} from "@/types/domain";

/* Deterministic PRNG so dashboards don't jitter between renders */
export function rng(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

export function daysBack(n: number): string[] {
  const out: string[] = [];
  const now = new Date("2026-07-03T12:00:00Z");
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(now.getTime() - i * 86400000);
    out.push(d.toLocaleDateString("en-US", { month: "short", day: "numeric" }));
  }
  return out;
}

export function genSeries(seed: number, n: number, base: number, spread: number, trend = 0): number[] {
  const r = rng(seed);
  return Array.from({ length: n }, (_, i) =>
    Math.max(0, Math.round(base + trend * i + (r() - 0.5) * spread)),
  );
}

/* ---------- Tenants ---------- */

export const tenants: Tenant[] = [
  { id: "tn-001", name: "Meridian Health Group", domain: "meridianhealth.com", industry: "Healthcare", region: "US-East", plan: "enterprise", status: "active", createdAt: "2025-03-12", users: 42, bots: 6, callsMonth: 48210, minutesMonth: 156400, mrr: 12400, aiCostMonth: 3820, health: "good", adminEmail: "ops@meridianhealth.com" },
  { id: "tn-002", name: "Northwind Insurance", domain: "northwind.io", industry: "Insurance", region: "US-West", plan: "enterprise", status: "active", createdAt: "2025-05-02", users: 31, bots: 4, callsMonth: 36100, minutesMonth: 121300, mrr: 9800, aiCostMonth: 2910, health: "warning", adminEmail: "admin@northwind.io" },
  { id: "tn-003", name: "Velora Retail", domain: "velora.shop", industry: "Retail", region: "EU-Central", plan: "growth", status: "active", createdAt: "2025-06-18", users: 18, bots: 3, callsMonth: 21050, minutesMonth: 60900, mrr: 4200, aiCostMonth: 1470, health: "good", adminEmail: "it@velora.shop" },
  { id: "tn-004", name: "Apex Logistics", domain: "apexlogistics.com", industry: "Logistics", region: "US-East", plan: "growth", status: "active", createdAt: "2025-08-30", users: 12, bots: 3, callsMonth: 15800, minutesMonth: 44100, mrr: 3600, aiCostMonth: 1180, health: "good", adminEmail: "support@apexlogistics.com" },
  { id: "tn-005", name: "Banco Sol", domain: "bancosol.mx", industry: "Banking", region: "LATAM", plan: "enterprise", status: "active", createdAt: "2025-09-14", users: 27, bots: 5, callsMonth: 29400, minutesMonth: 98800, mrr: 8900, aiCostMonth: 2540, health: "serious", adminEmail: "ti@bancosol.mx" },
  { id: "tn-006", name: "TalkFlow Telecom", domain: "talkflow.net", industry: "Telecom", region: "APAC", plan: "growth", status: "trial", createdAt: "2026-06-02", users: 6, bots: 2, callsMonth: 4100, minutesMonth: 11200, mrr: 0, aiCostMonth: 380, health: "good", adminEmail: "eval@talkflow.net" },
  { id: "tn-007", name: "Cobalt Airlines", domain: "cobaltair.com", industry: "Travel", region: "EU-West", plan: "enterprise", status: "active", createdAt: "2025-11-20", users: 35, bots: 4, callsMonth: 33900, minutesMonth: 128700, mrr: 10600, aiCostMonth: 3140, health: "good", adminEmail: "digital@cobaltair.com" },
  { id: "tn-008", name: "Quill & Co.", domain: "quillco.com", industry: "Legal", region: "US-East", plan: "starter", status: "suspended", createdAt: "2025-07-08", users: 4, bots: 1, callsMonth: 0, minutesMonth: 0, mrr: 490, aiCostMonth: 0, health: "critical", adminEmail: "office@quillco.com" },
  { id: "tn-009", name: "Grove Utilities", domain: "groveutilities.com", industry: "Utilities", region: "US-Central", plan: "growth", status: "provisioning", createdAt: "2026-07-01", users: 1, bots: 0, callsMonth: 0, minutesMonth: 0, mrr: 2400, aiCostMonth: 0, health: "neutral", adminEmail: "admin@groveutilities.com" },
];

/* The tenant the Tenant Admin persona operates */
export const currentTenant = tenants[0];

/* ---------- VoiceBots (current tenant) ---------- */

const readinessFull = (done: boolean[]): VoiceBot["readiness"] => [
  { id: "r1", label: "Knowledge sources indexed", done: done[0], studioTab: "knowledge" },
  { id: "r2", label: "Voice selected & tuned", done: done[1], studioTab: "voice" },
  { id: "r3", label: "Core prompts approved", done: done[2], studioTab: "prompts" },
  { id: "r4", label: "Intents validated", done: done[3], studioTab: "intents" },
  { id: "r5", label: "Workflow published", done: done[4], studioTab: "workflows" },
  { id: "r6", label: "Channel connected", done: done[5], studioTab: "channels" },
  { id: "r7", label: "Regression suite passing", done: done[6], studioTab: "testing" },
];

export const bots: VoiceBot[] = [
  {
    id: "bot-101", tenantId: "tn-001", name: "Appointment Concierge",
    useCase: "Appointment booking", description: "Books, reschedules and cancels patient appointments across 14 clinics.",
    languages: ["en-US", "es-US"], status: "published", version: "v2.4.1", liveVersion: "v2.4.1",
    owner: "Priya Sharma", health: "good", containment: 78, callsToday: 612, callsMonth: 18240,
    avgCostPerCall: 0.14, csat: 4.5, channels: ["voice", "whatsapp"], voiceId: "vp-02",
    updatedAt: "2026-07-02T14:20:00Z", publishedAt: "2026-06-24T09:00:00Z",
    readiness: readinessFull([true, true, true, true, true, true, true]),
  },
  {
    id: "bot-102", tenantId: "tn-001", name: "Billing Helpdesk",
    useCase: "Billing support", description: "Answers billing questions, payment plans and insurance coverage checks.",
    languages: ["en-US"], status: "published", version: "v1.9.0", liveVersion: "v1.8.2",
    owner: "Marcus Webb", health: "warning", containment: 64, callsToday: 289, callsMonth: 9310,
    avgCostPerCall: 0.19, csat: 4.1, channels: ["voice"], voiceId: "vp-05",
    updatedAt: "2026-07-01T10:05:00Z", publishedAt: "2026-06-12T16:30:00Z",
    readiness: readinessFull([true, true, true, false, true, true, false]),
  },
  {
    id: "bot-103", tenantId: "tn-001", name: "Pharmacy Refill Line",
    useCase: "Prescription refills", description: "Automates refill requests and pickup notifications for pharmacy patients.",
    languages: ["en-US", "es-US", "vi-VN"], status: "in_review", version: "v0.9.0",
    owner: "Priya Sharma", health: "good", containment: 0, callsToday: 0, callsMonth: 0,
    avgCostPerCall: 0, csat: 0, channels: ["voice"], voiceId: "vp-01",
    updatedAt: "2026-07-03T08:40:00Z",
    readiness: readinessFull([true, true, true, true, true, false, true]),
  },
  {
    id: "bot-104", tenantId: "tn-001", name: "Lab Results Assistant",
    useCase: "Results & FAQs", description: "Securely shares lab result availability and answers preparation FAQs.",
    languages: ["en-US"], status: "draft", version: "v0.3.2",
    owner: "Dana Okafor", health: "neutral", containment: 0, callsToday: 0, callsMonth: 0,
    avgCostPerCall: 0, csat: 0, channels: [], updatedAt: "2026-06-29T11:15:00Z",
    readiness: readinessFull([true, false, false, false, false, false, false]),
  },
  {
    id: "bot-105", tenantId: "tn-001", name: "After-Hours Triage",
    useCase: "Nurse triage routing", description: "Screens after-hours calls and routes urgent cases to the on-call nurse line.",
    languages: ["en-US", "es-US"], status: "published", version: "v3.1.0", liveVersion: "v3.1.0",
    owner: "Marcus Webb", health: "good", containment: 71, callsToday: 148, callsMonth: 5120,
    avgCostPerCall: 0.17, csat: 4.4, channels: ["voice"], voiceId: "vp-03",
    updatedAt: "2026-06-30T19:00:00Z", publishedAt: "2026-06-30T19:00:00Z",
    readiness: readinessFull([true, true, true, true, true, true, true]),
  },
  {
    id: "bot-106", tenantId: "tn-001", name: "Patient Feedback Survey",
    useCase: "Post-visit surveys", description: "Runs short post-visit CSAT surveys over WhatsApp and web chat.",
    languages: ["en-US"], status: "rolled_back", version: "v1.2.0", liveVersion: "v1.1.3",
    owner: "Dana Okafor", health: "serious", containment: 88, callsToday: 96, callsMonth: 3480,
    avgCostPerCall: 0.05, csat: 4.7, channels: ["whatsapp", "web"], voiceId: "vp-04",
    updatedAt: "2026-07-02T22:10:00Z", publishedAt: "2026-05-30T12:00:00Z",
    readiness: readinessFull([true, true, true, true, true, true, false]),
  },
];

/* ---------- Knowledge ---------- */

export const knowledgeSources: KnowledgeSource[] = [
  { id: "ks-01", botId: "bot-101", scope: "bot", type: "document", name: "Clinic Locations & Hours", detail: "clinic-directory-2026.pdf", status: "indexed", chunks: 214, sizeKb: 1840, lastSync: "2026-07-01T06:00:00Z", quality: 96, usage30d: 4820 },
  { id: "ks-02", botId: "bot-101", scope: "bot", type: "document", name: "Appointment Policy Handbook", detail: "appt-policies-v4.docx", status: "indexed", chunks: 156, sizeKb: 920, lastSync: "2026-06-28T06:00:00Z", quality: 91, usage30d: 3110 },
  { id: "ks-03", botId: "bot-101", scope: "bot", type: "url", name: "Insurance Providers Page", detail: "meridianhealth.com/insurance", status: "stale", chunks: 48, sizeKb: 210, lastSync: "2026-06-05T06:00:00Z", quality: 62, usage30d: 1890 },
  { id: "ks-04", botId: "bot-101", scope: "bot", type: "faq", name: "Top 60 Patient FAQs", detail: "60 curated Q&A pairs", status: "indexed", chunks: 60, sizeKb: 84, lastSync: "2026-06-30T06:00:00Z", quality: 98, usage30d: 6240 },
  { id: "ks-05", botId: "bot-102", scope: "bot", type: "document", name: "Billing Codes Reference", detail: "billing-codes-2026.xlsx", status: "indexing", chunks: 0, sizeKb: 3400, lastSync: "2026-07-03T09:30:00Z", quality: 0, usage30d: 0 },
  { id: "ks-06", botId: "bot-102", scope: "bot", type: "connector", name: "Zendesk Help Center", detail: "Zendesk · 412 articles", status: "indexed", chunks: 1893, sizeKb: 12100, lastSync: "2026-07-03T02:00:00Z", quality: 88, usage30d: 5470 },
  { id: "ks-07", scope: "tenant", type: "document", name: "HIPAA Communication Guidelines", detail: "hipaa-comms-guide.pdf", status: "indexed", chunks: 89, sizeKb: 640, lastSync: "2026-06-20T06:00:00Z", quality: 94, usage30d: 2130 },
  { id: "ks-08", botId: "bot-105", scope: "bot", type: "document", name: "Triage Severity Protocols", detail: "triage-protocols-v7.pdf", status: "failed", chunks: 0, sizeKb: 2210, lastSync: "2026-07-02T18:00:00Z", quality: 0, usage30d: 0 },
  { id: "ks-09", botId: "bot-103", scope: "bot", type: "url", name: "Pharmacy Services", detail: "meridianhealth.com/pharmacy", status: "indexed", chunks: 71, sizeKb: 260, lastSync: "2026-07-01T06:00:00Z", quality: 90, usage30d: 0 },
  { id: "ks-10", scope: "tenant", type: "faq", name: "Holiday Hours FAQ", detail: "12 curated Q&A pairs", status: "pending", chunks: 0, sizeKb: 9, lastSync: "—", quality: 0, usage30d: 0 },
];

export const knowledgeGaps: KnowledgeGap[] = [
  { id: "kg-1", question: "Do you accept Aetna Medicare Advantage?", frequency: 142, lastAsked: "2026-07-03T10:12:00Z", suggestedSource: "Insurance Providers Page (stale — re-sync)" },
  { id: "kg-2", question: "Can I get a same-day X-ray appointment?", frequency: 87, lastAsked: "2026-07-03T09:41:00Z", suggestedSource: "Add imaging services doc" },
  { id: "kg-3", question: "What is the copay for a telehealth visit?", frequency: 63, lastAsked: "2026-07-02T17:05:00Z", suggestedSource: "Billing Codes Reference (indexing)" },
  { id: "kg-4", question: "Is parking validated at the Oakwood clinic?", frequency: 31, lastAsked: "2026-07-02T13:22:00Z", suggestedSource: "Add facility amenities FAQ" },
];

/* ---------- Prompts ---------- */

export const prompts: Prompt[] = [
  {
    id: "pr-01", botId: "bot-101", type: "greeting", name: "Welcome greeting",
    variables: ["{caller_name}", "{clinic_name}"], state: "approved", activeVersion: 4,
    versions: [
      { version: 4, editedBy: "Priya Sharma", editedAt: "2026-06-22T10:00:00Z", note: "Warmer tone, mention Spanish option", variants: [
        { language: "en-US", content: "Hi {caller_name}, thanks for calling {clinic_name}. I can help you book, change or cancel an appointment. You can also say “Spanish” at any time. How can I help today?" },
        { language: "es-US", content: "Hola {caller_name}, gracias por llamar a {clinic_name}. Puedo ayudarle a reservar, cambiar o cancelar una cita. ¿Cómo puedo ayudarle hoy?" },
      ]},
      { version: 3, editedBy: "Marcus Webb", editedAt: "2026-05-30T15:20:00Z", note: "Shortened opener", variants: [
        { language: "en-US", content: "Thanks for calling {clinic_name}. I can help with appointments. How can I help?" },
        { language: "es-US", content: "Gracias por llamar a {clinic_name}. Puedo ayudarle con citas. ¿Cómo puedo ayudarle?" },
      ]},
    ],
  },
  {
    id: "pr-02", botId: "bot-101", type: "fallback", name: "Low-confidence fallback",
    variables: [], state: "approved", activeVersion: 2,
    versions: [
      { version: 2, editedBy: "Priya Sharma", editedAt: "2026-06-10T09:00:00Z", note: "Offer menu of options", variants: [
        { language: "en-US", content: "Sorry, I didn’t quite catch that. You can say things like “book an appointment”, “reschedule”, or “talk to the front desk”." },
        { language: "es-US", content: "Perdón, no le entendí bien. Puede decir “reservar una cita”, “cambiar mi cita” o “hablar con recepción”." },
      ]},
    ],
  },
  {
    id: "pr-03", botId: "bot-101", type: "escalation", name: "Handover to front desk",
    variables: ["{queue_wait}"], state: "pending_approval", activeVersion: 5,
    versions: [
      { version: 6, editedBy: "Dana Okafor", editedAt: "2026-07-02T16:45:00Z", note: "Adds live wait-time variable — awaiting approval", variants: [
        { language: "en-US", content: "No problem — I’ll connect you with the front desk. The current wait is about {queue_wait}. Please stay on the line." },
        { language: "es-US", content: "Con gusto le comunico con recepción. La espera actual es de {queue_wait}. Por favor, no cuelgue." },
      ]},
      { version: 5, editedBy: "Priya Sharma", editedAt: "2026-06-01T11:30:00Z", note: "Approved baseline", variants: [
        { language: "en-US", content: "No problem — I’ll connect you with the front desk now. Please stay on the line." },
        { language: "es-US", content: "Con gusto le comunico con recepción. Por favor, no cuelgue." },
      ]},
    ],
  },
  {
    id: "pr-04", botId: "bot-101", type: "closing", name: "Call wrap-up",
    variables: ["{appointment_date}"], state: "approved", activeVersion: 3,
    versions: [
      { version: 3, editedBy: "Priya Sharma", editedAt: "2026-06-18T14:00:00Z", note: "Confirmation recap", variants: [
        { language: "en-US", content: "You’re all set for {appointment_date}. You’ll get a text confirmation shortly. Anything else I can help with?" },
        { language: "es-US", content: "Su cita quedó para {appointment_date}. Recibirá una confirmación por mensaje de texto. ¿Algo más en que pueda ayudarle?" },
      ]},
    ],
  },
  {
    id: "pr-05", botId: "bot-101", type: "hold", name: "Lookup hold message",
    variables: [], state: "draft", activeVersion: 1,
    versions: [
      { version: 1, editedBy: "Dana Okafor", editedAt: "2026-07-01T10:20:00Z", note: "New draft", variants: [
        { language: "en-US", content: "One moment while I check the schedule for you…" },
      ]},
    ],
  },
];

/* ---------- Voices ---------- */

export const voices: VoiceProfile[] = [
  { id: "vp-01", name: "Amara", gender: "female", languages: ["en-US", "es-US"], accent: "American · warm", styles: ["Empathetic", "Calm"], latencyMs: 210, premium: false, sample: "Hi there! I can help you book your next appointment in just a minute." },
  { id: "vp-02", name: "Nova", gender: "female", languages: ["en-US", "es-US", "fr-FR"], accent: "American · bright", styles: ["Friendly", "Energetic"], latencyMs: 190, premium: true, sample: "Thanks for calling! Let’s find a time that works perfectly for you." },
  { id: "vp-03", name: "Atlas", gender: "male", languages: ["en-US"], accent: "American · steady", styles: ["Professional", "Reassuring"], latencyMs: 220, premium: false, sample: "I understand. Let me route you to the right team straight away." },
  { id: "vp-04", name: "Lyra", gender: "female", languages: ["en-GB", "en-US"], accent: "British · crisp", styles: ["Professional", "Concise"], latencyMs: 205, premium: true, sample: "Certainly. Your feedback helps us improve every visit." },
  { id: "vp-05", name: "Orion", gender: "male", languages: ["en-US", "es-US"], accent: "American · deep", styles: ["Calm", "Trustworthy"], latencyMs: 230, premium: false, sample: "I can help with that billing question — one moment please." },
  { id: "vp-06", name: "Sana", gender: "female", languages: ["en-US", "hi-IN"], accent: "Neutral · soft", styles: ["Empathetic", "Patient"], latencyMs: 215, premium: false, sample: "Take your time. I’m here to help whenever you’re ready." },
  { id: "vp-07", name: "Kai", gender: "neutral", languages: ["en-US", "vi-VN"], accent: "Neutral · modern", styles: ["Friendly", "Clear"], latencyMs: 200, premium: true, sample: "Your refill is ready for pickup after 2 PM today." },
  { id: "vp-08", name: "Elena", gender: "female", languages: ["es-US", "es-MX"], accent: "Latin American · warm", styles: ["Empathetic", "Expressive"], latencyMs: 225, premium: false, sample: "Con mucho gusto le ayudo a encontrar una cita disponible." },
];

/* ---------- Intents & entities ---------- */

export const intents: Intent[] = [
  { id: "in-01", botId: "bot-101", name: "book_appointment", description: "Caller wants to schedule a new appointment", samples: ["I need to see a doctor", "book me an appointment", "can I come in tomorrow", "schedule a visit with Dr. Reyes", "I want to make an appointment for my son"], confidenceThreshold: 0.72, avgConfidence30d: 0.91, route: "Booking workflow", entities: ["date", "clinic", "provider"], status: "active", version: 7, testPass: 24, testTotal: 24 },
  { id: "in-02", botId: "bot-101", name: "reschedule_appointment", description: "Caller wants to move an existing appointment", samples: ["change my appointment", "move my visit to next week", "I can't make it Friday"], confidenceThreshold: 0.72, avgConfidence30d: 0.87, route: "Reschedule workflow", entities: ["date", "appointment_id"], status: "active", version: 5, testPass: 18, testTotal: 19 },
  { id: "in-03", botId: "bot-101", name: "cancel_appointment", description: "Caller wants to cancel", samples: ["cancel my appointment", "I need to cancel Friday's visit"], confidenceThreshold: 0.75, avgConfidence30d: 0.93, route: "Cancel workflow", entities: ["appointment_id"], status: "active", version: 4, testPass: 12, testTotal: 12 },
  { id: "in-04", botId: "bot-101", name: "insurance_question", description: "Coverage and network questions", samples: ["do you take Blue Cross", "is my insurance accepted"], confidenceThreshold: 0.7, avgConfidence30d: 0.66, route: "Knowledge answer", entities: ["insurer"], status: "needs_samples", version: 3, testPass: 7, testTotal: 11 },
  { id: "in-05", botId: "bot-101", name: "talk_to_human", description: "Explicit request for a person", samples: ["let me talk to someone", "front desk please", "operator"], confidenceThreshold: 0.6, avgConfidence30d: 0.95, route: "Human handover", entities: [], status: "active", version: 2, testPass: 9, testTotal: 9 },
  { id: "in-06", botId: "bot-101", name: "clinic_hours", description: "Opening hours and locations", samples: ["what time do you open", "are you open Saturday"], confidenceThreshold: 0.7, avgConfidence30d: 0.9, route: "Knowledge answer", entities: ["clinic"], status: "active", version: 3, testPass: 10, testTotal: 10 },
];

export const entities: EntityDef[] = [
  { id: "en-01", name: "date", kind: "system", example: "“next Tuesday at 3” → 2026-07-07T15:00", pii: false, usedBy: ["book_appointment", "reschedule_appointment"] },
  { id: "en-02", name: "clinic", kind: "custom", example: "“Oakwood” → clinic_id 14", pii: false, usedBy: ["book_appointment", "clinic_hours"] },
  { id: "en-03", name: "provider", kind: "custom", example: "“Dr. Reyes” → provider_id 88", pii: false, usedBy: ["book_appointment"] },
  { id: "en-04", name: "appointment_id", kind: "regex", example: "“APT-58201” → 58201", pii: false, usedBy: ["reschedule_appointment", "cancel_appointment"] },
  { id: "en-05", name: "insurer", kind: "custom", example: "“Blue Cross” → payer BCBS", pii: false, usedBy: ["insurance_question"] },
  { id: "en-06", name: "date_of_birth", kind: "system", example: "“March 4th 1985” → 1985-03-04", pii: true, usedBy: ["identity_verification"] },
  { id: "en-07", name: "phone_number", kind: "system", example: "“555 0142” → +15550142", pii: true, usedBy: ["identity_verification", "book_appointment"] },
];

/* ---------- APIs ---------- */

export const apiConnections: ApiConnection[] = [
  { id: "api-01", botId: "bot-101", name: "EHR Slot Availability", method: "GET", url: "https://api.meridianhealth.com/ehr/v2/slots", authType: "oauth2", secretRef: "secret://tenants/tn-001/ehr-oauth", timeoutMs: 4000, retries: 2, responseMapping: [{ from: "$.slots[*].start", to: "available_times" }, { from: "$.slots[*].provider.name", to: "provider_name" }], status: "healthy", lastTestedAt: "2026-07-03T08:00:00Z", lastLatencyMs: 340, version: 6 },
  { id: "api-02", botId: "bot-101", name: "Create Appointment", method: "POST", url: "https://api.meridianhealth.com/ehr/v2/appointments", authType: "oauth2", secretRef: "secret://tenants/tn-001/ehr-oauth", timeoutMs: 6000, retries: 1, responseMapping: [{ from: "$.appointment.id", to: "appointment_id" }, { from: "$.appointment.confirmed_at", to: "appointment_date" }], status: "healthy", lastTestedAt: "2026-07-03T08:00:00Z", lastLatencyMs: 520, version: 4 },
  { id: "api-03", botId: "bot-101", name: "SMS Confirmation", method: "POST", url: "https://api.meridianhealth.com/notify/sms", authType: "api_key", secretRef: "secret://tenants/tn-001/notify-key", timeoutMs: 3000, retries: 3, responseMapping: [{ from: "$.message_id", to: "sms_id" }], status: "degraded", lastTestedAt: "2026-07-03T07:45:00Z", lastLatencyMs: 1840, version: 2 },
  { id: "api-04", botId: "bot-102", name: "Billing Balance Lookup", method: "GET", url: "https://api.meridianhealth.com/billing/v1/balance", authType: "bearer", secretRef: "secret://tenants/tn-001/billing-token", timeoutMs: 4000, retries: 2, responseMapping: [{ from: "$.balance.amount", to: "balance_due" }], status: "failing", lastTestedAt: "2026-07-02T22:10:00Z", lastLatencyMs: 0, version: 3 },
  { id: "api-05", botId: "bot-105", name: "On-call Roster", method: "GET", url: "https://api.meridianhealth.com/staff/oncall", authType: "api_key", secretRef: "secret://tenants/tn-001/staff-key", timeoutMs: 2500, retries: 2, responseMapping: [{ from: "$.oncall.phone", to: "oncall_number" }], status: "healthy", lastTestedAt: "2026-07-03T06:00:00Z", lastLatencyMs: 180, version: 1 },
];

/* ---------- Workflow ---------- */

export const workflow: Workflow = {
  id: "wf-01", botId: "bot-101", name: "Booking journey", version: 12, status: "approved",
  updatedAt: "2026-07-02T15:30:00Z", updatedBy: "Priya Sharma",
  nodes: [
    { id: "n1", kind: "start", label: "Call starts", sub: "Voice · WhatsApp", x: 40, y: 40 },
    { id: "n2", kind: "message", label: "Welcome greeting", sub: "Prompt v4", x: 40, y: 150 },
    { id: "n3", kind: "intent", label: "Detect intent", sub: "6 intents", x: 40, y: 260 },
    { id: "n4", kind: "api", label: "EHR Slot Availability", sub: "GET · 340ms p50", x: 252, y: 190 },
    { id: "n5", kind: "condition", label: "Slots found?", sub: "available_times > 0", x: 252, y: 310 },
    { id: "n6", kind: "api", label: "Create Appointment", sub: "POST", x: 462, y: 250 },
    { id: "n7", kind: "message", label: "Confirm & recap", sub: "Prompt v3", x: 462, y: 370 },
    { id: "n8", kind: "knowledge", label: "Answer from knowledge", sub: "4 sources", x: 252, y: 60 },
    { id: "n9", kind: "handover", label: "Front desk handover", sub: "Queue: reception", x: 462, y: 60 },
    { id: "n10", kind: "end", label: "End call", sub: "Survey via SMS", x: 462, y: 480 },
  ],
  edges: [
    { id: "e1", from: "n1", to: "n2" },
    { id: "e2", from: "n2", to: "n3" },
    { id: "e3", from: "n3", to: "n4", label: "book / reschedule" },
    { id: "e4", from: "n3", to: "n8", label: "FAQ" },
    { id: "e5", from: "n4", to: "n5" },
    { id: "e6", from: "n5", to: "n6", label: "yes" },
    { id: "e7", from: "n5", to: "n9", label: "no slots" },
    { id: "e8", from: "n6", to: "n7" },
    { id: "e9", from: "n8", to: "n9", label: "low confidence" },
    { id: "e10", from: "n7", to: "n10" },
  ],
  issues: [
    { nodeId: "n9", level: "warning", message: "No after-hours fallback configured for handover when reception queue is closed." },
    { nodeId: "n8", level: "warning", message: "Knowledge source “Insurance Providers Page” is stale (28 days)." },
  ],
};

/* ---------- Channels ---------- */

export const channels: ChannelConfig[] = [
  { type: "voice", botId: "bot-101", status: "live", detail: "+1 (415) 555-0119 · 4 lines", workflow: "Booking journey v12", lastTest: { at: "2026-07-02T09:00:00Z", ok: true, message: "Test call completed · 3.2s connect" } },
  { type: "whatsapp", botId: "bot-101", status: "live", detail: "Business acct · meridian-health", workflow: "Booking journey v12", lastTest: { at: "2026-07-01T14:00:00Z", ok: true, message: "Template messages verified" } },
  { type: "web", botId: "bot-101", status: "testing", detail: "widget key wgt_…f24e", workflow: "Booking journey v12", lastTest: { at: "2026-07-03T08:30:00Z", ok: false, message: "CORS origin missing for portal.meridianhealth.com" } },
  { type: "mobile", botId: "bot-101", status: "not_configured", detail: "SDK not installed", workflow: "—" },
];

/* ---------- Testing ---------- */

export const scenarios: TestScenario[] = [
  { id: "ts-01", botId: "bot-101", name: "Happy path — new booking (EN)", suite: "Booking", steps: 9, lastRun: { at: "2026-07-03T07:00:00Z", pass: true } },
  { id: "ts-02", botId: "bot-101", name: "Happy path — new booking (ES)", suite: "Booking", steps: 9, lastRun: { at: "2026-07-03T07:00:00Z", pass: true } },
  { id: "ts-03", botId: "bot-101", name: "Reschedule with appointment ID", suite: "Booking", steps: 7, lastRun: { at: "2026-07-03T07:00:00Z", pass: true } },
  { id: "ts-04", botId: "bot-101", name: "No slots available → handover", suite: "Edge cases", steps: 6, lastRun: { at: "2026-07-03T07:01:00Z", pass: false, failedStep: 5, reason: "Expected handover message, got fallback prompt (low intent confidence 0.58)" } },
  { id: "ts-05", botId: "bot-101", name: "Insurance question from knowledge", suite: "Knowledge", steps: 4, lastRun: { at: "2026-07-03T07:01:00Z", pass: false, failedStep: 3, reason: "Retrieved chunk from stale source; answer outdated" } },
  { id: "ts-06", botId: "bot-101", name: "Explicit human request", suite: "Edge cases", steps: 3, lastRun: { at: "2026-07-03T07:02:00Z", pass: true } },
  { id: "ts-07", botId: "bot-101", name: "Caller interrupts mid-sentence", suite: "Voice UX", steps: 5, lastRun: { at: "2026-07-03T07:02:00Z", pass: true } },
  { id: "ts-08", botId: "bot-101", name: "Background noise / low ASR", suite: "Voice UX", steps: 6 },
];

/* ---------- Releases ---------- */

export const releases: Release[] = [
  {
    id: "rel-06", botId: "bot-101", version: "v2.5.0", stage: "review",
    notes: "Adds live wait-time to escalation prompt; re-synced insurance knowledge; 2 new intent samples.",
    requestedBy: "Dana Okafor",
    checklist: [
      { id: "c1", label: "All regression tests passing", ok: false, detail: "2 of 8 scenarios failing" },
      { id: "c2", label: "Prompts approved", ok: false, detail: "Escalation prompt v6 pending approval" },
      { id: "c3", label: "Knowledge sources fresh (<14 days)", ok: false, detail: "1 stale source" },
      { id: "c4", label: "Workflow validation clean", ok: false, detail: "2 warnings" },
      { id: "c5", label: "Channels tested", ok: false, detail: "Web widget test failing" },
      { id: "c6", label: "No unresolved critical alerts", ok: true },
    ],
    diff: [
      { area: "Prompts", change: "Escalation prompt v5 → v6 (adds {queue_wait})", kind: "changed" },
      { area: "Knowledge", change: "Insurance Providers Page re-sync scheduled", kind: "changed" },
      { area: "Intents", change: "insurance_question +2 samples", kind: "added" },
      { area: "Workflow", change: "No changes", kind: "changed" },
    ],
  },
  {
    id: "rel-05", botId: "bot-101", version: "v2.4.1", stage: "published",
    notes: "Hotfix: SMS confirmation retries raised to 3.",
    requestedBy: "Priya Sharma", approvedBy: "Marcus Webb", publishedAt: "2026-06-24T09:00:00Z",
    checklist: [
      { id: "c1", label: "All regression tests passing", ok: true },
      { id: "c2", label: "Prompts approved", ok: true },
      { id: "c3", label: "Knowledge sources fresh (<14 days)", ok: true },
      { id: "c4", label: "Workflow validation clean", ok: true },
      { id: "c5", label: "Channels tested", ok: true },
      { id: "c6", label: "No unresolved critical alerts", ok: true },
    ],
    diff: [{ area: "APIs", change: "SMS Confirmation retries 1 → 3", kind: "changed" }],
  },
  {
    id: "rel-04", botId: "bot-101", version: "v2.4.0", stage: "published",
    notes: "Spanish language variant for all prompts; Elena voice mapping for es-US.",
    requestedBy: "Priya Sharma", approvedBy: "Marcus Webb", publishedAt: "2026-06-15T10:00:00Z",
    checklist: [],
    diff: [
      { area: "Prompts", change: "es-US variants added to 5 prompts", kind: "added" },
      { area: "Voice", change: "es-US → Elena mapping", kind: "added" },
    ],
  },
  {
    id: "rel-03", botId: "bot-101", version: "v2.3.2", stage: "rolled_back",
    notes: "Aggressive barge-in tuning caused callers to be cut off. Rolled back 4h after publish.",
    requestedBy: "Marcus Webb", approvedBy: "Priya Sharma", publishedAt: "2026-06-08T13:00:00Z",
    checklist: [],
    diff: [{ area: "Voice", change: "Barge-in sensitivity high → default", kind: "changed" }],
  },
];

/* ---------- Conversations ---------- */

const transcriptBooking: TraceStep[] = [
  { turn: 1, speaker: "bot", text: "Hi, thanks for calling Meridian Health. I can help you book, change or cancel an appointment. How can I help today?", promptVersion: "greeting v4", latencyMs: 480, costUsd: 0.004 },
  { turn: 2, speaker: "user", text: "Yeah hi, I need to see Dr. Reyes sometime this week if possible." },
  { turn: 3, speaker: "bot", text: "Sure — let me check Dr. Reyes' availability this week.", intent: "book_appointment", confidence: 0.94, apiCalls: [{ name: "EHR Slot Availability", ms: 356, ok: true }], latencyMs: 820, costUsd: 0.006 },
  { turn: 4, speaker: "bot", text: "Dr. Reyes has Thursday at 10:15 AM or Friday at 2:40 PM at the Oakwood clinic. Would either of those work?", chunksUsed: ["Clinic Locations & Hours §3"], latencyMs: 510, costUsd: 0.005 },
  { turn: 5, speaker: "user", text: "Thursday morning works great." },
  { turn: 6, speaker: "bot", text: "Perfect. Can I confirm your date of birth for verification?", intent: "book_appointment", confidence: 0.9, latencyMs: 430, costUsd: 0.004 },
  { turn: 7, speaker: "user", text: "March 4th, 1985." },
  { turn: 8, speaker: "bot", text: "Thanks. You're all set for Thursday, July 9 at 10:15 AM with Dr. Reyes at Oakwood. You'll get a text confirmation shortly. Anything else?", apiCalls: [{ name: "Create Appointment", ms: 540, ok: true }, { name: "SMS Confirmation", ms: 1620, ok: true }], promptVersion: "closing v3", latencyMs: 940, costUsd: 0.008 },
  { turn: 9, speaker: "user", text: "No that's it, thank you!" },
];

const transcriptEscalation: TraceStep[] = [
  { turn: 1, speaker: "bot", text: "Hi, thanks for calling Meridian Health billing support. How can I help?", promptVersion: "greeting v2", latencyMs: 460, costUsd: 0.004 },
  { turn: 2, speaker: "user", text: "I got charged twice for my last visit and I'm pretty upset about it." },
  { turn: 3, speaker: "bot", text: "I'm sorry about that — let me pull up your balance.", intent: "billing_dispute", confidence: 0.81, apiCalls: [{ name: "Billing Balance Lookup", ms: 4000, ok: false }], latencyMs: 4620, costUsd: 0.007 },
  { turn: 4, speaker: "bot", text: "I'm having trouble reaching the billing system right now. Let me connect you with a billing specialist.", promptVersion: "escalation v5", latencyMs: 520, costUsd: 0.004 },
  { turn: 5, speaker: "user", text: "Fine, please hurry." },
];

export const conversations: Conversation[] = [
  { id: "cv-9001", botId: "bot-101", bot: "Appointment Concierge", channel: "voice", caller: "+1 •••-0184", startedAt: "2026-07-03T10:42:00Z", durationSec: 154, sentiment: "positive", intents: ["book_appointment"], contained: true, csat: 5, costUsd: 0.14, language: "en-US", qaScore: 96, flagged: false, transcript: transcriptBooking },
  { id: "cv-9002", botId: "bot-102", bot: "Billing Helpdesk", channel: "voice", caller: "+1 •••-3327", startedAt: "2026-07-03T10:31:00Z", durationSec: 208, sentiment: "negative", intents: ["billing_dispute"], contained: false, escalationReason: "API failure — Billing Balance Lookup timeout", csat: 2, costUsd: 0.21, language: "en-US", qaScore: 61, flagged: true, transcript: transcriptEscalation },
  { id: "cv-9003", botId: "bot-101", bot: "Appointment Concierge", channel: "whatsapp", caller: "+1 •••-8850", startedAt: "2026-07-03T10:18:00Z", durationSec: 96, sentiment: "neutral", intents: ["reschedule_appointment"], contained: true, csat: 4, costUsd: 0.06, language: "es-US", qaScore: 88, flagged: false, transcript: transcriptBooking.slice(0, 6) },
  { id: "cv-9004", botId: "bot-105", bot: "After-Hours Triage", channel: "voice", caller: "+1 •••-2211", startedAt: "2026-07-03T04:02:00Z", durationSec: 312, sentiment: "negative", intents: ["urgent_symptoms", "talk_to_human"], contained: false, escalationReason: "Urgency rule — routed to on-call nurse", csat: 4, costUsd: 0.24, language: "en-US", qaScore: 92, flagged: false, transcript: transcriptEscalation.slice(0, 4) },
  { id: "cv-9005", botId: "bot-101", bot: "Appointment Concierge", channel: "voice", caller: "+1 •••-6402", startedAt: "2026-07-03T09:55:00Z", durationSec: 187, sentiment: "neutral", intents: ["insurance_question", "book_appointment"], contained: true, csat: 3, costUsd: 0.16, language: "en-US", qaScore: 74, flagged: true, transcript: transcriptBooking.slice(0, 8) },
  { id: "cv-9006", botId: "bot-106", bot: "Patient Feedback Survey", channel: "whatsapp", caller: "+1 •••-1177", startedAt: "2026-07-03T09:40:00Z", durationSec: 64, sentiment: "positive", intents: ["survey_response"], contained: true, csat: 5, costUsd: 0.03, language: "en-US", qaScore: 98, flagged: false, transcript: transcriptBooking.slice(0, 4) },
  { id: "cv-9007", botId: "bot-101", bot: "Appointment Concierge", channel: "voice", caller: "+1 •••-9034", startedAt: "2026-07-03T09:22:00Z", durationSec: 243, sentiment: "negative", intents: ["insurance_question"], contained: false, escalationReason: "Low intent confidence (0.58) after 2 fallbacks", csat: 2, costUsd: 0.2, language: "en-US", qaScore: 58, flagged: true, transcript: transcriptEscalation },
  { id: "cv-9008", botId: "bot-105", bot: "After-Hours Triage", channel: "voice", caller: "+1 •••-4415", startedAt: "2026-07-03T02:48:00Z", durationSec: 126, sentiment: "neutral", intents: ["clinic_hours"], contained: true, csat: 4, costUsd: 0.11, language: "es-US", qaScore: 90, flagged: false, transcript: transcriptBooking.slice(0, 5) },
];

/* ---------- Platform (Super Admin) ---------- */

export const alerts: PlatformAlert[] = [
  { id: "al-01", severity: "critical", title: "Telephony trunk EU-West-2 degraded — 8.2% call failures", source: "SIP · Voxbone trunk 3", time: "2026-07-03T10:05:00Z", status: "open", scope: "telephony" },
  { id: "al-02", severity: "serious", title: "Banco Sol: containment dropped 14pts after v3.0 publish", source: "Tenant tn-005 · anomaly detector", time: "2026-07-03T08:50:00Z", status: "acknowledged", scope: "tenant" },
  { id: "al-03", severity: "warning", title: "Embedding queue backlog above 10 min (14,220 chunks)", source: "Knowledge pipeline", time: "2026-07-03T07:30:00Z", status: "open", scope: "ai" },
  { id: "al-04", severity: "warning", title: "Northwind Insurance approaching plan minute limit (92%)", source: "Usage metering", time: "2026-07-02T21:15:00Z", status: "open", scope: "tenant" },
  { id: "al-05", severity: "good", title: "STT provider latency recovered to p95 280ms", source: "AI health monitor", time: "2026-07-02T18:40:00Z", status: "resolved", scope: "ai" },
];

export const auditEvents: AuditEvent[] = [
  { id: "au-01", actor: "Priya Sharma", actorRole: "tenant_admin", action: "Submitted release for review", target: "Appointment Concierge v2.5.0", tenant: "Meridian Health Group", time: "2026-07-03T09:12:00Z", ip: "73.92.14.8" },
  { id: "au-02", actor: "System", actorRole: "super_admin", action: "Auto-rolled back publish (error-rate guard)", target: "Patient Feedback Survey v1.2.0", tenant: "Meridian Health Group", time: "2026-07-02T22:10:00Z", ip: "—" },
  { id: "au-03", actor: "Alex Rivera", actorRole: "super_admin", action: "Suspended tenant (payment failure)", target: "Quill & Co.", time: "2026-07-02T16:00:00Z", ip: "10.4.1.22" },
  { id: "au-04", actor: "Dana Okafor", actorRole: "tenant_admin", action: "Edited prompt (pending approval)", target: "Handover to front desk v6", tenant: "Meridian Health Group", time: "2026-07-02T16:45:00Z", ip: "73.92.14.31" },
  { id: "au-05", actor: "Alex Rivera", actorRole: "super_admin", action: "Approved model for production", target: "sonnet-5 · conversation", time: "2026-07-01T11:00:00Z", ip: "10.4.1.22" },
  { id: "au-06", actor: "Marcus Webb", actorRole: "tenant_admin", action: "Rotated API secret reference", target: "secret://tenants/tn-001/notify-key", tenant: "Meridian Health Group", time: "2026-07-01T09:30:00Z", ip: "73.92.15.2" },
];

export const approvedModels: ApprovedModel[] = [
  { id: "md-01", name: "sonnet-5", provider: "Anthropic", purpose: "conversation", status: "approved", tenantsUsing: 41, costPer1k: 0.003, latencyP50: 640 },
  { id: "md-02", name: "haiku-4.5", provider: "Anthropic", purpose: "classification", status: "approved", tenantsUsing: 47, costPer1k: 0.0008, latencyP50: 210 },
  { id: "md-03", name: "opus-4.8", provider: "Anthropic", purpose: "conversation", status: "testing", tenantsUsing: 3, costPer1k: 0.012, latencyP50: 980 },
  { id: "md-04", name: "embed-multilingual-3", provider: "VectorWorks", purpose: "embedding", status: "approved", tenantsUsing: 47, costPer1k: 0.0001, latencyP50: 45 },
  { id: "md-05", name: "summarize-lite-2", provider: "VectorWorks", purpose: "summarization", status: "deprecated", tenantsUsing: 5, costPer1k: 0.0004, latencyP50: 380 },
];

export const guardrails: Guardrail[] = [
  { id: "gr-01", name: "PII redaction in transcripts", category: "Privacy", description: "Redacts SSN, card numbers and DOB from stored transcripts and logs.", enforcement: "redact", enabled: true, triggers30d: 12840 },
  { id: "gr-02", name: "Medical advice boundary", category: "Safety", description: "Blocks diagnosis or dosage advice; routes to licensed staff.", enforcement: "block", enabled: true, triggers30d: 431 },
  { id: "gr-03", name: "Payment collection restriction", category: "Compliance", description: "Bots may reference balances but never collect card numbers by voice.", enforcement: "block", enabled: true, triggers30d: 96 },
  { id: "gr-04", name: "Competitor mention flag", category: "Brand", description: "Flags conversations where competitors are discussed for QA review.", enforcement: "flag", enabled: false, triggers30d: 0 },
  { id: "gr-05", name: "Profanity / abuse de-escalation", category: "Safety", description: "Switches to calm register and offers human handover on repeated abuse.", enforcement: "flag", enabled: true, triggers30d: 1210 },
];

export const phoneNumbers: PhoneNumber[] = [
  { id: "pn-01", number: "+1 (415) 555-0119", country: "US", tenant: "Meridian Health Group", bot: "Appointment Concierge", provider: "Twilio", status: "assigned", monthlyCost: 1.15 },
  { id: "pn-02", number: "+1 (415) 555-0184", country: "US", tenant: "Meridian Health Group", bot: "Billing Helpdesk", provider: "Twilio", status: "assigned", monthlyCost: 1.15 },
  { id: "pn-03", number: "+44 20 7946 0958", country: "GB", tenant: "Cobalt Airlines", bot: "Flight Status Line", provider: "Voxbone", status: "assigned", monthlyCost: 2.4 },
  { id: "pn-04", number: "+52 55 4170 8821", country: "MX", tenant: "Banco Sol", bot: "Saldo y Movimientos", provider: "Telnyx", status: "assigned", monthlyCost: 3.1 },
  { id: "pn-05", number: "+1 (628) 555-0022", country: "US", provider: "Twilio", status: "available", monthlyCost: 1.15 },
  { id: "pn-06", number: "+49 30 901820", country: "DE", tenant: "Velora Retail", provider: "Voxbone", status: "porting", monthlyCost: 2.2 },
];

export const platformHealth: HealthMetric[] = [
  { name: "API gateway", status: "good", value: "99.98% uptime", target: "≥99.95%", spark: genSeries(11, 24, 99.9, 0.15) },
  { name: "Call orchestration", status: "good", value: "142ms p95", target: "<250ms", spark: genSeries(12, 24, 140, 40) },
  { name: "SIP trunks", status: "critical", value: "8.2% failures EU-West-2", target: "<0.5%", spark: genSeries(13, 24, 2, 6, 0.3) },
  { name: "STT latency", status: "good", value: "280ms p95", target: "<400ms", spark: genSeries(14, 24, 300, 80, -2) },
  { name: "LLM latency", status: "warning", value: "890ms p95", target: "<800ms", spark: genSeries(15, 24, 800, 150, 4) },
  { name: "TTS latency", status: "good", value: "210ms p95", target: "<300ms", spark: genSeries(16, 24, 220, 50) },
  { name: "Embedding queue", status: "warning", value: "11.4 min backlog", target: "<5 min", spark: genSeries(17, 24, 5, 6, 0.3) },
  { name: "Recording storage", status: "good", value: "61% used", target: "<80%", spark: genSeries(18, 24, 58, 4, 0.15) },
];

export const teamMembers: TeamMember[] = [
  { id: "tm-01", name: "Priya Sharma", email: "priya.sharma@meridianhealth.com", role: "Tenant Admin", status: "active", lastActive: "2026-07-03T10:50:00Z", botsOwned: 2 },
  { id: "tm-02", name: "Marcus Webb", email: "marcus.webb@meridianhealth.com", role: "Bot Manager", status: "active", lastActive: "2026-07-03T09:20:00Z", botsOwned: 2 },
  { id: "tm-03", name: "Dana Okafor", email: "dana.okafor@meridianhealth.com", role: "Content Editor", status: "active", lastActive: "2026-07-02T17:10:00Z", botsOwned: 2 },
  { id: "tm-04", name: "Sam Ellery", email: "sam.ellery@meridianhealth.com", role: "QA Reviewer", status: "active", lastActive: "2026-07-01T15:40:00Z", botsOwned: 0 },
  { id: "tm-05", name: "Jordan Liu", email: "jordan.liu@meridianhealth.com", role: "Analyst (read-only)", status: "invited", lastActive: "—", botsOwned: 0 },
];

export const integrations: Integration[] = [
  { id: "ig-01", name: "Epic EHR", category: "Healthcare", description: "Appointment slots, patient verification and scheduling.", status: "connected", connectedAt: "2025-04-02" },
  { id: "ig-02", name: "Zendesk", category: "Support", description: "Help-center articles as a live knowledge connector.", status: "connected", connectedAt: "2025-06-11" },
  { id: "ig-03", name: "Salesforce Health Cloud", category: "CRM", description: "Sync caller context and escalation cases.", status: "error", connectedAt: "2025-09-20" },
  { id: "ig-04", name: "Slack", category: "Notifications", description: "Publish, rollback and alert notifications to channels.", status: "connected", connectedAt: "2025-05-15" },
  { id: "ig-05", name: "Genesys Cloud", category: "Contact Center", description: "Warm-transfer escalations into agent queues.", status: "available" },
  { id: "ig-06", name: "Microsoft Teams", category: "Notifications", description: "Approval requests and daily digest cards.", status: "available" },
];

export const subscriptions: Subscription[] = tenants
  .filter((t) => t.status !== "provisioning")
  .map((t) => ({
    tenantId: t.id, tenant: t.name, plan: t.plan,
    seats: t.users + 5, botLimit: t.plan === "enterprise" ? 20 : t.plan === "growth" ? 8 : 2,
    minutesIncluded: t.plan === "enterprise" ? 200000 : t.plan === "growth" ? 80000 : 10000,
    minutesUsed: t.minutesMonth,
    renewsAt: "2026-08-01",
    status: t.status === "suspended" ? "past_due" : t.status === "trial" ? "trial" : "active",
    mrr: t.mrr,
  }));

export const invoices: Invoice[] = [
  { id: "INV-2026-0611", tenantId: "tn-001", tenant: "Meridian Health Group", period: "Jun 2026", amount: 12400, status: "paid", issuedAt: "2026-06-01" },
  { id: "INV-2026-0612", tenantId: "tn-002", tenant: "Northwind Insurance", period: "Jun 2026", amount: 11240, status: "paid", issuedAt: "2026-06-01" },
  { id: "INV-2026-0613", tenantId: "tn-005", tenant: "Banco Sol", period: "Jun 2026", amount: 9320, status: "open", issuedAt: "2026-06-01" },
  { id: "INV-2026-0614", tenantId: "tn-007", tenant: "Cobalt Airlines", period: "Jun 2026", amount: 10600, status: "paid", issuedAt: "2026-06-01" },
  { id: "INV-2026-0615", tenantId: "tn-008", tenant: "Quill & Co.", period: "Jun 2026", amount: 490, status: "past_due", issuedAt: "2026-06-01" },
  { id: "INV-2026-0616", tenantId: "tn-003", tenant: "Velora Retail", period: "Jun 2026", amount: 4200, status: "paid", issuedAt: "2026-06-01" },
];

/* ---------- Analytics ---------- */

export function tenantAnalytics(days = 30): AnalyticsBundle {
  const labels = daysBack(days);
  const calls = genSeries(21, days, 1450, 500, 6);
  const containedPct = genSeries(22, days, 74, 8, 0.1);
  const csat = genSeries(23, days, 44, 4).map((v) => v / 10);
  const llm = genSeries(24, days, 92, 30, 0.6);
  const tts = genSeries(25, days, 41, 12, 0.2);
  const stt = genSeries(26, days, 35, 10, 0.2);
  const tel = genSeries(27, days, 118, 25, 0.4);

  return {
    kpis: [
      { label: "Total calls", value: calls.reduce((a, b) => a + b, 0).toLocaleString(), delta: 12.4, spark: calls.slice(-14), intent: "up-good" },
      { label: "Containment rate", value: "74.2%", delta: 3.1, spark: containedPct.slice(-14), intent: "up-good" },
      { label: "Escalations", value: "2,318", delta: -8.2, spark: genSeries(28, 14, 80, 24, -1), intent: "down-good" },
      { label: "Avg CSAT", value: "4.4 / 5", delta: 1.9, spark: csat.slice(-14), intent: "up-good" },
      { label: "AI cost", value: "$3,820", delta: 5.7, spark: llm.slice(-14), intent: "down-good" },
      { label: "Avg cost / call", value: "$0.132", delta: -4.1, spark: genSeries(29, 14, 13, 3, -0.1), intent: "down-good" },
    ],
    callsSeries: labels.map((t, i) => ({ t, calls: calls[i], contained: Math.round((calls[i] * containedPct[i]) / 100) })),
    containmentSeries: labels.map((t, i) => ({ t, rate: containedPct[i] })),
    sentimentSplit: [
      { label: "Positive", value: 58 },
      { label: "Neutral", value: 31 },
      { label: "Negative", value: 11 },
    ],
    languageMix: [
      { label: "English (US)", value: 78 },
      { label: "Spanish (US)", value: 19 },
      { label: "Vietnamese", value: 3 },
    ],
    topIntents: [
      { label: "book_appointment", value: 14210, trend: 9 },
      { label: "reschedule_appointment", value: 6120, trend: 4 },
      { label: "clinic_hours", value: 4890, trend: -2 },
      { label: "insurance_question", value: 3940, trend: 18 },
      { label: "cancel_appointment", value: 2710, trend: -6 },
      { label: "talk_to_human", value: 2318, trend: -8 },
    ],
    knowledgeUsage: [
      { label: "Top 60 Patient FAQs", value: 6240 },
      { label: "Zendesk Help Center", value: 5470 },
      { label: "Clinic Locations & Hours", value: 4820 },
      { label: "Appointment Policy Handbook", value: 3110 },
      { label: "HIPAA Guidelines", value: 2130 },
    ],
    costSeries: labels.map((t, i) => ({ t, llm: llm[i], tts: tts[i], stt: stt[i], telephony: tel[i] })),
    recommendations: [
      { id: "rc-1", title: "Re-sync stale insurance knowledge", detail: "“insurance_question” escalations rose 18% while its top source went stale 28 days ago. Re-sync is likely to recover ~120 contained calls/week.", impact: "high", link: "/t/bots/bot-101/knowledge" },
      { id: "rc-2", title: "Add samples to insurance_question intent", detail: "Average confidence is 0.66, below your 0.70 threshold. 4 failing test utterances are ready to import as samples.", impact: "high", link: "/t/bots/bot-101/intents" },
      { id: "rc-3", title: "Fix Billing Balance Lookup API", detail: "The endpoint has failed 100% of calls for 12 hours, forcing escalations on Billing Helpdesk.", impact: "high", link: "/t/bots/bot-102/apis" },
      { id: "rc-4", title: "Enable web channel for Appointment Concierge", detail: "22% of WhatsApp users arrive from the patient portal, where a web widget would deflect calls at lower cost.", impact: "medium", link: "/t/bots/bot-101/channels" },
    ],
  };
}

export function platformAnalytics(days = 30) {
  const labels = daysBack(days);
  const callVol = genSeries(31, days, 6100, 1400, 22);
  const revenue = genSeries(32, days, 1720, 260, 6);
  const aiCost = genSeries(33, days, 495, 90, 2);
  return {
    labels,
    callVol,
    revenue,
    aiCost,
    callsSeries: labels.map((t, i) => ({ t, calls: callVol[i] })) as SeriesPoint[],
    revVsCost: labels.map((t, i) => ({ t, revenue: revenue[i], aiCost: aiCost[i] })) as SeriesPoint[],
    planMix: [
      { label: "Enterprise", value: 21 },
      { label: "Growth", value: 18 },
      { label: "Starter", value: 8 },
    ],
    topTenantsByCalls: tenants
      .filter((t) => t.callsMonth > 0)
      .sort((a, b) => b.callsMonth - a.callsMonth)
      .slice(0, 6)
      .map((t) => ({ label: t.name, value: t.callsMonth })),
    aiCostByProvider: [
      { label: "Anthropic (LLM)", value: 9840 },
      { label: "VectorWorks (embeddings)", value: 1310 },
      { label: "SpeechCore (STT)", value: 3220 },
      { label: "VoxTTS (TTS)", value: 2480 },
    ],
  };
}
