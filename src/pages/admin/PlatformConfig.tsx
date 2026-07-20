/* Platform Configuration — Super Admin master-data management.
   Industries, Data Regions, Plans, AI Configuration Profiles, Providers
   (Voice/STT/TTS/LLM/Embedding), Supported Languages and Voice Profiles.

   Every list is server-backed (search / sort / pagination), every mutation is
   permission-enforced and audited on the backend; referenced records cannot be
   permanently deleted — the API returns a clear message and the UI offers
   deactivation instead. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useApp } from "@/state/AppContext";
import {
  createMaster, deleteMaster, duplicatePlan, getMasterAudit, listMaster,
  listPlanTenants, setMasterStatus, updateMaster, type MasterType,
} from "@/services/api";
import { Button, Callout, ConfirmModal, Drawer, EmptyState, ErrorState, Field, Modal, StatusChip, Tabs, Toggle } from "@/components/ui";
import { DataTable, type Column } from "@/components/DataTable";

/* ---------- Generic row & field descriptors ---------- */

type Row = Record<string, unknown> & {
  id: string;
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
  type: "text" | "textarea" | "number" | "toggle" | "select";
  options?: { value: string; label: string }[];
  required?: boolean;
  hint?: string;
  createOnly?: boolean;
  step?: number;
}

interface TypeSpec {
  mtype: MasterType;
  label: string;
  singular: string;
  columns: Column<Row>[];
  fields: FieldDef[];
  kindFilter?: boolean;
}

const text = (key: string, label: string, opts: Partial<FieldDef> = {}): FieldDef =>
  ({ key, label, type: "text", ...opts });
const num = (key: string, label: string, opts: Partial<FieldDef> = {}): FieldDef =>
  ({ key, label, type: "number", ...opts });

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
const updatedCol: Column<Row> = {
  key: "updatedAt", header: "Updated", sortValue: (r) => String(r.updatedAt ?? ""),
  render: (r) => <span className="t-micro">{r.updatedAt ? new Date(String(r.updatedAt)).toLocaleDateString() : "—"}{r.updatedBy ? ` · ${r.updatedBy}` : ""}</span>,
};

const PROVIDER_KINDS = ["stt", "tts", "llm", "embedding", "voice"] as const;

const SPECS: TypeSpec[] = [
  {
    mtype: "industries", label: "Industries", singular: "industry",
    columns: [nameCol, codeCol, { key: "description", header: "Description", render: (r) => <span className="t-sub">{String(r.description ?? "")}</span> }, usageCol, statusCol, updatedCol],
    fields: [
      text("code", "Code", { required: true, createOnly: true, hint: "Stable identifier, e.g. banking" }),
      text("name", "Name", { required: true }),
      { key: "description", label: "Description", type: "textarea" },
      text("icon", "Icon"),
      num("sortOrder", "Sort order"),
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
      usageCol, statusCol, updatedCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true, hint: "e.g. in-mumbai" }),
      text("name", "Name", { required: true }),
      { key: "description", label: "Description", type: "textarea" },
      text("country", "Country"),
      text("region", "Region"),
      text("cloudProvider", "Cloud provider"),
      text("storageRegion", "Storage region"),
      text("databaseRegion", "Database region"),
      text("recordingRegion", "Recording region"),
      text("transcriptRegion", "Transcript region"),
      { key: "infrastructureReady", label: "Infrastructure deployed", type: "toggle",
        hint: "Only enable when infrastructure actually runs in this region — the UI distinguishes configured vs deployed regions." },
      num("sortOrder", "Sort order"),
    ],
  },
  {
    mtype: "plans", label: "Plans", singular: "plan",
    columns: [
      nameCol, codeCol,
      { key: "priceMonthly", header: "Monthly", align: "right", sortValue: (r) => Number(r.priceMonthly ?? 0), render: (r) => <span>{String(r.currency ?? "USD")} {Number(r.priceMonthly ?? 0).toLocaleString()}</span> },
      { key: "limits", header: "Limits", render: (r) => <span className="t-micro">{Number(r.botLimit ?? 0)} bots · {Number(r.minutesIncluded ?? 0).toLocaleString()} min · {Number(r.seatsIncluded ?? 0)} seats</span> },
      { key: "isRecommended", header: "Flags", render: (r) => <span className="row gap-6">{Boolean(r.isRecommended) && <span className="tag">Recommended</span>}{!r.isPublic && <span className="tag">Hidden</span>}</span> },
      usageCol, statusCol, updatedCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true }),
      text("name", "Name", { required: true }),
      { key: "description", label: "Description", type: "textarea" },
      num("priceMonthly", "Monthly price", { step: 0.01 }),
      num("priceAnnual", "Annual price", { step: 0.01 }),
      text("currency", "Currency", { hint: "ISO code, e.g. USD / INR" }),
      num("botLimit", "Included bots"),
      num("minutesIncluded", "Included minutes"),
      num("seatsIncluded", "Included users"),
      num("kbLimit", "Included knowledge bases"),
      num("storageGbIncluded", "Included storage (GB)"),
      num("languagesIncluded", "Included languages"),
      num("concurrentCallLimit", "Concurrent call limit"),
      num("monthlyCallLimit", "Monthly call limit (0 = unlimited)"),
      num("monthlyTokenLimit", "Monthly token limit (0 = unlimited)"),
      num("monthlyEmbeddingLimit", "Monthly embedding limit (0 = unlimited)"),
      num("recordingRetentionDays", "Recording retention (days)"),
      num("transcriptRetentionDays", "Transcript retention (days)"),
      num("analyticsRetentionDays", "Analytics retention (days)"),
      { key: "isPublic", label: "Visible in onboarding", type: "toggle" },
      { key: "isRecommended", label: "Recommended", type: "toggle" },
      num("sortOrder", "Sort order"),
    ],
  },
  {
    mtype: "ai-profiles", label: "AI Profiles", singular: "AI configuration profile",
    columns: [
      nameCol, codeCol,
      { key: "models", header: "Stack", render: (r) => <span className="t-micro">{[r.llmProvider && `LLM ${r.llmProvider}/${r.llmModel ?? ""}`, r.sttProvider && `STT ${r.sttProvider}`, r.ttsProvider && `TTS ${r.ttsProvider}`].filter(Boolean).join(" · ") || "Custom"}</span> },
      { key: "costCategory", header: "Cost", render: (r) => <span className="tag">{String(r.costCategory ?? "")}</span> },
      usageCol, statusCol, updatedCol,
    ],
    fields: [
      text("code", "Code", { required: true, createOnly: true }),
      text("name", "Name", { required: true }),
      { key: "description", label: "Description", type: "textarea" },
      text("sttProvider", "STT provider"), text("sttModel", "STT model"),
      text("llmProvider", "LLM provider"), text("llmModel", "LLM model"),
      text("ttsProvider", "TTS provider"), text("ttsModel", "TTS model"),
      text("defaultVoice", "Default voice"),
      text("embeddingProvider", "Embedding provider"), text("embeddingModel", "Embedding model"),
      num("embeddingDimension", "Embedding dimension"),
      text("rerankingModel", "Reranking model"),
      num("retrievalTopK", "Retrieval top-K"),
      num("retrievalThreshold", "Retrieval threshold", { step: 0.05 }),
      num("temperature", "Temperature", { step: 0.1 }),
      num("maxOutputTokens", "Max output tokens"),
      num("responseTimeoutMs", "Response timeout (ms)"),
      { key: "costCategory", label: "Cost category", type: "select", options: [
        { value: "low", label: "Low" }, { value: "medium", label: "Medium" }, { value: "high", label: "High" },
      ]},
      num("sortOrder", "Sort order"),
    ],
  },
  {
    mtype: "providers", label: "Providers", singular: "provider", kindFilter: true,
    columns: [
      nameCol, codeCol,
      { key: "kind", header: "Kind", render: (r) => <span className="tag">{String(r.kind ?? "").toUpperCase()}</span> },
      { key: "requiresApiKey", header: "API key", render: (r) => <span className="t-micro">{r.requiresApiKey ? String(r.secretRef ?? "required") : "not required"}</span> },
      usageCol, statusCol, updatedCol,
    ],
    fields: [
      { key: "kind", label: "Kind", type: "select", required: true, createOnly: true,
        options: PROVIDER_KINDS.map((k) => ({ value: k, label: k.toUpperCase() })) },
      text("code", "Code", { required: true, createOnly: true }),
      text("name", "Name", { required: true }),
      { key: "description", label: "Description", type: "textarea" },
      text("website", "Website"),
      { key: "requiresApiKey", label: "Requires API key", type: "toggle" },
      text("secretRef", "Secret reference", { hint: "env:VAR_NAME reference only — raw keys are never stored here." }),
      num("sortOrder", "Sort order"),
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
      usageCol, statusCol, updatedCol,
    ],
    fields: [
      text("code", "Locale code", { required: true, createOnly: true, hint: "BCP-47, e.g. hi-IN" }),
      text("name", "Display name", { required: true }),
      text("nativeName", "Native name"),
      text("isoCode", "ISO 639 code", { hint: "e.g. hi" }),
      text("script", "Script", { hint: "e.g. Devanagari" }),
      { key: "direction", label: "Text direction", type: "select", options: [
        { value: "ltr", label: "Left to right" }, { value: "rtl", label: "Right to left" },
      ]},
      { key: "isDefault", label: "Platform default", type: "toggle" },
      num("sortOrder", "Sort order"),
    ],
  },
  {
    mtype: "voices", label: "Voices", singular: "voice",
    columns: [
      { ...nameCol, render: (r) => <span className="row gap-8"><span className="t-strong">{String(r.name ?? "")}</span>{Boolean(r.isDefault) && <span className="tag">Default</span>}</span> },
      { key: "provider", header: "Provider", render: (r) => <span>{String(r.provider ?? "platform")}</span> },
      { key: "languages", header: "Languages", render: (r) => <span className="t-micro">{((r.languages as string[]) ?? []).join(", ")}</span> },
      { key: "gender", header: "Gender", render: (r) => <span>{String(r.gender ?? "")}</span> },
      { key: "speakingRate", header: "Rate", align: "right", render: (r) => <span>{Number(r.speakingRate ?? 1).toFixed(2)}×</span> },
      usageCol, statusCol, updatedCol,
    ],
    fields: [
      text("name", "Name", { required: true }),
      { key: "gender", label: "Gender / style", type: "select", options: [
        { value: "female", label: "Female" }, { value: "male", label: "Male" }, { value: "neutral", label: "Neutral" },
      ]},
      text("provider", "Provider", { hint: "platform, elevenlabs, azure, google…" }),
      text("providerVoiceId", "Provider voice ID"),
      text("locale", "Locale", { hint: "e.g. en-IN" }),
      text("accent", "Accent"),
      { key: "description", label: "Description", type: "textarea" },
      num("speakingRate", "Speaking rate", { step: 0.05 }),
      num("pitch", "Pitch", { step: 0.05 }),
      num("latencyMs", "Latency (ms)"),
      { key: "premium", label: "Premium", type: "toggle" },
      { key: "isDefault", label: "Platform default", type: "toggle" },
      { key: "sampleText", label: "Sample sentence", type: "textarea" },
      num("sortOrder", "Sort order"),
    ],
  },
];

/* ---------- Editor modal ---------- */

function MasterEditor({ spec, row, onClose, onSaved }: {
  spec: TypeSpec; row: Row | null; onClose: () => void; onSaved: () => void;
}) {
  const { toast } = useApp();
  const [form, setForm] = useState<Record<string, unknown>>(() => {
    const initial: Record<string, unknown> = {};
    for (const f of spec.fields) initial[f.key] = row ? row[f.key] ?? "" : f.type === "toggle" ? false : "";
    return initial;
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const set = (key: string, value: unknown) => setForm((f) => ({ ...f, [key]: value }));

  const save = async () => {
    for (const f of spec.fields) {
      if (f.required && (row ? !f.createOnly : true) && !String(form[f.key] ?? "").trim()) {
        setError(`${f.label} is required.`);
        return;
      }
    }
    setBusy(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {};
      for (const f of spec.fields) {
        if (row && f.createOnly) continue;
        let value = form[f.key];
        if (f.type === "number") value = value === "" || value === null ? undefined : Number(value);
        if (value === "" || value === undefined) continue;
        payload[f.key] = value;
      }
      if (row) await updateMaster(spec.mtype, row.id, payload);
      else await createMaster(spec.mtype, payload);
      toast(row ? `${spec.singular} updated` : `${spec.singular} created`);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} wide
      title={row ? `Edit ${spec.singular}` : `Add ${spec.singular}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" busy={busy} onClick={save}>{row ? "Save changes" : "Create"}</Button>
        </>
      }>
      <div className="col gap-12">
        {error && <Callout tone="critical">{error}</Callout>}
        <div className="grid grid-2">
          {spec.fields.map((f) => (
            <Field key={f.key} label={f.label} required={f.required} hint={f.hint}>
              {f.type === "toggle" ? (
                <Toggle checked={Boolean(form[f.key])} onChange={(v) => set(f.key, v)} />
              ) : f.type === "select" ? (
                <select className="select" value={String(form[f.key] ?? "")}
                  disabled={Boolean(row && f.createOnly)}
                  onChange={(e) => set(f.key, e.target.value)}>
                  <option value="">—</option>
                  {(f.options ?? []).map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              ) : f.type === "textarea" ? (
                <textarea className="textarea" rows={2} value={String(form[f.key] ?? "")}
                  onChange={(e) => set(f.key, e.target.value)} />
              ) : (
                <input className="input" type={f.type === "number" ? "number" : "text"}
                  step={f.step} disabled={Boolean(row && f.createOnly)}
                  value={String(form[f.key] ?? "")}
                  onChange={(e) => set(f.key, e.target.value)} />
              )}
            </Field>
          ))}
        </div>
      </div>
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
    listPlanTenants(row.id).then(setTenants).catch((e) => setError(e.message));
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

/* ---------- Panel per master type ---------- */

function MasterPanel({ spec }: { spec: TypeSpec }) {
  const { toast, hasPermission } = useApp();
  const canManage = hasPermission("manage_master_data")
    || hasPermission(`manage_${spec.mtype.replace("-", "_")}`)
    || hasPermission("manage_languages") && spec.mtype === "languages";

  const [rows, setRows] = useState<Row[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState("");
  const [kind, setKind] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Row | null | "new">(null);
  const [auditRow, setAuditRow] = useState<Row | null>(null);
  const [tenantsRow, setTenantsRow] = useState<Row | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Row | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const pageSize = 25;

  const load = useCallback(async () => {
    setError(null);
    try {
      const result = await listMaster<Row>(spec.mtype, {
        search: search || undefined, page, pageSize,
        kind: spec.kindFilter && kind ? kind : undefined,
      });
      setRows(result.items);
      setTotal(result.meta?.total ?? result.items.length);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load.");
      setRows([]);
    }
  }, [spec.mtype, spec.kindFilter, search, page, kind]);

  useEffect(() => { void load(); }, [load]);

  const doStatus = async (row: Row, status: "active" | "inactive" | "archived") => {
    setActionError(null);
    try {
      await setMasterStatus(spec.mtype, row.id, status);
      toast(`${spec.singular} ${status === "active" ? "activated" : status === "inactive" ? "deactivated" : "archived"}`);
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Action failed.");
    }
  };

  const doDelete = async (row: Row) => {
    setActionError(null);
    try {
      await deleteMaster(spec.mtype, row.id);
      toast(`${spec.singular} archived`);
      void load();
    } catch (e) {
      // Referenced records return a clear 409 message from the backend.
      setActionError(e instanceof Error ? e.message : "Delete failed.");
    } finally {
      setConfirmDelete(null);
    }
  };

  const doDuplicate = async (row: Row) => {
    setActionError(null);
    try {
      await duplicatePlan(row.id);
      toast("Plan duplicated (created inactive)");
      void load();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Duplicate failed.");
    }
  };

  const columns = useMemo<Column<Row>[]>(() => [
    ...spec.columns,
    {
      key: "actions", header: "", align: "right",
      render: (r) => {
        const active = (r.status ?? (r.enabled === false ? "inactive" : "active")) === "active";
        return (
          <span className="row gap-6" style={{ justifyContent: "flex-end" }} onClick={(e) => e.stopPropagation()}>
            <Button size="sm" disabled={!canManage} onClick={() => setEditing(r)}>Edit</Button>
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
  ], [spec, canManage]);

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <div className="col gap-12">
      <div className="row gap-8" style={{ flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", minWidth: 240 }}>
          <input className="input" placeholder={`Search ${spec.label.toLowerCase()}…`} value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(1); }} />
        </div>
        {spec.kindFilter && (
          <select className="select" style={{ width: 160 }} value={kind}
            onChange={(e) => { setKind(e.target.value); setPage(1); }}>
            <option value="">All kinds</option>
            {PROVIDER_KINDS.map((k) => <option key={k} value={k}>{k.toUpperCase()}</option>)}
          </select>
        )}
        <div className="grow" />
        <Button variant="primary" icon="plus" disabled={!canManage}
          title={canManage ? undefined : "You don't have permission to manage this master data"}
          onClick={() => setEditing("new")}>
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
        rowKey={(r) => r.id}
        empty={{ icon: "settings", title: `No ${spec.label.toLowerCase()} yet`, body: canManage ? "Add the first one to make it available across the platform." : undefined }}
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

      {editing !== null && (
        <MasterEditor spec={spec} row={editing === "new" ? null : editing}
          onClose={() => setEditing(null)} onSaved={() => void load()} />
      )}
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

export default function PlatformConfig() {
  const [tab, setTab] = useState(SPECS[0].mtype);
  const spec = SPECS.find((s) => s.mtype === tab) ?? SPECS[0];
  return (
    <div className="col gap-16">
      <div>
        <h1 className="page-title">Platform Configuration</h1>
        <p className="t-sub">
          Master data powering tenant onboarding and bot configuration. Active values appear
          immediately in onboarding; deactivated values are hidden for new tenants while existing
          tenants keep their historical selection.
        </p>
      </div>
      <Tabs
        tabs={SPECS.map((s) => ({ id: s.mtype, label: s.label }))}
        active={tab}
        onChange={(id) => setTab(id as MasterType)}
      />
      <MasterPanel key={spec.mtype} spec={spec} />
    </div>
  );
}
