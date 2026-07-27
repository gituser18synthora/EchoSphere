/* Platform Configuration — Super Admin master-data management.
   Industries, Countries, Data Regions, Plans, AI Configuration Profiles, Providers
   (Voice/STT/TTS/LLM/Embedding), Supported Languages and Voice Profiles.

   Every list is server-backed (search / filters / sort / pagination), every
   mutation is permission-enforced and audited on the backend; referenced
   records cannot be permanently deleted — the API returns a clear message and
   the UI offers deactivation instead.

   Add-form drafts live at the page level (per master type), so accidentally
   closing a modal never loses typed data — drafts clear only on successful
   save or an explicit Reset. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useApp } from "@/state/AppContext";
import {
  createMaster, deleteMaster, duplicatePlan, getMasterAudit, getModelLanguages,
  listMaster, listPlanTenants, setMasterStatus, updateMaster, type MasterType,
} from "@/services/api";
import type { ApiRequestError } from "@/services/http";
import type { ModelLanguagesInfo, ProviderSettings, VoiceCapability } from "@/types/domain";
import {
  Button, Callout, ConfirmModal, Drawer, EmptyState, ErrorState, Field, Modal,
  NumberInput, StatusChip, Tabs, Toggle,
} from "@/components/ui";
import { ModelSelect, ProviderSelect, useModelInfos } from "@/components/ProviderModelSelect";
import { ParamFields, reconcileSettings, schemaDefaults } from "@/components/ProviderParams";
import { DataTable, type Column } from "@/components/DataTable";

/* ---------- Generic row & field descriptors ---------- */

type Row = Record<string, unknown> & {
  id: string | number;
  name?: string;
  code?: string;
  status?: string;
  enabled?: boolean;
  usageCount?: number;
  createdAt?: string;
  updatedAt?: string;
  createdBy?: string;
  updatedBy?: string;
};

interface FieldDef {
  key: string;
  label: string;
  type: "text" | "textarea" | "number" | "toggle" | "select" | "country" | "currency" | "provider" | "model" | "voiceProvider" | "datetime";
  options?: { value: string; label: string }[];
  required?: boolean;
  /** Required only while creating; legacy records can still be edited. */
  requiredOnCreate?: boolean;
  hint?: string;
  createOnly?: boolean;
  /** Displayed but never directly editable (the value may be auto-filled). */
  readOnly?: boolean;
  step?: number;
  /** Fields are rendered grouped under their section heading. */
  section?: string;
  /** provider/model fields: which catalog capability they configure. */
  capability?: VoiceCapability;
  /** model fields: the form key holding the selected provider. */
  providerKey?: string;
  /** Initial value in the add form (overrides the per-type defaults). */
  defaultValue?: string;
}

interface TypeSpec {
  mtype: MasterType;
  label: string;
  singular: string;
  columns: Column<Row>[];
  fields: FieldDef[];
  kindFilter?: boolean;
  voiceFilters?: boolean;
}

const text = (key: string, label: string, opts: Partial<FieldDef> = {}): FieldDef =>
  ({ key, label, type: "text", ...opts });
const num = (key: string, label: string, opts: Partial<FieldDef> = {}): FieldDef =>
  ({ key, label, type: "number", ...opts });

/* Supported plan currencies — must stay in sync with the backend whitelist. */
export const CURRENCIES = [
  { code: "USD", symbol: "$", name: "US Dollar" },
  { code: "INR", symbol: "₹", name: "Indian Rupee" },
  { code: "EUR", symbol: "€", name: "Euro" },
  { code: "GBP", symbol: "£", name: "British Pound" },
  { code: "AED", symbol: "د.إ", name: "UAE Dirham" },
] as const;
/* The Plan table's DB default is USD — the existing application configuration. */
const DEFAULT_CURRENCY = "USD";

const currencySymbol = (code: unknown): string =>
  CURRENCIES.find((c) => c.code === String(code ?? ""))?.symbol ?? "";

const nameCol: Column<Row> = {
  key: "name", header: "Name", sortValue: (r) => String(r.name ?? ""),
  render: (r) => <span className="t-strong">{String(r.name ?? "")}</span>,
};
const codeCol: Column<Row> = { key: "code", header: "Code", sortValue: (r) => String(r.code ?? "") };
const statusCol: Column<Row> = {
  key: "status", header: "Status",
  render: (r) => {
    const s = r.status ?? (r.enabled === false ? "inactive" : "active");
    return <StatusChip status={s === "active" ? "active" : s === "archived" ? "archived" : "disabled"} label={String(s)} />;
  },
};
const usageCol: Column<Row> = {
  key: "usageCount", header: "In use", align: "right",
  sortValue: (r) => Number(r.usageCount ?? 0),
  render: (r) => <span>{Number(r.usageCount ?? 0)}</span>,
};
const sortOrderCol: Column<Row> = {
  key: "sortOrder", header: "Sort order", align: "right",
  sortValue: (r) => Number(r.sortOrder ?? 0),
  render: (r) => <span className="t-num">{Number(r.sortOrder ?? 0)}</span>,
};
/* Sections that used to show an "Updated" timestamp now surface the DB-backed
   sort order under the label "Order" instead — active rows sort ahead of
   inactive ones and, within a status group, by this value ascending (the server
   returns that order). Audit timestamps stay in the DB, just not in this table. */
const orderCol: Column<Row> = {
  key: "sortOrder", header: "Order", align: "right",
  sortValue: (r) => Number(r.sortOrder ?? 0),
  render: (r) => <span className="t-num">{Number(r.sortOrder ?? 0)}</span>,
};

/* ---------- Plans: Limits & Flags cell renderers ---------- */

/** The included limits, as compact key-value metrics. Each metric stays on one
    line (numbers never split); the row wraps between metrics when the column is
    narrow. Falls back to the shared em-dash empty state when no limits exist. */
function PlanLimits({ row }: { row: Row }) {
  const metrics: { value: number; unit: string }[] = [
    { key: "botLimit", unit: "bots" },
    { key: "minutesIncluded", unit: "min" },
    { key: "seatsIncluded", unit: "seats" },
  ]
    .filter((m) => row[m.key] !== null && row[m.key] !== undefined)
    .map((m) => ({ value: Number(row[m.key]), unit: m.unit }));

  if (metrics.length === 0) return <span className="t-sub">—</span>;
  return (
    <div className="plan-limits">
      {metrics.map((m) => (
        <span key={m.unit} className="limit"><b>{m.value.toLocaleString()}</b> {m.unit}</span>
      ))}
    </div>
  );
}

/** Plan flags as compact pills that wrap when both are present; em dash when
    none, so the column never reserves blank space. */
function PlanFlags({ row }: { row: Row }) {
  const flags: string[] = [];
  if (row.isRecommended) flags.push("Recommended");
  if (!row.isPublic) flags.push("Hidden");
  if (flags.length === 0) return <span className="t-sub">—</span>;
  return (
    <div className="plan-flags">
      {flags.map((f) => <span key={f} className="tag">{f}</span>)}
    </div>
  );
}

const PROVIDER_KINDS = ["stt", "tts", "llm", "embedding", "voice"] as const;

/* All master-data section definitions. One authoritative implementation —
   the sections are split across two Super Admin pages below:
   - Platform Configuration: product/AI configuration masters.
   - Regional & Currency Settings: geographic + monetary masters. */
const ALL_SPECS: TypeSpec[] = [
  {
    mtype: "industries", label: "Industries", singular: "industry",
    columns: [nameCol, codeCol, { key: "description", header: "Description", render: (r) => <span className="t-sub">{String(r.description ?? "")}</span> }, usageCol, statusCol, orderCol],
    fields: [
      text("code", "Code", { required: true, createOnly: true, hint: "Stable identifier, e.g. banking", section: "Identity" }),
      text("name", "Name", { required: true, section: "Identity" }),
      { key: "description", label: "Description", type: "textarea", section: "Identity" },
      text("icon", "Icon", { section: "Presentation" }),
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "countries", label: "Countries", singular: "country",
    columns: [
      { key: "id", header: "ID", align: "right", sortValue: (r) => Number(r.id),
        render: (r) => <span className="t-num">{Number(r.id)}</span> },
      nameCol,
      { key: "iso2", header: "ISO2", sortValue: (r) => String(r.iso2 ?? "") },
      { key: "iso3", header: "ISO3", sortValue: (r) => String(r.iso3 ?? "") },
      { key: "region", header: "Region", render: () => <span className="tag">Asia</span> },
      usageCol, statusCol, orderCol,
    ],
    fields: [
      text("name", "Country name", { required: true, section: "Identity" }),
      text("iso2", "ISO2", { required: true, createOnly: true, hint: "2-letter ISO code, e.g. IN", section: "Identity" }),
      text("iso3", "ISO3", { required: true, createOnly: true, hint: "3-letter ISO code, e.g. IND", section: "Identity" }),
      { key: "region", label: "Region", type: "select", readOnly: true, section: "Location",
        hint: "The current rollout supports Asia only.", options: [{ value: "Asia", label: "Asia" }] },
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "data-regions", label: "Data Regions", singular: "data region",
    columns: [
      nameCol, codeCol,
      { key: "country", header: "Country / Region", render: (r) => <span>{[r.country, r.region].filter(Boolean).join(" · ") || "—"}</span> },
      {
        key: "infrastructureReady", header: "Infrastructure",
        render: (r) => r.infrastructureReady
          ? <StatusChip status="active" label="Deployed" />
          : <span className="tag" title="Configured operational region — infrastructure is not deployed here yet">Configured only</span>,
      },
      usageCol, statusCol, orderCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true, hint: "e.g. in-mumbai", section: "Identity" }),
      text("name", "Name", { required: true, section: "Identity" }),
      { key: "description", label: "Description", type: "textarea", section: "Identity" },
      { key: "countryId", label: "Country", type: "country", requiredOnCreate: true,
        section: "Location", hint: "Loaded from the active Asia country master." },
      { key: "region", label: "Region", type: "select", readOnly: true, section: "Location",
        hint: "Auto-filled from the selected country.", options: [{ value: "Asia", label: "Asia" }] },
      text("cloudProvider", "Cloud provider", { section: "Location" }),
      text("storageRegion", "Storage region", { section: "Service regions" }),
      text("databaseRegion", "Database region", { section: "Service regions" }),
      text("recordingRegion", "Recording region", { section: "Service regions" }),
      text("transcriptRegion", "Transcript region", { section: "Service regions" }),
      { key: "infrastructureReady", label: "Infrastructure deployed", type: "toggle", section: "Deployment",
        hint: "Only enable when infrastructure actually runs in this region — the UI distinguishes configured vs deployed regions." },
      num("sortOrder", "Order", { section: "Deployment" }),
    ],
  },
  {
    mtype: "plans", label: "Plans", singular: "plan",
    columns: [
      nameCol, codeCol,
      { key: "priceMonthly", header: "Monthly", align: "right", sortValue: (r) => Number(r.priceMonthly ?? 0), render: (r) => <span>{currencySymbol(r.currency)}{Number(r.priceMonthly ?? 0).toLocaleString()} {String(r.currency ?? DEFAULT_CURRENCY)}</span> },
      // Limits get the widest content area; each metric is a nowrap unit so
      // values never split mid-number — the group wraps cleanly between metrics.
      { key: "limits", header: "Limits", width: 280, render: (r) => <PlanLimits row={r} /> },
      // Flags collapse to just the width their pills need (width: 1 lets the
      // auto table layout shrink the column to its content); empty → em dash.
      { key: "isRecommended", header: "Flags", width: 1, render: (r) => <PlanFlags row={r} /> },
      usageCol, statusCol, orderCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true, section: "Basics" }),
      text("name", "Name", { required: true, section: "Basics" }),
      { key: "description", label: "Description", type: "textarea", section: "Basics" },
      num("priceMonthly", "Monthly price", { step: 0.01, section: "Pricing" }),
      num("priceAnnual", "Annual price", { step: 0.01, section: "Pricing" }),
      { key: "currency", label: "Currency", type: "currency", section: "Pricing",
        hint: "Applies to both prices; shown to tenants during onboarding." },
      num("botLimit", "Included bots", { section: "Included limits" }),
      num("minutesIncluded", "Included minutes", { section: "Included limits" }),
      num("seatsIncluded", "Included users", { section: "Included limits" }),
      num("kbLimit", "Included knowledge bases", { section: "Included limits" }),
      num("storageGbIncluded", "Included storage (GB)", { section: "Included limits" }),
      num("languagesIncluded", "Included languages", { section: "Included limits" }),
      num("concurrentCallLimit", "Concurrent call limit", { section: "Usage caps", hint: "0 = unlimited" }),
      num("monthlyCallLimit", "Monthly call limit", { section: "Usage caps", hint: "0 = unlimited" }),
      num("monthlyTokenLimit", "Monthly token limit", { section: "Usage caps", hint: "0 = unlimited" }),
      num("monthlyEmbeddingLimit", "Monthly embedding limit", { section: "Usage caps", hint: "0 = unlimited" }),
      num("recordingRetentionDays", "Recording retention (days)", { section: "Data retention" }),
      num("transcriptRetentionDays", "Transcript retention (days)", { section: "Data retention" }),
      num("analyticsRetentionDays", "Analytics retention (days)", { section: "Data retention" }),
      { key: "isPublic", label: "Visible in onboarding", type: "toggle", section: "Presentation" },
      { key: "isRecommended", label: "Recommended", type: "toggle", section: "Presentation" },
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "ai-profiles", label: "AI Profiles", singular: "AI configuration profile",
    columns: [
      nameCol, codeCol,
      { key: "models", header: "Stack", render: (r) => <span className="t-micro">{[r.llmProvider && `LLM ${r.llmProvider}/${r.llmModel ?? ""}`, r.sttProvider && `STT ${r.sttProvider}`, r.ttsProvider && `TTS ${r.ttsProvider}`].filter(Boolean).join(" · ") || "Custom"}</span> },
      { key: "costCategory", header: "Cost", render: (r) => <span className="tag">{String(r.costCategory ?? "")}</span> },
      usageCol, statusCol, orderCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true, section: "Basics" }),
      text("name", "Name", { required: true, section: "Basics" }),
      { key: "description", label: "Description", type: "textarea", section: "Basics" },
      { key: "sttProvider", label: "STT provider", type: "provider", capability: "stt", section: "Speech to text" },
      { key: "sttModel", label: "STT model", type: "model", capability: "stt", providerKey: "sttProvider", section: "Speech to text" },
      { key: "llmProvider", label: "LLM provider", type: "provider", capability: "llm", section: "Language model" },
      { key: "llmModel", label: "LLM model", type: "model", capability: "llm", providerKey: "llmProvider", section: "Language model" },
      { key: "ttsProvider", label: "TTS provider", type: "provider", capability: "tts", section: "Text to speech" },
      { key: "ttsModel", label: "TTS model", type: "model", capability: "tts", providerKey: "ttsProvider", section: "Text to speech" },
      text("defaultVoice", "Default voice", { section: "Text to speech" }),
      { key: "embeddingProvider", label: "Embedding provider", type: "provider", capability: "embedding", section: "Retrieval" },
      { key: "embeddingModel", label: "Embedding model", type: "model", capability: "embedding", providerKey: "embeddingProvider", section: "Retrieval" },
      num("embeddingDimension", "Embedding dimension", { section: "Retrieval" }),
      text("rerankingModel", "Reranking model", { section: "Retrieval" }),
      num("retrievalTopK", "Retrieval top-K", { section: "Retrieval" }),
      num("retrievalThreshold", "Retrieval threshold", { step: 0.05, section: "Retrieval" }),
      num("temperature", "Temperature", { step: 0.1, section: "Generation" }),
      num("maxOutputTokens", "Max output tokens", { section: "Generation" }),
      num("responseTimeoutMs", "Response timeout (ms)", { section: "Generation" }),
      { key: "costCategory", label: "Cost category", type: "select", section: "Presentation", options: [
        { value: "low", label: "Low" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" },
      ]},
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "providers", label: "Providers", singular: "provider", kindFilter: true,
    columns: [
      nameCol, codeCol,
      { key: "kind", header: "Kind", render: (r) => <span className="tag">{String(r.kind ?? "").toUpperCase()}</span> },
      { key: "requiresApiKey", header: "API key", render: (r) => <span className="t-micro">{r.requiresApiKey ? String(r.secretRef ?? "required") : "not required"}</span> },
      usageCol, statusCol, orderCol,
    ],
    fields: [
      { key: "kind", label: "Kind", type: "select", required: true, createOnly: true, section: "Identity",
        options: PROVIDER_KINDS.map((k) => ({ value: k, label: k.toUpperCase() })) },
      text("code", "Code", { required: true, createOnly: true, section: "Identity" }),
      text("name", "Name", { required: true, section: "Identity" }),
      { key: "description", label: "Description", type: "textarea", section: "Identity" },
      text("website", "Website", { section: "Access" }),
      { key: "requiresApiKey", label: "Requires API key", type: "toggle", section: "Access" },
      text("secretRef", "Secret reference", { hint: "env:VAR_NAME reference only — raw keys are never stored here.", section: "Access" }),
      num("sortOrder", "Sort order", { section: "Access" }),
    ],
  },
  {
    mtype: "languages", label: "Languages", singular: "language",
    columns: [
      {
        key: "name", header: "Language", sortValue: (r) => String(r.name ?? ""),
        render: (r) => (
          <span className="row gap-8" style={{ alignItems: "baseline" }}>
            <span className="t-strong">{String(r.name ?? "")}</span>
            {Boolean(r.nativeName) && (
              <span className="t-sub" dir={r.direction === "rtl" ? "rtl" : "ltr"}>{String(r.nativeName)}</span>
            )}
            {Boolean(r.isDefault) && <span className="tag">Default</span>}
          </span>
        ),
      },
      codeCol,
      { key: "script", header: "Script", render: (r) => <span className="t-micro">{String(r.script ?? "—")}{r.direction === "rtl" ? " · RTL" : ""}</span> },
      {
        key: "providerSupport", header: "Provider support",
        render: (r) => {
          const ps = (r.providerSupport ?? {}) as { stt?: string[]; tts?: string[] };
          return <span className="t-micro">STT {ps.stt?.length ?? 0} · TTS {ps.tts?.length ?? 0}</span>;
        },
      },
      usageCol, statusCol, sortOrderCol,
    ],
    fields: [
      text("code", "Locale code", { required: true, createOnly: true, hint: "BCP-47, e.g. hi-IN", section: "Identity" }),
      text("name", "Display name", { required: true, section: "Identity" }),
      text("nativeName", "Native name", { section: "Identity" }),
      text("isoCode", "ISO 639 code", { hint: "e.g. hi", section: "Script" }),
      text("script", "Script", { hint: "e.g. Devanagari", section: "Script" }),
      { key: "direction", label: "Text direction", type: "select", section: "Script", options: [
        { value: "ltr", label: "Left to right" }, { value: "rtl", label: "Right to left" },
      ]},
      { key: "isDefault", label: "Platform default", type: "toggle", section: "Presentation" },
      num("sortOrder", "Sort order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "voices", label: "Voices", singular: "voice", voiceFilters: true,
    columns: [
      { ...nameCol, render: (r) => <span className="row gap-8"><span className="t-strong">{String(r.name ?? "")}</span>{Boolean(r.isDefault) && <span className="tag">Default</span>}</span> },
      { key: "provider", header: "Provider", render: (r) => <span>{String(r.provider ?? "platform")}</span> },
      { key: "languages", header: "Languages", render: (r) => <span className="t-micro">{((r.languages as string[]) ?? []).join(", ") || String(r.locale ?? "")}</span> },
      { key: "gender", header: "Gender", render: (r) => <span>{String(r.gender ?? "")}</span> },
      { key: "speakingRate", header: "Rate", align: "right", render: (r) => <span>{Number(r.speakingRate ?? 1).toFixed(2)}×</span> },
      usageCol, statusCol, sortOrderCol,
    ],
    // Voices use the dedicated provider-aware VoiceEditor (fields depend on
    // the selected TTS provider's catalog schema), not the generic editor.
    fields: [],
  },
  {
    mtype: "currencies", label: "Currencies", singular: "currency",
    columns: [
      { ...nameCol, header: "Currency", render: (r) => (
        <span className="row gap-8" style={{ alignItems: "baseline" }}>
          <span className="t-strong">{String(r.name ?? "")}</span>
          {Boolean(r.isBase) && <span className="tag" title="Platform base currency — all provider costs are stored in it">Base</span>}
        </span>
      ) },
      codeCol,
      { key: "symbol", header: "Symbol", render: (r) => <span className="t-strong">{String(r.symbol ?? "")}</span> },
      usageCol, statusCol, orderCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true, hint: "ISO 4217, e.g. INR", section: "Identity" }),
      text("name", "Name", { required: true, section: "Identity" }),
      text("symbol", "Symbol", { required: true, hint: "e.g. ₹", section: "Identity" }),
      num("decimalPlaces", "Decimal places", { hint: "0–4; most currencies use 2.", section: "Presentation", defaultValue: "2" }),
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "exchange-rates", label: "Exchange Rates", singular: "exchange rate",
    columns: [
      { key: "baseCode", header: "From", sortValue: (r) => String(r.baseCode ?? ""),
        render: (r) => <span className="t-strong">{String(r.baseCode ?? "")}</span> },
      { key: "targetCode", header: "To", sortValue: (r) => String(r.targetCode ?? ""),
        render: (r) => <span className="t-strong">{String(r.targetCode ?? "")}</span> },
      { key: "rate", header: "Rate", align: "right", sortValue: (r) => Number(r.rate ?? 0),
        render: (r) => <span className="t-num">{Number(r.rate ?? 0).toLocaleString(undefined, { maximumFractionDigits: 6 })}</span> },
      { key: "effectiveFrom", header: "Effective From", sortValue: (r) => String(r.effectiveFrom ?? ""),
        render: (r) => <span className="t-micro">{r.effectiveFrom ? new Date(String(r.effectiveFrom)).toLocaleString() : "—"}</span> },
      statusCol, orderCol,
    ],
    fields: [
      { key: "baseCode", label: "Base currency", type: "currency", createOnly: true, section: "Pair",
        hint: "Rates are configured from the platform base currency (USD)." },
      { key: "targetCode", label: "Target currency", type: "currency", createOnly: true,
        requiredOnCreate: true, section: "Pair", defaultValue: "INR" },
      num("rate", "Exchange rate", { required: true, step: 0.0001, section: "Rate",
        hint: "1 unit of the base currency in the target currency, e.g. 86.50." }),
      { key: "effectiveFrom", label: "Effective from", type: "datetime", section: "Rate",
        hint: "UTC. Leave empty to take effect immediately." },
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
  {
    mtype: "provider-pricing", label: "Provider Pricing", singular: "provider price",
    columns: [
      { key: "providerCode", header: "Provider", sortValue: (r) => String(r.providerCode ?? ""),
        render: (r) => <span className="t-strong">{String(r.providerCode ?? "")}</span> },
      { key: "modelCode", header: "Model", render: (r) => <span className="t-micro">{String(r.modelCode || "—")}</span> },
      { key: "capability", header: "Capability", render: (r) => <span className="tag">{String(r.capability ?? "").toUpperCase()}</span> },
      { key: "component", header: "Component", render: (r) => <span className="t-micro">{String(r.component ?? "")}</span> },
      { key: "unitPrice", header: "Price", align: "right", sortValue: (r) => Number(r.unitPrice ?? 0),
        render: (r) => <span className="t-num">{currencySymbol(r.currencyCode) || String(r.currencyCode ?? "")}{Number(r.unitPrice ?? 0).toLocaleString(undefined, { maximumFractionDigits: 10 })} <span className="t-micro">{String(r.unit ?? "").replaceAll("_", " ")}</span></span> },
      statusCol, orderCol,
    ],
    fields: [
      { key: "capability", label: "Capability", type: "select", required: true, createOnly: true, section: "Scope",
        options: ["llm", "embedding", "stt", "tts", "telephony"].map((c) => ({ value: c, label: c.toUpperCase() })) },
      text("providerCode", "Provider code", { required: true, createOnly: true, section: "Scope",
        hint: "Catalog code, e.g. openai, sarvam, elevenlabs." }),
      text("modelCode", "Model code", { createOnly: true, section: "Scope",
        hint: "Provider wire code, e.g. gpt-4o-mini or bulbul:v3. Leave empty for flat prices." }),
      { key: "component", label: "Component", type: "select", required: true, createOnly: true, section: "Billing unit",
        options: [
          { value: "input_tokens", label: "Input tokens" },
          { value: "output_tokens", label: "Output tokens" },
          { value: "cached_input_tokens", label: "Cached input tokens" },
          { value: "tokens", label: "Tokens (blended)" },
          { value: "characters", label: "Characters" },
          { value: "audio_seconds", label: "Audio duration" },
          { value: "call_seconds", label: "Call duration" },
          { value: "requests", label: "Requests" },
        ] },
      { key: "unit", label: "Pricing unit", type: "select", required: true, section: "Billing unit",
        options: [
          { value: "per_token", label: "Per token" },
          { value: "per_1k_tokens", label: "Per 1K tokens" },
          { value: "per_1m_tokens", label: "Per 1M tokens" },
          { value: "per_character", label: "Per character" },
          { value: "per_1k_characters", label: "Per 1K characters" },
          { value: "per_second", label: "Per second" },
          { value: "per_minute", label: "Per minute" },
          { value: "per_request", label: "Per request" },
        ] },
      num("unitPrice", "Unit price", { required: true, step: 0.000001, section: "Billing unit",
        hint: "Native provider price for one pricing unit." }),
      { key: "currencyCode", label: "Price currency", type: "currency", readOnly: true, section: "Billing unit",
        hint: "Provider pricing is normalized to USD; display currencies convert via exchange rates." },
      { key: "effectiveFrom", label: "Effective from", type: "datetime", section: "Billing unit",
        hint: "UTC. Leave empty to take effect immediately." },
      num("sortOrder", "Order", { section: "Presentation" }),
    ],
  },
];

/* Geographic + monetary masters live under Regional & Currency Settings;
   everything else stays under Platform Configuration. */
const REGIONAL_MTYPES: readonly MasterType[] = [
  "countries", "data-regions", "currencies", "exchange-rates",
];
export const REGIONAL_SPECS: TypeSpec[] = REGIONAL_MTYPES.map(
  (mtype) => ALL_SPECS.find((s) => s.mtype === mtype)!,
);
export const PLATFORM_SPECS: TypeSpec[] = ALL_SPECS.filter(
  (s) => !REGIONAL_MTYPES.includes(s.mtype),
);

/* ---------- Form state helpers ---------- */

function buildInitialForm(spec: TypeSpec): Record<string, unknown> {
  if (spec.mtype === "voices") return buildVoiceForm(null);
  const initial: Record<string, unknown> = {};
  for (const f of spec.fields) {
    initial[f.key] = f.defaultValue !== undefined ? f.defaultValue
      : f.type === "toggle" ? false
      : f.type === "currency" ? DEFAULT_CURRENCY
      : f.key === "region" && (spec.mtype === "countries" || spec.mtype === "data-regions") ? "Asia"
      : "";
  }
  return initial;
}

function buildFormFromRow(spec: TypeSpec, row: Row): Record<string, unknown> {
  if (spec.mtype === "voices") return buildVoiceForm(row);
  const form: Record<string, unknown> = {};
  for (const f of spec.fields) {
    const value = row[f.key];
    form[f.key] = f.type === "toggle" ? Boolean(value)
      : f.type === "currency" ? String(value ?? DEFAULT_CURRENCY)
      // datetime-local inputs take "YYYY-MM-DDTHH:mm".
      : f.type === "datetime" ? String(value ?? "").slice(0, 16)
      : value ?? "";
  }
  return form;
}

/** Voice form state — the provider decides which settings exist; the single
    selected model is stored as `modelCodes: [model]` on the API. */
function buildVoiceForm(row: Row | null): Record<string, unknown> {
  return {
    name: String(row?.name ?? ""),
    gender: String(row?.gender ?? ""),
    description: String(row?.description ?? ""),
    provider: String(row?.provider ?? ""),
    providerVoiceId: String(row?.providerVoiceId ?? ""),
    model: (row?.modelCodes as string[] | undefined)?.[0] ?? "",
    locale: String(row?.locale ?? ""),
    sampleText: String(row?.sample ?? ""),
    premium: Boolean(row?.premium),
    isDefault: Boolean(row?.isDefault),
    sortOrder: row?.sortOrder ?? "",
    providerSettings: (row?.providerSettings as ProviderSettings | undefined) ?? {},
  };
}

/* ---------- Editor modal ---------- */

/** Multi-key form updates are delivered as one patch so no key is lost when a
    change cascades (e.g. a provider change also clearing its model). */
type FormPatch = Record<string, unknown>;

interface CountryOption {
  id: number;
  name: string;
  iso2: string;
  iso3: string;
  region: string;
}

function useAsiaCountries(enabled: boolean) {
  const [countries, setCountries] = useState<CountryOption[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setLoading(true);
    setError(null);
    listMaster<Row>("countries", {
      includeInactive: false, pageSize: 100, sortBy: "name", sortDir: "asc",
    }).then((result) => {
      if (!alive) return;
      setCountries(result.items.map((country) => ({
        id: Number(country.id),
        name: String(country.name ?? ""),
        iso2: String(country.iso2 ?? ""),
        iso3: String(country.iso3 ?? ""),
        region: String(country.region ?? "Asia"),
      })));
      setLoading(false);
    }).catch((e: Error) => {
      if (!alive) return;
      setError(e.message || "Countries could not be loaded.");
      setLoading(false);
    });
    return () => { alive = false; };
  }, [enabled]);
  return { countries, loading, error };
}

function MasterEditor({ spec, row, form, onChange, onReset, onClose, onSaved }: {
  spec: TypeSpec;
  row: Row | null; // null = add mode
  form: Record<string, unknown>;
  onChange: (patch: FormPatch) => void;
  onReset: () => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const countryCatalog = useAsiaCountries(spec.mtype === "data-regions");

  const set = (key: string, value: unknown, extra: FormPatch = {}) => {
    const patch: FormPatch = { ...extra, [key]: value };
    // A provider change invalidates its dependent model selection.
    for (const f of spec.fields) {
      if (f.type === "model" && f.providerKey === key && form[f.key]) patch[f.key] = "";
    }
    onChange(patch);
    // Stale API/client validation for this field is cleared as soon as it changes.
    setFieldErrors((errs) => {
      if (!(key in errs)) return errs;
      const next = { ...errs };
      delete next[key];
      return next;
    });
    if (error) setError(null);
  };

  const save = async () => {
    if (busy) return;
    const clientErrors: Record<string, string> = {};
    for (const f of spec.fields) {
      const required = f.required || (!row && f.requiredOnCreate);
      if (required && (row ? !f.createOnly : true) && !String(form[f.key] ?? "").trim()) {
        clientErrors[f.key] = `${f.label} is required.`;
      }
    }
    if (Object.keys(clientErrors).length) {
      setFieldErrors(clientErrors);
      setError("Fix the highlighted fields and try again.");
      return;
    }
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const payload: Record<string, unknown> = {};
      for (const f of spec.fields) {
        if (row && f.createOnly) continue;
        let value = form[f.key];
        if (f.type === "number") value = value === "" || value === null ? undefined : Number(value);
        // Provider/model fields submit explicitly on edit so an emptied model
        // clears the stored value instead of being silently skipped.
        const clearable = f.type === "provider" || f.type === "model";
        if ((value === "" && !(row && clearable)) || value === undefined) continue;
        payload[f.key] = value;
      }
      if (row) await updateMaster(spec.mtype, row.id, payload);
      else await createMaster(spec.mtype, payload);
      toast(row ? `${spec.singular} updated` : `${spec.singular} created`);
      onSaved();
      onClose();
    } catch (e) {
      const api = e as ApiRequestError;
      if (api.fieldErrors) setFieldErrors(api.fieldErrors);
      setError(api instanceof Error ? api.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  /* Group fields by section, preserving spec order. */
  const sections = useMemo(() => {
    const grouped: { title: string; fields: FieldDef[] }[] = [];
    for (const f of spec.fields) {
      const title = f.section ?? "";
      const last = grouped[grouped.length - 1];
      if (last && last.title === title) last.fields.push(f);
      else grouped.push({ title, fields: [f] });
    }
    return grouped;
  }, [spec]);

  const renderField = (f: FieldDef) => {
    const locked = Boolean(row && f.createOnly) || Boolean(f.readOnly);
    const common = { "aria-label": f.label, disabled: locked };
    if (f.type === "toggle") {
      return <Toggle checked={Boolean(form[f.key])} onChange={(v) => set(f.key, v)} label={f.label} />;
    }
    if (f.type === "currency") {
      return (
        <select className="select" value={String(form[f.key] ?? DEFAULT_CURRENCY)} {...common}
          onChange={(e) => set(f.key, e.target.value)}>
          {CURRENCIES.map((c) => (
            <option key={c.code} value={c.code}>{c.code} · {c.symbol} {c.name}</option>
          ))}
        </select>
      );
    }
    if (f.type === "country") {
      const value = String(form[f.key] ?? "");
      const known = countryCatalog.countries.some((country) => String(country.id) === value);
      return (
        <select className="select" value={value} {...common}
          onChange={(e) => {
            const selected = countryCatalog.countries.find(
              (country) => String(country.id) === e.target.value,
            );
            set(f.key, selected?.id ?? "", selected ? { region: selected.region } : {});
          }}>
          <option value="">
            {countryCatalog.loading ? "Loading countries…"
              : countryCatalog.error ? "Countries unavailable"
              : "Select country"}
          </option>
          {value && !known && <option value={value}>{String(row?.country ?? value)} (legacy)</option>}
          {countryCatalog.countries.map((country) => (
            <option key={country.id} value={country.id}>
              {country.name} ({country.iso2} / {country.iso3})
            </option>
          ))}
        </select>
      );
    }
    if (f.type === "provider" && f.capability) {
      return (
        <ProviderSelect capability={f.capability} value={String(form[f.key] ?? "")}
          label={f.label} disabled={locked} onChange={(code) => set(f.key, code)} />
      );
    }
    if (f.type === "voiceProvider") {
      return (
        <VoiceProviderSelect value={String(form[f.key] ?? "")} label={f.label}
          disabled={locked} onChange={(code) => set(f.key, code)} />
      );
    }
    if (f.type === "model" && f.capability && f.providerKey) {
      return (
        <ModelSelect capability={f.capability} provider={String(form[f.providerKey] ?? "")}
          value={String(form[f.key] ?? "")} label={f.label} disabled={locked}
          onChange={(code) => set(f.key, code)} />
      );
    }
    if (f.type === "select") {
      return (
        <select className="select" value={String(form[f.key] ?? "")} {...common}
          onChange={(e) => set(f.key, e.target.value)}>
          <option value="">—</option>
          {(f.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      );
    }
    if (f.type === "textarea") {
      return (
        <textarea className="textarea" rows={2} value={String(form[f.key] ?? "")} {...common}
          onChange={(e) => set(f.key, e.target.value)} />
      );
    }
    if (f.type === "datetime") {
      return (
        <input className="input" type="datetime-local" value={String(form[f.key] ?? "")} {...common}
          aria-invalid={Boolean(fieldErrors[f.key]) || undefined}
          onChange={(e) => set(f.key, e.target.value)} />
      );
    }
    if (f.type === "number") {
      return (
        <NumberInput value={String(form[f.key] ?? "")} step={f.step} min={0} {...common}
          invalid={Boolean(fieldErrors[f.key])} onChange={(v) => set(f.key, v)} />
      );
    }
    return (
      <input className="input" type="text" value={String(form[f.key] ?? "")} {...common}
        aria-invalid={Boolean(fieldErrors[f.key]) || undefined}
        onChange={(e) => set(f.key, e.target.value)} />
    );
  };

  return (
    <Modal open onClose={onClose} wide
      title={row ? `Edit ${spec.singular}` : `Add ${spec.singular}`}
      sub={row ? undefined : "Closing this dialog keeps your draft — it clears on Create or Reset."}
      footer={
        <>
          <Button onClick={onReset} icon="undo" title="Restore the initial values">Reset</Button>
          <div className="grow" />
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={busy} onClick={() => void save()}>
            {row ? "Save changes" : "Create"}
          </Button>
        </>
      }>
      <div className="col gap-16">
        {error && <Callout tone="critical">{error}</Callout>}
        {sections.map((s, i) => (
          <section key={s.title || i} className="col gap-12">
            {s.title && (
              <h3 className="t-label" style={{ margin: 0, paddingBottom: 6, borderBottom: "1px solid var(--hairline)" }}>
                {s.title}
              </h3>
            )}
            <div className="grid grid-2">
              {s.fields.map((f) => (
                <Field key={f.key} label={f.label} required={f.required || (!row && f.requiredOnCreate)}
                  error={fieldErrors[f.key]} hint={f.hint}
                  plain={f.type === "toggle"}>
                  {renderField(f)}
                </Field>
              ))}
            </div>
          </section>
        ))}
      </div>
    </Modal>
  );
}

/* ---------- Voice editor (provider-specific) ----------
   The TTS provider decides everything below it: which models exist, which
   languages the model supports, and which synthesis settings are legal — all
   read from the DB provider catalog (params_schema per model), never
   hardcoded. The backend re-validates the same catalog on save. */

const VOICE_ID_META: Record<string, { label: string; hint: string }> = {
  elevenlabs: { label: "ElevenLabs voice ID", hint: "Voice ID from the ElevenLabs voice library, e.g. f1abxvIEijusskcPWE5x." },
  sarvam: { label: "Speaker code", hint: "Lowercase Sarvam speaker code, e.g. shubh or priya." },
};

function VoiceEditor({ spec, row, form, onChange, onReset, onClose, onSaved }: {
  spec: TypeSpec;
  row: Row | null; // null = add mode
  form: Record<string, unknown>;
  onChange: (patch: FormPatch) => void;
  onReset: () => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useApp();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [pendingProvider, setPendingProvider] = useState<string | null>(null);
  const [switchNote, setSwitchNote] = useState<string | null>(null);

  const provider = String(form.provider ?? "");
  const model = String(form.model ?? "");
  const settings = (form.providerSettings ?? {}) as ProviderSettings;

  const { models, loading: modelsLoading } = useModelInfos("tts", provider || null);
  const modelInfo = (models ?? []).find((m) => m.code === model);
  const schema = modelInfo?.paramsSchema;
  const hasCatalogModels = (models ?? []).length > 0;

  /* Platform languages supported by the selected model. */
  const [langInfo, setLangInfo] = useState<ModelLanguagesInfo | null>(null);
  useEffect(() => {
    let alive = true;
    setLangInfo(null);
    if (provider && model) {
      getModelLanguages("tts", provider, model)
        .then((info) => { if (alive) setLangInfo(info); })
        .catch(() => { /* backend still validates; free-text locale stays available */ });
    }
    return () => { alive = false; };
  }, [provider, model]);

  const set = (patch: FormPatch) => {
    onChange(patch);
    setFieldErrors((errs) => {
      const next = { ...errs };
      for (const key of Object.keys(patch)) delete next[key];
      delete next.modelCodes; // model changes invalidate stale model errors
      delete next.providerSettings;
      return Object.keys(next).length === Object.keys(errs).length ? errs : next;
    });
    if (error) setError(null);
  };

  /* Provider-specific data worth a confirmation before it is discarded. */
  const hasSubstantialProviderData = () => {
    if (String(form.providerVoiceId ?? "").trim()) return true;
    if (!schema) return Object.keys(settings).length > 0;
    return JSON.stringify({ ...schemaDefaults(schema), ...settings }) !== JSON.stringify(schemaDefaults(schema));
  };

  const applyProvider = (next: string) => {
    set({ provider: next, model: "", providerVoiceId: "", locale: "", providerSettings: {} });
    setSwitchNote(next
      ? `Provider changed — model, voice ID, language and provider settings were reset for ${next}. Name, gender and description were kept.`
      : null);
  };

  const requestProvider = (next: string) => {
    if (next === provider) return;
    if (provider && hasSubstantialProviderData()) setPendingProvider(next);
    else applyProvider(next);
  };

  const applyModel = (next: string) => {
    const nextSchema = (models ?? []).find((m) => m.code === next)?.paramsSchema;
    set({ model: next, providerSettings: next ? reconcileSettings(nextSchema, settings) : {} });
  };

  const save = async () => {
    if (busy) return;
    const clientErrors: Record<string, string> = {};
    if (!String(form.name ?? "").trim()) clientErrors.name = "Name is required.";
    if (!provider) clientErrors.provider = "Select the TTS provider first.";
    if (provider && hasCatalogModels) {
      if (!model) clientErrors.model = "Select the provider model.";
      if (!String(form.providerVoiceId ?? "").trim()) {
        clientErrors.providerVoiceId = `${VOICE_ID_META[provider]?.label ?? "Provider voice ID"} is required.`;
      }
    }
    if (Object.keys(clientErrors).length) {
      setFieldErrors(clientErrors);
      setError("Fix the highlighted fields and try again.");
      return;
    }
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const payload: Record<string, unknown> = {
        name: String(form.name).trim(),
        provider,
        modelCodes: model ? [model] : [],
        providerSettings: settings,
        premium: Boolean(form.premium),
        isDefault: Boolean(form.isDefault),
      };
      // Clearable strings are sent explicitly on edit so provider switches
      // wipe stale values; optional fields are skipped when empty on create.
      for (const key of ["gender", "description", "providerVoiceId", "locale", "sampleText"]) {
        const value = String(form[key] ?? "");
        if (value !== "" || row) payload[key] = value;
      }
      if (form.sortOrder !== "" && form.sortOrder !== null && form.sortOrder !== undefined) {
        payload.sortOrder = Number(form.sortOrder);
      }
      if (row) await updateMaster(spec.mtype, row.id, payload);
      else await createMaster(spec.mtype, payload);
      toast(row ? "Voice updated" : "Voice created");
      onSaved();
      onClose();
    } catch (e) {
      const api = e as ApiRequestError;
      if (api.fieldErrors) {
        // Backend reports model errors as modelCodes — surface them on the model field.
        const mapped: Record<string, string> = { ...api.fieldErrors };
        if (mapped.modelCodes) mapped.model = mapped.modelCodes;
        setFieldErrors(mapped);
      }
      setError(api instanceof Error ? api.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  const voiceIdMeta = VOICE_ID_META[provider] ?? {
    label: "Provider voice ID",
    hint: "Identifier of this voice on the provider's side, if any.",
  };
  const localeOptions = langInfo?.languages ?? [];
  const localeKnown = localeOptions.some((l) => l.code === String(form.locale ?? ""));

  return (
    <Modal open onClose={onClose} wide
      title={row ? "Edit voice" : "Add voice"}
      sub={row ? undefined : "Closing this dialog keeps your draft — it clears on Create or Reset."}
      footer={
        <>
          <Button onClick={onReset} icon="undo" title="Restore the initial values">Reset</Button>
          <div className="grow" />
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} disabled={busy} onClick={() => void save()}>
            {row ? "Save changes" : "Create"}
          </Button>
        </>
      }>
      <div className="col gap-16">
        {error && <Callout tone="critical">{error}</Callout>}
        {switchNote && <Callout tone="info" title="Provider changed">{switchNote}</Callout>}

        <section className="col gap-12">
          <h3 className="t-label" style={{ margin: 0, paddingBottom: 6, borderBottom: "1px solid var(--hairline)" }}>
            Provider & model
          </h3>
          <div className="grid grid-2">
            <Field label="TTS provider" required error={fieldErrors.provider}
              hint={provider ? undefined : "Select the provider first — it decides the models, languages and settings below."}>
              <VoiceProviderSelect value={provider} label="TTS provider" onChange={requestProvider} />
            </Field>
            <Field label="Model" required={hasCatalogModels} error={fieldErrors.model}>
              <ModelSelect capability="tts" provider={provider} value={model} label="Model"
                onChange={applyModel} />
            </Field>
            <Field label={voiceIdMeta.label} required={hasCatalogModels}
              error={fieldErrors.providerVoiceId} hint={voiceIdMeta.hint}>
              <input className="input" value={String(form.providerVoiceId ?? "")}
                aria-label={voiceIdMeta.label}
                aria-invalid={Boolean(fieldErrors.providerVoiceId) || undefined}
                disabled={!provider}
                onChange={(e) => set({ providerVoiceId: e.target.value })} />
            </Field>
            <Field label="Language" error={fieldErrors.locale}
              hint={model ? "Languages supported by the selected model." : "Select a model to list its supported languages."}>
              {localeOptions.length > 0 ? (
                <select className="select" aria-label="Language" value={String(form.locale ?? "")}
                  onChange={(e) => set({ locale: e.target.value })}>
                  <option value="">—</option>
                  {!localeKnown && form.locale ? <option value={String(form.locale)}>{String(form.locale)} (not supported)</option> : null}
                  {localeOptions.map((l) => <option key={l.code} value={l.code}>{l.name} ({l.code})</option>)}
                </select>
              ) : (
                <input className="input" aria-label="Language" placeholder="e.g. hi-IN"
                  value={String(form.locale ?? "")} disabled={!provider}
                  onChange={(e) => set({ locale: e.target.value })} />
              )}
            </Field>
          </div>
        </section>

        <section className="col gap-12">
          <h3 className="t-label" style={{ margin: 0, paddingBottom: 6, borderBottom: "1px solid var(--hairline)" }}>
            Identity
          </h3>
          <div className="grid grid-2">
            <Field label="Display name" required error={fieldErrors.name}>
              <input className="input" value={String(form.name ?? "")} aria-label="Display name"
                aria-invalid={Boolean(fieldErrors.name) || undefined}
                onChange={(e) => set({ name: e.target.value })} />
            </Field>
            <Field label="Gender / style">
              <select className="select" aria-label="Gender / style" value={String(form.gender ?? "")}
                onChange={(e) => set({ gender: e.target.value })}>
                <option value="">—</option>
                <option value="female">Female</option>
                <option value="male">Male</option>
                <option value="neutral">Neutral</option>
              </select>
            </Field>
            <Field label="Description">
              <textarea className="textarea" rows={2} aria-label="Description"
                value={String(form.description ?? "")}
                onChange={(e) => set({ description: e.target.value })} />
            </Field>
            <Field label="Sample sentence" hint="Used by the voice preview.">
              <textarea className="textarea" rows={2} aria-label="Sample sentence"
                value={String(form.sampleText ?? "")}
                onChange={(e) => set({ sampleText: e.target.value })} />
            </Field>
          </div>
        </section>

        <section className="col gap-12">
          <h3 className="t-label" style={{ margin: 0, paddingBottom: 6, borderBottom: "1px solid var(--hairline)" }}>
            Provider settings{provider ? ` — ${provider}` : ""}
          </h3>
          {fieldErrors.providerSettings && <Callout tone="critical">{fieldErrors.providerSettings}</Callout>}
          {!provider ? (
            <p className="t-sub" style={{ margin: 0 }}>Select a TTS provider to see its synthesis settings.</p>
          ) : modelsLoading ? (
            <p className="t-sub" style={{ margin: 0 }}>Loading provider configuration…</p>
          ) : !hasCatalogModels ? (
            <p className="t-sub" style={{ margin: 0 }}>
              This provider has no configurable synthesis settings — voices are used as-is.
            </p>
          ) : !model ? (
            <p className="t-sub" style={{ margin: 0 }}>Select a model to configure its provider-specific settings.</p>
          ) : (
            <ParamFields schema={schema} values={settings}
              onChange={(next) => set({ providerSettings: next })} />
          )}
        </section>

        <section className="col gap-12">
          <h3 className="t-label" style={{ margin: 0, paddingBottom: 6, borderBottom: "1px solid var(--hairline)" }}>
            Presentation
          </h3>
          <div className="grid grid-2">
            <Field label="Premium" plain>
              <Toggle checked={Boolean(form.premium)} onChange={(v) => set({ premium: v })} label="Premium" />
            </Field>
            <Field label="Platform default" plain>
              <Toggle checked={Boolean(form.isDefault)} onChange={(v) => set({ isDefault: v })} label="Platform default" />
            </Field>
            <Field label="Sort order" error={fieldErrors.sortOrder}>
              <NumberInput value={String(form.sortOrder ?? "")} aria-label="Sort order"
                invalid={Boolean(fieldErrors.sortOrder)}
                onChange={(v) => set({ sortOrder: v })} />
            </Field>
          </div>
        </section>
      </div>

      {pendingProvider !== null && (
        <ConfirmModal
          open
          danger
          title="Switch provider?"
          body="Switching the provider discards the voice ID, model, language and provider-specific settings you entered. Name, gender and description are kept."
          confirmLabel="Switch provider"
          onConfirm={() => { applyProvider(pendingProvider); setPendingProvider(null); }}
          onClose={() => setPendingProvider(null)}
        />
      )}
    </Modal>
  );
}

/* ---------- Audit drawer ---------- */

function AuditDrawer({ spec, row, onClose }: { spec: TypeSpec; row: Row; onClose: () => void }) {
  const [events, setEvents] = useState<{ id: string; actor: string; action: string; time: string }[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    getMasterAudit(spec.mtype, row.id).then(setEvents).catch((e) => setError(e.message));
  }, [spec.mtype, row.id]);
  return (
    <Drawer open onClose={onClose} title={`Audit log — ${String(row.name ?? row.id)}`}>
      {error ? <ErrorState message={error} /> : events === null ? <div className="t-sub">Loading…</div>
        : events.length === 0 ? <EmptyState title="No audit events yet" />
        : (
          <div className="col gap-8">
            {events.map((e) => (
              <div key={e.id} className="card card-pad-sm row gap-8" style={{ justifyContent: "space-between" }}>
                <div>
                  <div className="t-strong" style={{ fontSize: 13 }}>{e.action}</div>
                  <div className="t-micro">{e.actor}</div>
                </div>
                <div className="t-micro">{new Date(e.time).toLocaleString()}</div>
              </div>
            ))}
          </div>
        )}
    </Drawer>
  );
}

/* ---------- Plan tenants drawer ---------- */

function PlanTenantsDrawer({ row, onClose }: { row: Row; onClose: () => void }) {
  const [tenants, setTenants] = useState<{ id: string; name: string; domain: string; subscriptionStatus: string; mrr: number }[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    listPlanTenants(String(row.id)).then(setTenants).catch((e) => setError(e.message));
  }, [row.id]);
  return (
    <Drawer open onClose={onClose} title={`Tenants on ${String(row.name ?? "plan")}`}>
      {error ? <ErrorState message={error} /> : tenants === null ? <div className="t-sub">Loading…</div>
        : tenants.length === 0 ? <EmptyState title="No tenants use this plan" />
        : (
          <div className="col gap-8">
            {tenants.map((t) => (
              <div key={t.id} className="card card-pad-sm row" style={{ justifyContent: "space-between" }}>
                <div>
                  <div className="t-strong" style={{ fontSize: 13 }}>{t.name}</div>
                  <div className="t-micro">{t.domain}</div>
                </div>
                <div className="col" style={{ alignItems: "flex-end" }}>
                  <StatusChip status={t.subscriptionStatus === "active" ? "active" : "disabled"} label={t.subscriptionStatus} />
                  <span className="t-micro">${t.mrr.toLocaleString()}/mo</span>
                </div>
              </div>
            ))}
          </div>
        )}
    </Drawer>
  );
}

/* ---------- Voice providers (shared by the voices filter bar and voice form) ---------- */

let voiceProvidersPromise: Promise<{ code: string; name: string }[]> | null = null;

function fetchVoiceProviders(): Promise<{ code: string; name: string }[]> {
  if (!voiceProvidersPromise) {
    /* Voice rows reference tts- and voice-kind providers; options come from the
       provider master itself — never duplicated locally. */
    voiceProvidersPromise = Promise.all([
      listMaster<Row>("providers", { kind: "tts", pageSize: 100 }),
      listMaster<Row>("providers", { kind: "voice", pageSize: 100 }),
    ]).then(([tts, voice]) => {
      const seen = new Map<string, string>();
      for (const p of [...tts.items, ...voice.items]) {
        if (p.status === "active" && !seen.has(String(p.code))) seen.set(String(p.code), String(p.name));
      }
      return [...seen].map(([code, name]) => ({ code, name }));
    });
    voiceProvidersPromise.catch(() => { voiceProvidersPromise = null; });
  }
  return voiceProvidersPromise;
}

function useVoiceProviders() {
  const [providers, setProviders] = useState<{ code: string; name: string }[]>([]);
  useEffect(() => {
    let alive = true;
    fetchVoiceProviders().then((p) => { if (alive) setProviders(p); }).catch(() => {});
    return () => { alive = false; };
  }, []);
  return providers;
}

function VoiceProviderSelect({ value, onChange, disabled, label }: {
  value: string; onChange: (code: string) => void; disabled?: boolean; label?: string;
}) {
  const providers = useVoiceProviders();
  const known = providers.some((p) => p.code === value);
  return (
    <select className="select" value={value} disabled={disabled} aria-label={label ?? "Voice provider"}
      onChange={(e) => onChange(e.target.value)}>
      <option value="">—</option>
      {value && !known && <option value={value}>{value} (unavailable — inactive)</option>}
      {providers.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
    </select>
  );
}

/* ---------- Voice filter bar ---------- */

interface VoiceFilterState {
  provider: string;
  gender: string;
  status: string;
}

const EMPTY_VOICE_FILTERS: VoiceFilterState = { provider: "", gender: "", status: "" };

function VoiceFilters({ value, onChange }: {
  value: VoiceFilterState;
  onChange: (next: VoiceFilterState) => void;
}) {
  const providers = useVoiceProviders();
  const active = Object.values(value).filter(Boolean).length;
  return (
    <>
      <select className="select" style={{ width: 170 }} value={value.provider}
        aria-label="Filter voices by provider"
        onChange={(e) => onChange({ ...value, provider: e.target.value })}>
        <option value="">All providers</option>
        {providers.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
      </select>
      <select className="select" style={{ width: 130 }} value={value.gender}
        aria-label="Filter voices by gender"
        onChange={(e) => onChange({ ...value, gender: e.target.value })}>
        <option value="">All genders</option>
        <option value="female">Female</option>
        <option value="male">Male</option>
        <option value="neutral">Neutral</option>
      </select>
      <select className="select" style={{ width: 130 }} value={value.status}
        aria-label="Filter voices by status"
        onChange={(e) => onChange({ ...value, status: e.target.value })}>
        <option value="">All statuses</option>
        <option value="active">Active</option>
        <option value="inactive">Inactive</option>
        <option value="archived">Archived</option>
      </select>
      {active > 0 && (
        <span className="row gap-6" style={{ alignItems: "center" }}>
          <span className="tag">{active} filter{active === 1 ? "" : "s"} active</span>
          <Button size="sm" variant="ghost" icon="x" onClick={() => onChange(EMPTY_VOICE_FILTERS)}>
            Clear filters
          </Button>
        </span>
      )}
    </>
  );
}

/* ---------- Panel per master type ---------- */

function MasterPanel({ spec, addDraft, onAddDraftChange }: {
  spec: TypeSpec;
  addDraft: Record<string, unknown> | null;
  /** Updater-style so cascaded patches never read a stale draft. */
  onAddDraftChange: (update: (prev: Record<string, unknown> | null) => Record<string, unknown> | null) => void;
}) {
  const { toast, hasPermission } = useApp();
  const canManage = hasPermission("manage_master_data")
    || hasPermission(`manage_${spec.mtype.replace("-", "_")}`)
    || hasPermission("manage_data_regions") && spec.mtype === "countries"
    || hasPermission("manage_languages") && spec.mtype === "languages";

  const [rows, setRows] = useState<Row[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [kind, setKind] = useState("");
  const [voiceFilters, setVoiceFilters] = useState<VoiceFilterState>(EMPTY_VOICE_FILTERS);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Row | null | "new">(null);
  const [editForm, setEditForm] = useState<Record<string, unknown> | null>(null);
  const [auditRow, setAuditRow] = useState<Row | null>(null);
  const [tenantsRow, setTenantsRow] = useState<Row | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Row | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const pageSize = 25;

  /* Text search is debounced so typing doesn't fire an API call per keystroke. */
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 350);
    return () => clearTimeout(timer);
  }, [search]);

  /* Monotonic request id — a response from a superseded request (page changed,
     filter changed, page clamped) must never overwrite newer rows. */
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    setError(null);
    try {
      const result = await listMaster<Row>(spec.mtype, {
        search: debouncedSearch || undefined, page, pageSize,
        kind: spec.kindFilter && kind ? kind : undefined,
        provider: spec.voiceFilters ? voiceFilters.provider || undefined : undefined,
        gender: spec.voiceFilters ? voiceFilters.gender || undefined : undefined,
        status: spec.voiceFilters ? voiceFilters.status || undefined : undefined,
      });
      if (seq !== loadSeq.current) return;
      const totalCount = result.meta?.total ?? result.items.length;
      const lastPage = Math.max(1, Math.ceil(totalCount / pageSize));
      if (page > lastPage) {
        /* The current page no longer exists (the last row was archived, or a
           status filter dropped it) — move to the nearest valid page and let
           the effect refetch. Current rows stay visible until it lands. */
        setTotal(totalCount);
        setPage(lastPage);
        return;
      }
      setRows(result.items);
      setTotal(totalCount);
    } catch (e) {
      if (seq !== loadSeq.current) return;
      setError(e instanceof Error ? e.message : "Failed to load.");
      setRows([]);
    }
  }, [spec.mtype, spec.kindFilter, spec.voiceFilters, debouncedSearch, page, kind, voiceFilters]);

  useEffect(() => { void load(); }, [load]);

  /* One mutation at a time — a double-click must never submit twice. */
  const mutating = useRef(false);

  /* Every mutation handler is a useCallback over the *current* `load` (whose
     closure holds the current page, search and filters) and feeds the columns
     memo below. Previously the memoized action buttons kept the first render's
     handlers, so a mutation on page 2 refetched page 1's unfiltered data while
     the pagination control still said page 2. */
  const doStatus = useCallback(async (row: Row, status: "active" | "inactive" | "archived") => {
    if (mutating.current) return;
    mutating.current = true;
    setActionError(null);
    try {
      await setMasterStatus(spec.mtype, row.id, status);
      toast(`${spec.singular} ${status === "active" ? "activated" : status === "inactive" ? "deactivated" : "archived"}`);
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Action failed.");
    } finally {
      mutating.current = false;
    }
  }, [spec.mtype, spec.singular, toast, load]);

  const doDelete = useCallback(async (row: Row) => {
    if (mutating.current) return;
    mutating.current = true;
    setActionError(null);
    try {
      await deleteMaster(spec.mtype, row.id);
      toast(`${spec.singular} archived`);
      void load();
    } catch (e) {
      // Referenced records return a clear 409 message from the backend.
      setActionError(e instanceof Error ? e.message : "Delete failed.");
    } finally {
      mutating.current = false;
      setConfirmDelete(null);
    }
  }, [spec.mtype, spec.singular, toast, load]);

  const doDuplicate = useCallback(async (row: Row) => {
    if (mutating.current) return;
    mutating.current = true;
    setActionError(null);
    try {
      await duplicatePlan(String(row.id));
      toast("Plan duplicated (created inactive)");
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Duplicate failed.");
    } finally {
      mutating.current = false;
    }
  }, [toast, load]);

  const openAdd = () => {
    if (!addDraft) onAddDraftChange(() => buildInitialForm(spec));
    setEditing("new");
  };

  const openEdit = useCallback((row: Row) => {
    setEditForm(buildFormFromRow(spec, row)); // fresh per row — edits never leak between records
    setEditing(row);
  }, [spec]);

  const columns = useMemo<Column<Row>[]>(() => [
    ...spec.columns,
    {
      key: "actions", header: "", align: "right",
      render: (r) => {
        const active = (r.status ?? (r.enabled === false ? "inactive" : "active")) === "active";
        return (
          <span className="row gap-6" style={{ justifyContent: "flex-end" }} onClick={(e) => e.stopPropagation()}>
            <Button size="sm" disabled={!canManage} onClick={() => openEdit(r)}>Edit</Button>
            {spec.mtype === "plans" && (
              <>
                <Button size="sm" disabled={!canManage} onClick={() => void doDuplicate(r)}>Duplicate</Button>
                <Button size="sm" onClick={() => setTenantsRow(r)}>Tenants</Button>
              </>
            )}
            <Button size="sm" disabled={!canManage}
              onClick={() => void doStatus(r, active ? "inactive" : "active")}>
              {active ? "Deactivate" : "Activate"}
            </Button>
            <Button size="sm" onClick={() => setAuditRow(r)} icon="activity" aria-label="Audit log" />
            <Button size="sm" variant="danger-ghost" disabled={!canManage}
              onClick={() => setConfirmDelete(r)} icon="trash" aria-label="Delete" />
          </span>
        );
      },
    },
  ], [spec, canManage, openEdit, doStatus, doDuplicate]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const filtersActive = Boolean(debouncedSearch) || (spec.voiceFilters && Object.values(voiceFilters).some(Boolean));

  const isAdd = editing === "new";
  const editorForm = isAdd ? (addDraft ?? buildInitialForm(spec)) : editForm;

  return (
    <div className="col gap-12">
      <div className="row gap-8" style={{ flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", minWidth: 240 }}>
          <input className="input" placeholder={`Search ${spec.label.toLowerCase()}…`} value={search}
            aria-label={`Search ${spec.label.toLowerCase()}`}
            onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
        </div>
        {spec.kindFilter && (
          <select className="select" style={{ width: 160 }} value={kind}
            aria-label="Filter by provider kind"
            onChange={(e) => { setKind(e.target.value); setPage(1); }}>
            <option value="">All kinds</option>
            {PROVIDER_KINDS.map((k) => <option key={k} value={k}>{k.toUpperCase()}</option>)}
          </select>
        )}
        {spec.voiceFilters && (
          <VoiceFilters value={voiceFilters} onChange={(next) => { setVoiceFilters(next); setPage(1); }} />
        )}
        <div className="grow" />
        <Button variant="primary" icon="plus" disabled={!canManage}
          title={canManage ? undefined : "You don't have permission to manage this master data"}
          onClick={openAdd}>
          Add {spec.singular}
        </Button>
      </div>

      {actionError && <Callout tone="critical" title="Action failed">{actionError}</Callout>}

      <DataTable<Row>
        columns={columns}
        rows={rows}
        loading={rows === null}
        error={error}
        onRetry={() => void load()}
        rowKey={(r) => String(r.id)}
        empty={
          filtersActive
            ? { icon: "filter", title: `No ${spec.label.toLowerCase()} match the current filters`, body: "Adjust or clear the filters to see more results." }
            : { icon: "settings", title: `No ${spec.label.toLowerCase()} yet`, body: canManage ? "Add the first one to make it available across the platform." : undefined }
        }
        footer={
          totalPages > 1 ? (
            <div className="row gap-8" style={{ justifyContent: "flex-end", alignItems: "center" }}>
              <span className="t-micro">{total} total · page {page} of {totalPages}</span>
              <Button size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</Button>
              <Button size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</Button>
            </div>
          ) : undefined
        }
      />

      {editing !== null && editorForm && (() => {
        const Editor = spec.mtype === "voices" ? VoiceEditor : MasterEditor;
        return (
          <Editor
            spec={spec}
            row={isAdd ? null : (editing as Row)}
            form={editorForm}
            onChange={(patch) => {
              if (isAdd) onAddDraftChange((prev) => ({ ...(prev ?? buildInitialForm(spec)), ...patch }));
              else setEditForm((f) => ({ ...(f ?? {}), ...patch }));
            }}
            onReset={() => {
              if (isAdd) onAddDraftChange(() => buildInitialForm(spec));
              else setEditForm(buildFormFromRow(spec, editing as Row));
            }}
            onClose={() => setEditing(null)}
            onSaved={() => {
              if (isAdd) onAddDraftChange(() => null); // draft clears only after a successful create
              void load();
            }}
          />
        );
      })()}
      {auditRow && <AuditDrawer spec={spec} row={auditRow} onClose={() => setAuditRow(null)} />}
      {tenantsRow && <PlanTenantsDrawer row={tenantsRow} onClose={() => setTenantsRow(null)} />}
      {confirmDelete && (
        <ConfirmModal
          open
          title={`Delete ${spec.singular}?`}
          body={`"${String(confirmDelete.name ?? confirmDelete.id)}" will be archived. Records referenced by tenants, bots or subscriptions cannot be deleted — deactivate them instead.`}
          confirmLabel="Delete"
          danger
          onConfirm={() => void doDelete(confirmDelete)}
          onClose={() => setConfirmDelete(null)}
        />
      )}
    </div>
  );
}

/* ---------- Page ---------- */

/** Shared master-data page shell: title, tab strip and the generic
    MasterPanel. The tab is controlled by the caller (page state for
    Platform Configuration, the URL for Regional & Currency Settings). */
export function MasterDataPage({ title, sub, specs, tab, onTabChange }: {
  title: string;
  sub: string;
  specs: TypeSpec[];
  tab: MasterType;
  onTabChange: (tab: MasterType) => void;
}) {
  const spec = specs.find((s) => s.mtype === tab) ?? specs[0];
  /* Add-form drafts per master type — kept at page level so closing the modal
     or switching tabs never discards typed data. Cleared on save or Reset. */
  const drafts = useRef<Record<string, Record<string, unknown> | null>>({});
  const [, bump] = useState(0);
  return (
    <div className="col gap-16">
      <div>
        <h1 className="page-title">{title}</h1>
        <p className="t-sub">{sub}</p>
      </div>
      <Tabs
        tabs={specs.map((s) => ({ id: s.mtype, label: s.label }))}
        active={spec.mtype}
        onChange={(id) => onTabChange(id as MasterType)}
      />
      <MasterPanel
        key={spec.mtype}
        spec={spec}
        addDraft={drafts.current[spec.mtype] ?? null}
        onAddDraftChange={(update) => {
          drafts.current[spec.mtype] = update(drafts.current[spec.mtype] ?? null);
          bump((n) => n + 1);
        }}
      />
    </div>
  );
}

export default function PlatformConfig() {
  const [tab, setTab] = useState<MasterType>(PLATFORM_SPECS[0].mtype);
  return (
    <MasterDataPage
      title="Platform Configuration"
      sub="Master data powering tenant onboarding and bot configuration. Active values appear
        immediately in onboarding; deactivated values are hidden for new tenants while existing
        tenants keep their historical selection."
      specs={PLATFORM_SPECS}
      tab={tab}
      onTabChange={setTab}
    />
  );
}
