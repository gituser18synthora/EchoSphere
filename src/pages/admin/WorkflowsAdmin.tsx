import { useMemo, useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listEntities, listIntents, listTemplates, listTenants } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { StatusChip, Tabs, Callout, CardSkeleton, EmptyState, ErrorState } from "@/components/ui";
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
        {tab === "journeys" && <JourneysTab />}
        {tab === "intents" && <IntentsTab />}
        {tab === "entities" && <EntitiesTab />}
        {tab === "actions" && <ActionsTab />}
      </div>
    </>
  );
}

function JourneysTab() {
  const q = useAsync(() => listTemplates("journey_template"), []);
  if (q.loading) return <div className="grid grid-3">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={2} />)}</div>;
  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (!q.data || q.data.length === 0) return <EmptyState icon="workflow" title="No journey templates" />;
  return (
    <div className="grid grid-3">
      {q.data.map((j) => (
        <div key={String(j.id)} className="card card-pad col gap-12 card-clickable">
          <div className="row gap-12">
            <span className="icon-tile brand"><Icon name="workflow" size={16} /></span>
            <div className="grow">
              <div className="t-strong" style={{ fontSize: 13.5 }}>{String(j.name)}</div>
              <div className="t-micro">{j.nodes ? `${j.nodes} nodes · ` : ""}{String(j.description ?? "")}</div>
            </div>
          </div>
          <div className="row-between">
            <StatusChip status={String(j.status ?? "active")} />
            <span className="t-micro">Template</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function ActionsTab() {
  const q = useAsync(() => listTemplates("action_block"), []);
  return (
    <>
      <Callout tone="info" title="Action registry">
        Reusable action blocks (API call, SMS, warm transfer, ticket creation) exposed to tenant workflow builders. Adding an action here makes it available in every tenant's node palette.
      </Callout>
      <div className="grid grid-4 mt-16">
        {q.loading && Array.from({ length: 8 }).map((_, i) => <CardSkeleton key={i} rows={2} />)}
        {q.error && <ErrorState message={q.error} onRetry={q.reload} />}
        {q.data?.map((a) => (
          <div key={String(a.id)} className="card card-pad col gap-8">
            <span className="icon-tile neutral"><Icon name={(a.icon as never) ?? "zap"} size={16} /></span>
            <span className="t-strong" style={{ fontSize: 13 }}>{String(a.name)}</span>
            <span className="t-micro">{String(a.description ?? "")}</span>
          </div>
        ))}
        {q.data && q.data.length === 0 && <EmptyState icon="zap" title="No action blocks" />}
      </div>
    </>
  );
}

/** Pick the first tenant that has bots so the shared intent/entity views show data. */
function useFirstTenant() {
  const tenants = useAsync(listTenants, []);
  const firstId = useMemo(
    () => tenants.data?.find((t) => t.bots > 0)?.id ?? tenants.data?.[0]?.id,
    [tenants.data],
  );
  return { tenants, firstId };
}

function IntentsTab() {
  const { tenants, firstId } = useFirstTenant();
  const bots = useAsync<import("@/types/domain").VoiceBot[]>(
    async () => (firstId ? listBots(firstId) : []), [firstId],
  );
  const firstBot = bots.data?.[0]?.id;
  const q = useAsync<import("@/types/domain").Intent[]>(
    async () => (firstBot ? listIntents(firstBot) : []), [firstBot],
  );
  const tenantName = tenants.data?.find((t) => t.id === firstId)?.name;
  return (
    <div className="card">
      {tenantName && (
        <div className="card-header">
          <span className="card-title">Intents — {tenantName}{bots.data?.[0] ? ` · ${bots.data[0].name}` : ""}</span>
        </div>
      )}
      <DataTable
        loading={tenants.loading || bots.loading || q.loading} error={q.error} onRetry={q.reload} rows={q.data}
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
  const { tenants, firstId } = useFirstTenant();
  const q = useAsync<import("@/types/domain").EntityDef[]>(
    async () => (firstId ? listEntities(firstId) : []), [firstId],
  );
  return (
    <div className="card">
      <DataTable
        loading={tenants.loading || q.loading} error={q.error} onRetry={q.reload} rows={q.data}
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
