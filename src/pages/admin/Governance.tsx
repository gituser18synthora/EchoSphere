import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listGuardrails, listModels, listTemplates, updateGuardrail, updateModelStatus } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Button, Callout, StatusChip, Tabs, Toggle, EmptyState, CardSkeleton, ErrorState } from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import { Icon } from "@/components/Icon";

const tabs = [
  { id: "models", label: "Approved Models" },
  { id: "prompts", label: "Prompt Library" },
  { id: "versions", label: "Prompt Versions" },
  { id: "templates", label: "Knowledge Templates" },
  { id: "guardrails", label: "Guardrails" },
];

export default function Governance() {
  const [tab, setTab] = useState("models");
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
