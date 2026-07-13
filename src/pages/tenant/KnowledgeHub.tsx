import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listKnowledge, listKnowledgeGaps, simulateAction } from "@/services/api";
import { Button, KpiCard, Progress, StatusChip, CardSkeleton } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Icon, type IconName } from "@/components/Icon";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";

const typeIcon: Record<string, IconName> = { document: "file", url: "link", faq: "message", connector: "plug" };

export default function KnowledgeHub() {
  const q = useAsync(() => listKnowledge(), []);
  const botsQ = useAsync(listBots, []);
  const gapsQ = useAsync(listKnowledgeGaps, []);
  const navigate = useNavigate();
  const { toast } = useApp();
  const [type, setType] = useState("all");
  const [query, setQuery] = useState("");

  const rows = useMemo(() => {
    let r = q.data ?? [];
    if (type !== "all") r = r.filter((k) => k.type === type);
    if (query) {
      const s = query.toLowerCase();
      r = r.filter((k) => k.name.toLowerCase().includes(s) || k.detail.toLowerCase().includes(s));
    }
    return r;
  }, [q.data, type, query]);

  const sources = q.data ?? [];
  const needsAttention = sources.filter((s) => s.status === "stale" || s.status === "failed").length;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Knowledge Hub</h1>
          <p className="page-sub">Everything your bots can answer from — across all bots and shared tenant sources</p>
        </div>
        <div className="page-actions">
          <Button variant="primary" icon="upload" onClick={() => navigate("/t/bots/bot-101/knowledge")}>Add knowledge</Button>
        </div>
      </div>

      <div className="grid grid-4">
        {q.loading ? Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />) : (
          <>
            <KpiCard label="Sources" value={String(sources.length)} icon="book" />
            <KpiCard label="Indexed chunks" value={fmtNum(sources.reduce((a, s) => a + s.chunks, 0))} icon="database" />
            <KpiCard label="Retrieval hits (30d)" value={fmtNum(sources.reduce((a, s) => a + s.usage30d, 0))} icon="search" />
            <KpiCard label="Needs attention" value={String(needsAttention)} icon="alert" />
          </>
        )}
      </div>

      <div className="filter-bar mt-16">
        <div className="search-box">
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search sources…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search knowledge sources" />
        </div>
        <select className="select" value={type} onChange={(e) => setType(e.target.value)} aria-label="Filter by type">
          <option value="all">All types</option>
          <option value="document">Documents</option>
          <option value="url">URLs</option>
          <option value="faq">FAQs</option>
          <option value="connector">Connectors</option>
        </select>
      </div>

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
          onRowClick={(k) => navigate(`/t/bots/${k.botId ?? "bot-101"}/knowledge`)}
          empty={{ icon: "book", title: "No sources match", body: "Adjust the filters or add a new source." }}
          columns={[
            {
              key: "name", header: "Source", sortValue: (k) => k.name,
              render: (k) => (
                <div className="row gap-12">
                  <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name={typeIcon[k.type]} size={14} /></span>
                  <div><div className="t-strong">{k.name}</div><div className="t-micro">{k.detail}</div></div>
                </div>
              ),
            },
            {
              key: "bot", header: "Used by", sortValue: (k) => k.botId ?? "",
              render: (k) => k.scope !== "bot"
                ? <span className="tag" style={{ textTransform: "capitalize" }}>{k.scope}-wide</span>
                : <span className="t-sub">{botsQ.data?.find((b) => b.id === k.botId)?.name ?? k.botId}</span>,
            },
            { key: "status", header: "Status", sortValue: (k) => k.status, render: (k) => <StatusChip status={k.status} /> },
            { key: "chunks", header: "Chunks", align: "right", sortValue: (k) => k.chunks, render: (k) => <span className="t-num">{k.chunks ? fmtNum(k.chunks) : "—"}</span> },
            {
              key: "quality", header: "Index health", width: 150, sortValue: (k) => k.quality,
              render: (k) => k.quality ? (
                <div className="row gap-8">
                  <Progress value={k.quality} tone={k.quality > 85 ? "good" : k.quality > 65 ? "warning" : "critical"} />
                  <span className="t-num t-micro">{k.quality}%</span>
                </div>
              ) : <span className="t-micro">—</span>,
            },
            { key: "usage", header: "Hits (30d)", align: "right", sortValue: (k) => k.usage30d, render: (k) => <span className="t-num">{fmtNum(k.usage30d)}</span> },
            {
              key: "act", header: "", width: 110,
              render: (k) => (k.status === "stale" || k.status === "failed") ? (
                <Button size="sm" icon="refresh" onClick={async (e) => { e.stopPropagation(); await simulateAction("resync"); toast(`Re-sync queued for “${k.name}”`); }}>Re-sync</Button>
              ) : null,
            },
          ]}
        />
      </div>

      <div className="card mt-16">
        <div className="card-header">
          <div className="col gap-2">
            <span className="card-title">Knowledge gaps across bots</span>
            <span className="t-micro">Unanswered caller questions, ranked by frequency</span>
          </div>
        </div>
        <div className="col" style={{ padding: 16, gap: 8 }}>
          {gapsQ.loading && <CardSkeleton rows={3} />}
          {gapsQ.data?.map((g) => (
            <div key={g.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <span className="icon-tile warning" style={{ width: 30, height: 30 }}><Icon name="search" size={14} /></span>
              <div className="grow">
                <div className="t-strong" style={{ fontSize: 13 }}>“{g.question}”</div>
                <div className="t-micro">Asked {g.frequency}× in 30 days · {g.suggestedSource}</div>
              </div>
              <Button size="sm" onClick={() => navigate("/t/bots/bot-101/knowledge")}>Resolve</Button>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
