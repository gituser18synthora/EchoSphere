import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listKnowledge, listKnowledgeGaps, resyncKnowledge } from "@/services/api";
import { Button, KpiCard, Progress, StatusChip, CardSkeleton, EmptyState, ErrorState } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { Icon, type IconName } from "@/components/Icon";
import { RetrievalTester } from "@/components/RetrievalTester";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import type { KnowledgeSource } from "@/types/domain";

const typeIcon: Record<string, IconName> = { document: "file", url: "link", faq: "message", connector: "plug" };

const VIEW_KEY = "echosphere.knowledgeView";

function fmtDate(iso?: string | null): string {
  if (!iso || iso === "—") return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

export default function KnowledgeHub() {
  const q = useAsync(() => listKnowledge(), []);
  const botsQ = useAsync(listBots, []);
  const gapsQ = useAsync(listKnowledgeGaps, []);
  const navigate = useNavigate();
  const { toast } = useApp();
  const [type, setType] = useState("all");
  const [status, setStatus] = useState("all");
  const [query, setQuery] = useState("");
  const [view, setView] = useState<"cards" | "table">(
    () => (localStorage.getItem(VIEW_KEY) as "cards" | "table") || "cards",
  );

  const switchView = (v: "cards" | "table") => {
    setView(v);
    localStorage.setItem(VIEW_KEY, v);
  };

  // Dedupe by id — a source must never appear twice regardless of how the
  // API composes bot/tenant/global scopes.
  const sources = useMemo(() => {
    const seen = new Set<string>();
    return (q.data ?? []).filter((k) => !seen.has(k.id) && seen.add(k.id));
  }, [q.data]);

  const rows = useMemo(() => {
    let r = sources;
    if (type !== "all") r = r.filter((k) => k.type === type);
    if (status !== "all") r = r.filter((k) => k.status === status);
    if (query) {
      const s = query.toLowerCase();
      r = r.filter((k) =>
        k.name.toLowerCase().includes(s) || k.detail.toLowerCase().includes(s) || k.id.toLowerCase().includes(s));
    }
    return r;
  }, [sources, type, status, query]);

  const needsAttention = sources.filter((s) => s.status === "stale" || s.status === "failed").length;
  const firstBotId = botsQ.data?.[0]?.id;

  const resync = async (k: KnowledgeSource) => {
    try {
      await resyncKnowledge(k.id);
      toast(`Re-sync queued for “${k.name}”`);
      q.reload();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Re-sync failed", "error");
    }
  };

  const openSource = (k: KnowledgeSource) => {
    const target = k.botId ?? firstBotId;
    if (target) navigate(`/t/bots/${target}/knowledge`);
  };

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Knowledge Hub</h1>
          <p className="page-sub">Everything your bots can answer from — across all bots and shared tenant sources</p>
        </div>
        <div className="page-actions">
          <Button variant="primary" icon="upload" disabled={!firstBotId} onClick={() => firstBotId && navigate(`/t/bots/${firstBotId}/knowledge`)}>Add knowledge</Button>
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

      {/* ── Search sources ── */}
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
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
          <option value="all">All statuses</option>
          <option value="indexed">Indexed</option>
          <option value="indexing">Indexing</option>
          <option value="pending">Pending</option>
          <option value="stale">Stale</option>
          <option value="failed">Failed</option>
        </select>
        <div className="segmented" role="group" aria-label="View" style={{ marginLeft: "auto" }}>
          <button aria-pressed={view === "cards"} onClick={() => switchView("cards")} title="Card view" aria-label="Card view"><Icon name="layers" size={14} /></button>
          <button aria-pressed={view === "table"} onClick={() => switchView("table")} title="Table view" aria-label="Table view"><Icon name="menu" size={14} /></button>
        </div>
      </div>

      {view === "cards" ? (
        <SourceCards
          loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
          botName={(id?: string) => botsQ.data?.find((b) => b.id === id)?.name}
          onOpen={openSource} onResync={resync}
        />
      ) : (
        <div className="card">
          <DataTable
            loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
            onRowClick={openSource}
            empty={{ icon: "book", title: "No sources match", body: "Adjust the filters or add a new source." }}
            columns={[
              {
                key: "name", header: "Source", sortValue: (k) => k.name,
                render: (k) => (
                  <div className="row gap-12">
                    <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name={typeIcon[k.type]} size={14} /></span>
                    <div><div className="t-strong">{k.name}</div><div className="t-micro">{k.detail || k.id}</div></div>
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
              { key: "created", header: "Created", sortValue: (k) => k.createdAt ?? "", render: (k) => <span className="t-sub">{fmtDate(k.createdAt)}</span> },
              {
                key: "act", header: "", width: 110,
                render: (k) => (k.status === "stale" || k.status === "failed") ? (
                  <Button size="sm" icon="refresh" onClick={(e) => { e.stopPropagation(); void resync(k); }}>Re-sync</Button>
                ) : null,
              },
            ]}
          />
        </div>
      )}

      {/* ── Test retrieval across the tenant's knowledge ── */}
      <div className="card mt-16">
        <div className="card-header">
          <div className="col gap-2">
            <span className="card-title">Test retrieval</span>
            <span className="t-micro">Run a query against your indexed knowledge — pick specific knowledge bases or search them all</span>
          </div>
        </div>
        <div style={{ padding: 16 }}>
          <RetrievalTester kbOptions={sources.filter((s) => s.status === "indexed" || s.status === "stale")} />
        </div>
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
              <Button size="sm" disabled={!firstBotId} onClick={() => firstBotId && navigate(`/t/bots/${firstBotId}/knowledge`)}>Resolve</Button>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

/* ---------- Card view of knowledge sources ---------- */

function SourceCards({ loading, error, onRetry, rows, botName, onOpen, onResync }: {
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  rows: KnowledgeSource[];
  botName: (id?: string) => string | undefined;
  onOpen: (k: KnowledgeSource) => void;
  onResync: (k: KnowledgeSource) => void;
}) {
  if (loading) {
    return (
      <div className="grid grid-3">
        {Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}
      </div>
    );
  }
  if (error) {
    return <div className="card"><ErrorState message={error} onRetry={onRetry} /></div>;
  }
  if (rows.length === 0) {
    return (
      <div className="card">
        <EmptyState icon="book" title="No sources match" body="Adjust the filters or add a new source." />
      </div>
    );
  }
  return (
    <div className="grid grid-3">
      {rows.map((k) => (
        <div
          key={k.id}
          className="card card-pad col gap-10"
          role="button"
          tabIndex={0}
          style={{ cursor: "pointer" }}
          onClick={() => onOpen(k)}
          onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(k); } }}
          aria-label={`Open ${k.name}`}
        >
          <div className="row-between gap-8">
            <div className="row gap-10" style={{ minWidth: 0 }}>
              <span className="icon-tile neutral" style={{ width: 32, height: 32, flexShrink: 0 }}><Icon name={typeIcon[k.type]} size={15} /></span>
              <div style={{ minWidth: 0 }}>
                <div className="t-strong truncate" title={k.name}>{k.name}</div>
                <div className="t-micro truncate" title={k.detail || k.id}>{k.detail || "—"}</div>
              </div>
            </div>
            <StatusChip status={k.status} />
          </div>
          <div className="row gap-6 wrap">
            <span className="tag" style={{ textTransform: "capitalize" }}>{k.type}</span>
            <span className="tag" style={{ textTransform: "capitalize" }}>
              {k.scope === "bot" ? (botName(k.botId) ?? "bot") : `${k.scope}-wide`}
            </span>
            <span className="tag t-num" title="Knowledge base id">{k.id}</span>
          </div>
          <div className="row gap-16 wrap t-micro">
            <span className="t-num"><Icon name="database" size={12} /> {k.chunks ? `${fmtNum(k.chunks)} chunks` : "no chunks"}</span>
            <span className="t-num"><Icon name="search" size={12} /> {fmtNum(k.usage30d)} hits</span>
            {k.sizeKb > 0 && <span className="t-num">{fmtNum(k.sizeKb)} KB</span>}
          </div>
          <div className="row-between t-micro" style={{ borderTop: "1px solid var(--hairline)", paddingTop: 8 }}>
            <span>Created {fmtDate(k.createdAt)}</span>
            <span>{k.lastSync === "—" ? "never synced" : `synced ${fmtDate(k.lastSync)}`}</span>
          </div>
          {(k.status === "stale" || k.status === "failed") && (
            <Button size="sm" icon="refresh" style={{ alignSelf: "flex-start" }}
              onClick={(e) => { e.stopPropagation(); onResync(k); }}>
              Re-sync
            </Button>
          )}
        </div>
      ))}
    </div>
  );
}
