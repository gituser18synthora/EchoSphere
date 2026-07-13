import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listEntities, listIntents } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { StatusChip, Tabs, Callout } from "@/components/ui";
import { Icon } from "@/components/Icon";

const tabs = [
  { id: "journeys", label: "Journey Builder" },
  { id: "intents", label: "Intents" },
  { id: "entities", label: "Entities" },
  { id: "actions", label: "Actions" },
];

export default function WorkflowsAdmin() {
  const [tab, setTab] = useState("journeys");
  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Workflows</h1>
          <p className="page-sub">Platform building blocks shared across tenants</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <div className="mt-16">
        {tab === "journeys" && (
          <div className="grid grid-3">
            {[
              { name: "Appointment booking journey", tenants: 12, nodes: 10, status: "published" },
              { name: "Billing enquiry journey", tenants: 8, nodes: 14, status: "published" },
              { name: "Order status journey", tenants: 9, nodes: 8, status: "published" },
              { name: "Identity verification block", tenants: 23, nodes: 5, status: "published" },
              { name: "Abuse de-escalation block", tenants: 47, nodes: 4, status: "published" },
              { name: "Outbound survey journey", tenants: 6, nodes: 7, status: "draft" },
            ].map((j) => (
              <div key={j.name} className="card card-pad col gap-12 card-clickable">
                <div className="row gap-12">
                  <span className="icon-tile brand"><Icon name="workflow" size={16} /></span>
                  <div className="grow">
                    <div className="t-strong" style={{ fontSize: 13.5 }}>{j.name}</div>
                    <div className="t-micro">{j.nodes} nodes · used by {j.tenants} tenants</div>
                  </div>
                </div>
                <div className="row-between">
                  <StatusChip status={j.status} />
                  <span className="t-micro">Template</span>
                </div>
              </div>
            ))}
          </div>
        )}
        {tab === "intents" && <IntentsTab />}
        {tab === "entities" && <EntitiesTab />}
        {tab === "actions" && (
          <>
            <Callout tone="info" title="Action registry">
              Reusable action blocks (API call, SMS, warm transfer, ticket creation) exposed to tenant workflow builders. Adding an action here makes it available in every tenant's node palette.
            </Callout>
            <div className="grid grid-4 mt-16">
              {[
                ["API call", "zap", "HTTP request with mapped response"],
                ["Send SMS", "message", "Templated SMS via notify service"],
                ["Warm transfer", "headphones", "Bridge caller to agent queue"],
                ["Create ticket", "file", "Zendesk / Salesforce case"],
                ["Schedule callback", "calendar", "Outbound dial at chosen slot"],
                ["Send email", "mail", "Templated transactional email"],
                ["Update CRM", "database", "Write fields to connected CRM"],
                ["Run sub-journey", "workflow", "Invoke a shared journey block"],
              ].map(([name, icon, sub]) => (
                <div key={name} className="card card-pad col gap-8">
                  <span className="icon-tile neutral"><Icon name={icon as never} size={16} /></span>
                  <span className="t-strong" style={{ fontSize: 13 }}>{name}</span>
                  <span className="t-micro">{sub}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </>
  );
}

function IntentsTab() {
  const q = useAsync(() => listIntents("bot-101"), []);
  return (
    <div className="card">
      <DataTable
        loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
        empty={{ icon: "target", title: "No shared intents" }}
        columns={[
          { key: "name", header: "Intent", sortValue: (i) => i.name, render: (i) => <div><code className="t-strong" style={{ fontSize: 12.5 }}>{i.name}</code><div className="t-micro">{i.description}</div></div> },
          { key: "samples", header: "Samples", align: "right", sortValue: (i) => i.samples.length, render: (i) => <span className="t-num">{i.samples.length}</span> },
          { key: "conf", header: "Avg confidence", align: "right", sortValue: (i) => i.avgConfidence30d, render: (i) => <span className="t-num">{(i.avgConfidence30d * 100).toFixed(0)}%</span> },
          { key: "status", header: "Status", render: (i) => <StatusChip status={i.status} /> },
          { key: "version", header: "Version", align: "right", render: (i) => <code>v{i.version}</code> },
        ]}
      />
    </div>
  );
}

function EntitiesTab() {
  const q = useAsync(listEntities, []);
  return (
    <div className="card">
      <DataTable
        loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
        empty={{ icon: "layers", title: "No entities defined" }}
        columns={[
          { key: "name", header: "Entity", sortValue: (e) => e.name, render: (e) => <code className="t-strong" style={{ fontSize: 12.5 }}>{e.name}</code> },
          { key: "kind", header: "Kind", sortValue: (e) => e.kind, render: (e) => <span className="tag" style={{ textTransform: "capitalize" }}>{e.kind}</span> },
          { key: "example", header: "Extraction example", render: (e) => <span className="t-sub" style={{ fontSize: 12.5 }}>{e.example}</span> },
          { key: "pii", header: "PII", render: (e) => e.pii ? <StatusChip status="warning" label="PII" /> : <span className="t-micro">—</span> },
          { key: "used", header: "Used by", render: (e) => <span className="t-sub">{e.usedBy.join(", ")}</span> },
        ]}
      />
    </div>
  );
}
