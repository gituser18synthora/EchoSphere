import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listKnowledge } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { StatusChip, Tabs, Callout, KpiCard, CardSkeleton } from "@/components/ui";
import { fmtNum } from "@/components/charts";

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
          <EmbeddingMonitor
            sources={q.data ?? []}
            loading={q.loading}
          />
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

/** Pipeline health computed from the live source inventory. */
function EmbeddingMonitor({ sources, loading }: { sources: import("@/types/domain").KnowledgeSource[]; loading: boolean }) {
  if (loading) return <div className="grid grid-4">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />)}</div>;
  const indexing = sources.filter((k) => k.status === "indexing");
  const failed = sources.filter((k) => k.status === "failed");
  const stale = sources.filter((k) => k.status === "stale");
  const totalChunks = sources.reduce((s, k) => s + k.chunks, 0);
  const queuedKb = indexing.reduce((s, k) => s + k.sizeKb, 0);
  return (
    <>
      {(indexing.length > 0 || failed.length > 0) && (
        <Callout tone="warning" title={failed.length ? "Failed indexing jobs need attention" : "Sources currently indexing"}>
          {indexing.length} source{indexing.length === 1 ? "" : "s"} indexing ({fmtNum(queuedKb)} KB queued)
          {failed.length ? ` · ${failed.length} failed job${failed.length === 1 ? "" : "s"}` : ""}
          {stale.length ? ` · ${stale.length} stale source${stale.length === 1 ? "" : "s"} awaiting re-sync` : ""}
        </Callout>
      )}
      <div className="grid grid-4 mt-16">
        <KpiCard label="Chunks indexed" value={fmtNum(totalChunks)} icon="database" />
        <KpiCard label="Indexing now" value={String(indexing.length)} intent="down-good" icon="clock" />
        <KpiCard label="Failed sources" value={String(failed.length)} intent="down-good" icon="x-circle" />
        <KpiCard label="Stale sources" value={String(stale.length)} intent="down-good" icon="alert" />
      </div>
    </>
  );
}
