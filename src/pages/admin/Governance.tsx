import { useEffect, useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import {
  listGuardrails, listMaster, listModels, listTemplates, setMasterStatus,
  updateGuardrail, updateModelStatus,
} from "@/services/api";
import type { ProviderMaster, ProviderModelMaster, VoiceCapability } from "@/types/domain";
import { DataTable } from "@/components/DataTable";
import { Button, Callout, StatusChip, Tabs, Toggle, EmptyState, CardSkeleton, ErrorState } from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import { Icon } from "@/components/Icon";

const tabs = [
  { id: "matrix", label: "Provider Matrix" },
  { id: "models", label: "Approved Models" },
  { id: "prompts", label: "Prompt Library" },
  { id: "versions", label: "Prompt Versions" },
  { id: "templates", label: "Knowledge Templates" },
  { id: "guardrails", label: "Guardrails" },
];

export default function Governance() {
  const [tab, setTab] = useState("matrix");
  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">AI Governance</h1>
          <p className="page-sub">Central control of models, system prompts and guardrails — invisible to tenant admins</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <div className="mt-16">
        {tab === "matrix" && <MatrixTab />}
        {tab === "models" && <ModelsTab />}
        {tab === "guardrails" && <GuardrailsTab />}
        {tab === "prompts" && (
          <TemplateLibrary
            kind="prompt_library"
            title="Platform prompt library"
            body="System prompt templates (persona scaffolds, safety preambles, language-switch handlers) that tenant prompts compose into. Tenant admins never see these — they only edit business prompts in Prompt Studio."
          />
        )}
        {tab === "versions" && (
          <TemplateLibrary
            kind="prompt_version"
            title="Prompt version registry"
            body="Every system-prompt change is versioned with a diff, approver and rollout ring. Roll back re-pins the previous version platform-wide."
          />
        )}
        {tab === "templates" && (
          <TemplateLibrary
            kind="knowledge_template"
            title="Knowledge templates"
            body="Curated starter packs tenants can clone: chunking presets, FAQ scaffolds and per-industry source checklists."
          />
        )}
      </div>
    </>
  );
}

const CAPABILITIES: { id: VoiceCapability; label: string }[] = [
  { id: "llm", label: "LLM" },
  { id: "embedding", label: "Embedding" },
  { id: "stt", label: "Speech-to-Text" },
  { id: "tts", label: "Text-to-Speech" },
];

function MatrixTab() {
  const { toast, hasPermission } = useApp();
  const canManage = hasPermission("manage_master_data");
  const [capability, setCapability] = useState<VoiceCapability>("llm");
  const [provider, setProvider] = useState<string>("");

  const providersQ = useAsync(
    () => listMaster<ProviderMaster>("providers", { kind: capability, pageSize: 100 }).then((p) => p.items),
    [capability],
  );
  // A capability switch starts from a clean selection; the same provider code
  // can exist per capability with a different status.
  useEffect(() => { setProvider(""); }, [capability]);
  // Keep a valid provider selected for the models panel: prefer the current
  // selection, else the first ACTIVE provider, else the first row.
  useEffect(() => {
    const rows = providersQ.data ?? [];
    if (rows.length === 0) { setProvider(""); return; }
    if (provider && rows.some((r) => r.code === provider)) return;
    setProvider((rows.find((r) => r.status === "active") ?? rows[0]).code);
  }, [providersQ.data]); // eslint-disable-line react-hooks/exhaustive-deps

  const modelsQ = useAsync(
    () => provider
      ? listMaster<ProviderModelMaster>("provider-models", { capability, provider, pageSize: 100 }).then((p) => p.items)
      : Promise.resolve([] as ProviderModelMaster[]),
    [capability, provider],
  );

  const flipStatus = async (
    mtype: "providers" | "provider-models",
    row: { id: string | number; status: string },
    label: string,
    reload: () => void,
  ) => {
    const next = row.status === "active" ? "inactive" : "active";
    try {
      await setMasterStatus(mtype, row.id, next);
      toast(`${label} ${next === "active" ? "activated" : "deactivated"} — audit entry created`);
      reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Status change failed", "error");
    }
  };

  return (
    <>
      <Callout tone="info" title="Platform provider governance">
        The database catalog is the source of truth for provider and model availability.
        Deactivating an entry hides it from every dropdown, rejects it on save, and blocks it
        at runtime immediately (cached bot configurations are invalidated). Existing
        configurations that reference an inactive entry are flagged in edit mode and cannot be
        re-saved until corrected. Records are never deleted — history and IDs stay stable.
      </Callout>
      <div className="row gap-8 mt-16" role="tablist" aria-label="Capability">
        {CAPABILITIES.map((c) => (
          <Button
            key={c.id}
            size="sm"
            variant={capability === c.id ? "primary" : "ghost"}
            onClick={() => setCapability(c.id)}
            aria-pressed={capability === c.id}
          >
            {c.label}
          </Button>
        ))}
      </div>
      <div className="card mt-16">
        <div className="card-header"><span className="card-title">Providers — {CAPABILITIES.find((c) => c.id === capability)?.label}</span></div>
        <DataTable
          loading={providersQ.loading} error={providersQ.error} onRetry={providersQ.reload}
          rows={providersQ.data}
          empty={{ icon: "brain", title: "No providers configured for this capability" }}
          columns={[
            { key: "name", header: "Provider", sortValue: (p: ProviderMaster) => p.name, render: (p: ProviderMaster) => <div><div className="t-strong">{p.name}</div><div className="t-micro"><code>{p.code}</code></div></div> },
            { key: "status", header: "Status", render: (p: ProviderMaster) => <StatusChip status={p.status} /> },
            { key: "usage", header: "In use by", align: "right", sortValue: (p: ProviderMaster) => p.usageCount, render: (p: ProviderMaster) => <span className="t-num">{fmtNum(p.usageCount)}</span> },
            {
              key: "models", header: "", width: 110,
              render: (p: ProviderMaster) => (
                <Button size="sm" variant={provider === p.code ? "primary" : "ghost"} onClick={() => setProvider(p.code)}>
                  Models
                </Button>
              ),
            },
            {
              key: "act", header: "", width: 130,
              render: (p: ProviderMaster) => canManage ? (
                <Button
                  size="sm"
                  variant={p.status === "active" ? "ghost" : "primary"}
                  onClick={() => flipStatus("providers", p, p.name, providersQ.reload)}
                >
                  {p.status === "active" ? "Deactivate" : "Activate"}
                </Button>
              ) : null,
            },
          ]}
        />
      </div>
      <div className="card mt-16">
        <div className="card-header">
          <span className="card-title">
            Models — {provider ? <code>{provider}</code> : "select a provider"}
          </span>
        </div>
        <DataTable
          loading={modelsQ.loading} error={modelsQ.error} onRetry={modelsQ.reload}
          rows={modelsQ.data}
          empty={{ icon: "brain", title: provider ? "No models in the catalog for this provider" : "Select a provider to view its models" }}
          columns={[
            { key: "name", header: "Model", sortValue: (m: ProviderModelMaster) => m.displayName, render: (m: ProviderModelMaster) => <div><div className="t-strong">{m.displayName}</div><div className="t-micro"><code>{m.code}</code>{m.isDefault ? <span className="tag" style={{ marginLeft: 6 }}>default</span> : null}</div></div> },
            { key: "status", header: "Status", render: (m: ProviderModelMaster) => <StatusChip status={m.status} /> },
            { key: "usage", header: "In use by", align: "right", sortValue: (m: ProviderModelMaster) => m.usageCount, render: (m: ProviderModelMaster) => <span className="t-num">{fmtNum(m.usageCount)}</span> },
            {
              key: "act", header: "", width: 130,
              render: (m: ProviderModelMaster) => canManage ? (
                <Button
                  size="sm"
                  variant={m.status === "active" ? "ghost" : "primary"}
                  onClick={() => flipStatus("provider-models", m, m.displayName, modelsQ.reload)}
                >
                  {m.status === "active" ? "Deactivate" : "Activate"}
                </Button>
              ) : null,
            },
          ]}
        />
      </div>
    </>
  );
}

function ModelsTab() {
  const q = useAsync(listModels, []);
  const { toast } = useApp();
  return (
    <div className="card">
      <DataTable
        loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
        empty={{ icon: "brain", title: "No models registered" }}
        columns={[
          { key: "name", header: "Model", sortValue: (m) => m.name, render: (m) => <div><code className="t-strong" style={{ fontSize: 12.5 }}>{m.name}</code><div className="t-micro">{m.provider}</div></div> },
          { key: "purpose", header: "Purpose", sortValue: (m) => m.purpose, render: (m) => <span className="tag" style={{ textTransform: "capitalize" }}>{m.purpose}</span> },
          { key: "status", header: "Status", render: (m) => <StatusChip status={m.status === "approved" ? "approved" : m.status === "testing" ? "testing" : "deprecated"} /> },
          { key: "tenants", header: "Tenants", align: "right", sortValue: (m) => m.tenantsUsing, render: (m) => <span className="t-num">{m.tenantsUsing}</span> },
          { key: "cost", header: "Cost / 1K tok", align: "right", sortValue: (m) => m.costPer1k, render: (m) => <span className="t-num">${m.costPer1k}</span> },
          { key: "latency", header: "p50 latency", align: "right", sortValue: (m) => m.latencyP50, render: (m) => <span className="t-num">{fmtNum(m.latencyP50)}ms</span> },
          {
            key: "act", header: "", width: 130,
            render: (m) => m.status === "testing"
              ? <Button size="sm" variant="primary" onClick={async () => {
                  try {
                    await updateModelStatus(m.id, "approved");
                    toast(`${m.name} approved for production`);
                    q.reload();
                  } catch (e) {
                    toast(e instanceof Error ? e.message : "Approval failed", "error");
                  }
                }}>Approve</Button>
              : m.status === "deprecated"
                ? <Button size="sm" variant="ghost" onClick={() => toast(m.tenantsUsing > 0 ? `Migration plan required before removal — ${m.tenantsUsing} tenants still attached` : "Model can be retired", "info")}>Retire</Button>
                : null,
          },
        ]}
      />
    </div>
  );
}

function GuardrailsTab() {
  const q = useAsync(listGuardrails, []);
  const { toast } = useApp();
  return (
    <>
      <Callout tone="warning" title="Production impact">
        Guardrail changes apply to live traffic within one minute, are versioned, and are recorded in the audit log. Disabling a privacy guardrail requires a second approver.
      </Callout>
      <div className="card mt-16">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
          empty={{ icon: "shield", title: "No guardrails configured" }}
          columns={[
            { key: "name", header: "Guardrail", sortValue: (g) => g.name, render: (g) => <div><div className="t-strong">{g.name}</div><div className="t-micro" style={{ maxWidth: 380 }}>{g.description}</div></div> },
            { key: "category", header: "Category", sortValue: (g) => g.category, render: (g) => <span className="tag">{g.category}</span> },
            { key: "enforcement", header: "Enforcement", render: (g) => <StatusChip status={g.enforcement === "block" ? "critical" : g.enforcement === "redact" ? "info" : "warning"} label={g.enforcement} /> },
            { key: "triggers", header: "Triggers (30d)", align: "right", sortValue: (g) => g.triggers30d, render: (g) => <span className="t-num">{fmtNum(g.triggers30d)}</span> },
            {
              key: "enabled", header: "Enabled",
              render: (g) => (
                <Toggle
                  checked={g.enabled}
                  label={`Toggle ${g.name}`}
                  onChange={async (v) => {
                    if (!v && g.category === "Privacy") { toast("Privacy guardrails need a second approver to disable", "error"); return; }
                    try {
                      await updateGuardrail(g.id, { enabled: v });
                      toast(`${g.name} ${v ? "enabled" : "disabled"} — audit entry created`);
                      q.reload();
                    } catch (e) {
                      toast(e instanceof Error ? e.message : "Update failed", "error");
                    }
                  }}
                />
              ),
            },
          ]}
        />
      </div>
    </>
  );
}

function TemplateLibrary({ kind, title, body }: { kind: string; title: string; body: string }) {
  const q = useAsync(() => listTemplates(kind), [kind]);
  return (
    <div className="card">
      <div className="card-header">
        <div className="col gap-2">
          <span className="card-title">{title}</span>
          <span className="t-micro">{body}</span>
        </div>
      </div>
      {q.loading ? (
        <div style={{ padding: 16 }}><CardSkeleton rows={3} /></div>
      ) : q.error ? (
        <ErrorState message={q.error} onRetry={q.reload} />
      ) : !q.data || q.data.length === 0 ? (
        <EmptyState icon="file" title="Nothing here yet" />
      ) : (
        <div className="col" style={{ padding: 16, gap: 8 }}>
          {q.data.map((item) => (
            <div key={String(item.id)} className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <div className="row gap-12">
                <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name="file" size={14} /></span>
                <div>
                  <div className="t-strong" style={{ fontSize: 13 }}>{String(item.name)}</div>
                  <div className="t-micro">{String(item.description ?? "")}</div>
                </div>
              </div>
              <StatusChip status={String(item.status ?? "active")} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
