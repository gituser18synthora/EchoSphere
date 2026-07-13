import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listGuardrails, listModels, simulateAction } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Button, Callout, StatusChip, Tabs, Toggle, EmptyState } from "@/components/ui";
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
          <PlaceholderLibrary
            title="Platform prompt library"
            body="System prompt templates (persona scaffolds, safety preambles, language-switch handlers) that tenant prompts compose into. Tenant admins never see these — they only edit business prompts in Prompt Studio."
            items={[
              ["Safety preamble v9", "Injected into every conversation model call", "approved"],
              ["Healthcare persona scaffold v4", "Applied to healthcare guardrail profile", "approved"],
              ["Language-switch handler v2", "Mid-call language change behaviour", "approved"],
              ["Escalation de-escalation frame v3", "Wraps handover messages on abuse triggers", "pending_approval"],
            ]}
          />
        )}
        {tab === "versions" && (
          <PlaceholderLibrary
            title="Prompt version registry"
            body="Every system-prompt change is versioned with a diff, approver and rollout ring. Roll back re-pins the previous version platform-wide."
            items={[
              ["Safety preamble v9 → v10 (draft)", "Adds jailbreak-resistance clause · ring: canary 5%", "draft"],
              ["Healthcare scaffold v3 → v4", "Published Jun 20 · approved by A. Rivera", "published"],
              ["Safety preamble v8 → v9", "Published Jun 2 · approved by A. Rivera", "published"],
            ]}
          />
        )}
        {tab === "templates" && (
          <PlaceholderLibrary
            title="Knowledge templates"
            body="Curated starter packs tenants can clone: chunking presets, FAQ scaffolds and per-industry source checklists."
            items={[
              ["Healthcare clinic pack", "Locations, insurance, prep FAQs · used by 12 tenants", "approved"],
              ["Banking servicing pack", "Balances, disputes, card services · used by 7 tenants", "approved"],
              ["Retail order-support pack", "Orders, returns, shipping · used by 9 tenants", "approved"],
            ]}
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
              ? <Button size="sm" variant="primary" onClick={async () => { await simulateAction("approve"); toast(`${m.name} approved for production`); q.reload(); }}>Approve</Button>
              : m.status === "deprecated"
                ? <Button size="sm" variant="ghost" onClick={() => toast("Migration plan required before removal — 5 tenants still attached", "info")}>Retire</Button>
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
  const [local, setLocal] = useState<Record<string, boolean>>({});
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
                  checked={local[g.id] ?? g.enabled}
                  label={`Toggle ${g.name}`}
                  onChange={(v) => {
                    if (!v && g.category === "Privacy") { toast("Privacy guardrails need a second approver to disable", "error"); return; }
                    setLocal((l) => ({ ...l, [g.id]: v }));
                    toast(`${g.name} ${v ? "enabled" : "disabled"} — audit entry created`);
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

function PlaceholderLibrary({ title, body, items }: { title: string; body: string; items: [string, string, string][] }) {
  return (
    <div className="card">
      <div className="card-header">
        <div className="col gap-2">
          <span className="card-title">{title}</span>
          <span className="t-micro">{body}</span>
        </div>
      </div>
      {items.length === 0 ? (
        <EmptyState icon="file" title="Nothing here yet" />
      ) : (
        <div className="col" style={{ padding: 16, gap: 8 }}>
          {items.map(([name, sub, status]) => (
            <div key={name} className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <div className="row gap-12">
                <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name="file" size={14} /></span>
                <div>
                  <div className="t-strong" style={{ fontSize: 13 }}>{name}</div>
                  <div className="t-micro">{sub}</div>
                </div>
              </div>
              <StatusChip status={status} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
