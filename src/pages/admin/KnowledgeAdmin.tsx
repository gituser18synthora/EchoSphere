import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listKnowledge } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { StatusChip, Tabs, Callout, KpiCard, CardSkeleton } from "@/components/ui";
import { fmtNum, ChartCard, LineChart, Legend } from "@/components/charts";
import { genSeries } from "@/services/mockData";

const tabs = [
  { id: "global", label: "Global Knowledge" },
  { id: "docs", label: "Document Repository" },
  { id: "urls", label: "URL Repository" },
  { id: "embeddings", label: "Embedding Monitor" },
];

export default function KnowledgeAdmin() {
  const [tab, setTab] = useState("global");
  const q = useAsync(() => listKnowledge(), []);

  const filtered =
    tab === "docs" ? (q.data ?? []).filter((k) => k.type === "document")
    : tab === "urls" ? (q.data ?? []).filter((k) => k.type === "url")
    : q.data ?? [];

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Knowledge</h1>
          <p className="page-sub">Platform-wide index inventory and embedding pipeline health</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <div className="mt-16">
        {tab === "embeddings" ? (
          <>
            <Callout tone="warning" title="Backlog above target">
              14,220 chunks queued (11.4 min). Auto-scaling added 2 workers at 07:30 UTC; ETA to drain: ~22 min.
            </Callout>
            <div className="grid grid-4 mt-16">
              <KpiCard label="Chunks indexed (24h)" value="182K" delta={6.1} icon="database" />
              <KpiCard label="Queue depth" value="14,220" delta={38.2} intent="down-good" icon="clock" />
              <KpiCard label="Failed jobs (24h)" value="12" delta={-25} intent="down-good" icon="x-circle" />
              <KpiCard label="Embedding cost (24h)" value="$41.80" delta={4.4} intent="down-good" icon="dollar" />
            </div>
            <div className="mt-16">
              <ChartCard title="Embedding queue depth" sub="Chunks awaiting indexing, last 24h (hourly)" legend={<Legend shape="line" items={[{ label: "Queue depth", color: "var(--series-3)" }]} />}>
                <LineChart
                  data={Array.from({ length: 24 }, (_, i) => ({ t: `${i}:00`, depth: genSeries(77, 24, 6000, 7000, 350)[i] }))}
                  x="t" series={[{ key: "depth", label: "Queue depth", color: "var(--series-3)", area: true }]} height={220}
                />
              </ChartCard>
            </div>
          </>
        ) : (
          <div className="card">
            {q.loading ? <div style={{ padding: 16 }}><CardSkeleton rows={5} /></div> : (
              <DataTable
                loading={false} error={q.error} onRetry={q.reload} rows={filtered}
                empty={{ icon: "book", title: "No sources in this view" }}
                columns={[
                  { key: "name", header: "Source", sortValue: (k) => k.name, render: (k) => <div><div className="t-strong">{k.name}</div><div className="t-micro">{k.detail}</div></div> },
                  { key: "scope", header: "Scope", sortValue: (k) => k.scope, render: (k) => <span className="tag" style={{ textTransform: "capitalize" }}>{k.scope}</span> },
                  { key: "type", header: "Type", sortValue: (k) => k.type, render: (k) => <span className="tag" style={{ textTransform: "capitalize" }}>{k.type}</span> },
                  { key: "status", header: "Index status", sortValue: (k) => k.status, render: (k) => <StatusChip status={k.status} /> },
                  { key: "chunks", header: "Chunks", align: "right", sortValue: (k) => k.chunks, render: (k) => <span className="t-num">{fmtNum(k.chunks)}</span> },
                  { key: "usage", header: "Hits (30d)", align: "right", sortValue: (k) => k.usage30d, render: (k) => <span className="t-num">{fmtNum(k.usage30d)}</span> },
                  { key: "quality", header: "Quality", align: "right", sortValue: (k) => k.quality, render: (k) => <span className="t-num">{k.quality ? `${k.quality}%` : "—"}</span> },
                ]}
              />
            )}
          </div>
        )}
      </div>
    </>
  );
}
