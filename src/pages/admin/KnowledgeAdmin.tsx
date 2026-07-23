/* Super Admin → Knowledge: platform-wide Knowledge Base inventory.
   Server-filtered (tenant / search / status / type) and paginated; every row
   has a View action opening the shared KnowledgeDetailDrawer with live
   document/chunk statistics fetched through the permission-checked API. */

import { useEffect, useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listKnowledgePaged, listTenants } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import {
  Button, StatusChip, Tabs, Callout, KpiCard, CardSkeleton, SearchableSelect,
} from "@/components/ui";
import { Icon } from "@/components/Icon";
import { KnowledgeDetailDrawer } from "@/components/KnowledgeDetailDrawer";
import { fmtNum } from "@/components/charts";
import type { KnowledgeSource } from "@/types/domain";

const tabs = [
  { id: "sources", label: "Knowledge Bases" },
  { id: "embeddings", label: "Embedding Monitor" },
];

function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

interface Filters {
  tenantId: string;
  status: string;
  type: string;
}
const EMPTY_FILTERS: Filters = { tenantId: "", status: "", type: "" };

export default function KnowledgeAdmin() {
  const [tab, setTab] = useState("sources");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [searchRaw, setSearchRaw] = useState("");
  const search = useDebounced(searchRaw);
  const [page, setPage] = useState(1);
  const [viewId, setViewId] = useState<string | null>(null);
  const pageSize = 25;

  const tenantsQ = useAsync(listTenants, []);

  /* Any filter change restarts from page 1; pagination itself keeps filters. */
  useEffect(() => { setPage(1); }, [filters, search]);

  const q = useAsync(
    () => listKnowledgePaged({
      tenantId: filters.tenantId || undefined,
      status: filters.status || undefined,
      type: filters.type || undefined,
      search: search || undefined,
      page, pageSize,
    }),
    [filters, search, page],
  );

  const rows = q.data?.items ?? null;
  const total = q.data?.meta.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const filtersActive = Boolean(filters.tenantId || filters.status || filters.type || search);
  const activeCount = [filters.tenantId, filters.status, filters.type, search].filter(Boolean).length;

  const tenantOptions = (tenantsQ.data ?? []).map((t) => ({
    value: t.id,
    label: t.name,
    sub: `${t.id}${t.domain ? ` · ${t.domain}` : ""}`,
  }));
  const tenantName = (id?: string | null) =>
    tenantsQ.data?.find((t) => t.id === id)?.name ?? (id ? id : "—");

  const setF = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));
  const clearFilters = () => { setFilters(EMPTY_FILTERS); setSearchRaw(""); };

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
          <EmbeddingMonitorLoader />
        ) : (
          <div className="col gap-12">
            <div className="filter-bar" style={{ marginBottom: 0 }}>
              <div style={{ minWidth: 240 }}>
                <SearchableSelect
                  ariaLabel="Filter by tenant"
                  options={tenantOptions}
                  value={filters.tenantId}
                  onChange={(tenantId) => setF({ tenantId })}
                  placeholder="All tenants"
                  searchPlaceholder="Search tenants…"
                />
              </div>
              <div className="search-box">
                <Icon name="search" size={14} />
                <input className="input" placeholder="Search knowledge bases…"
                  aria-label="Search knowledge bases"
                  value={searchRaw} onChange={(e) => setSearchRaw(e.target.value)} />
              </div>
              <select className="select" value={filters.status} aria-label="Filter by status"
                onChange={(e) => setF({ status: e.target.value })}>
                <option value="">All statuses</option>
                <option value="indexed">Indexed</option>
                <option value="indexing">Indexing</option>
                <option value="pending">Pending</option>
                <option value="stale">Stale</option>
                <option value="failed">Failed</option>
              </select>
              <select className="select" value={filters.type} aria-label="Filter by source type"
                onChange={(e) => setF({ type: e.target.value })}>
                <option value="">All types</option>
                <option value="document">Documents</option>
                <option value="url">URLs</option>
                <option value="faq">FAQs</option>
                <option value="connector">Connectors</option>
              </select>
              {filtersActive && (
                <span className="row gap-6" style={{ alignItems: "center" }}>
                  <span className="tag">{activeCount} filter{activeCount === 1 ? "" : "s"} active</span>
                  <Button size="sm" variant="ghost" icon="x" onClick={clearFilters}>Clear filters</Button>
                </span>
              )}
            </div>

            <div className="card">
              <DataTable<KnowledgeSource>
                loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
                rowKey={(k) => k.id}
                onRowClick={(k) => setViewId(k.id)}
                empty={
                  filtersActive
                    ? { icon: "filter", title: "No knowledge bases match the current filters", body: "Adjust or clear the filters to see more results." }
                    : { icon: "book", title: "No knowledge bases yet" }
                }
                columns={[
                  { key: "name", header: "Knowledge Base", sortValue: (k) => k.name, render: (k) => <div><div className="t-strong">{k.name}</div><div className="t-micro">{k.detail || k.id}</div></div> },
                  {
                    key: "tenant", header: "Tenant",
                    render: (k) => k.scope === "global"
                      ? <span className="tag">Global</span>
                      : <span>{tenantName(k.tenantId)}</span>,
                  },
                  { key: "scope", header: "Scope", sortValue: (k) => k.scope, render: (k) => <span className="tag" style={{ textTransform: "capitalize" }}>{k.scope}</span> },
                  { key: "type", header: "Type", sortValue: (k) => k.type, render: (k) => <span className="tag" style={{ textTransform: "capitalize" }}>{k.type}</span> },
                  { key: "status", header: "Index status", sortValue: (k) => k.status, render: (k) => <StatusChip status={k.status} /> },
                  { key: "chunks", header: "Chunks", align: "right", sortValue: (k) => k.chunks, render: (k) => <span className="t-num">{fmtNum(k.chunks)}</span> },
                  { key: "usage", header: "Hits (30d)", align: "right", sortValue: (k) => k.usage30d, render: (k) => <span className="t-num">{fmtNum(k.usage30d)}</span> },
                  {
                    key: "actions", header: "", align: "right", width: 90,
                    render: (k) => (
                      <span onClick={(e) => e.stopPropagation()}>
                        <Button size="sm" icon="eye" onClick={() => setViewId(k.id)}>View</Button>
                      </span>
                    ),
                  },
                ]}
                footer={
                  totalPages > 1 ? (
                    <div className="row gap-8" style={{ justifyContent: "flex-end", alignItems: "center", padding: "8px 4px" }}>
                      <span className="t-micro">{fmtNum(total)} total · page {page} of {totalPages}</span>
                      <Button size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</Button>
                      <Button size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</Button>
                    </div>
                  ) : undefined
                }
              />
            </div>
          </div>
        )}
      </div>

      {viewId && <KnowledgeDetailDrawer sourceId={viewId} onClose={() => setViewId(null)} />}
    </>
  );
}

/* ---------- Embedding monitor (pipeline health from live inventory) ---------- */

function EmbeddingMonitorLoader() {
  const q = useAsync(() => listKnowledgePaged({ pageSize: 200 }), []);
  if (q.loading) return <div className="grid grid-4">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />)}</div>;
  const sources = q.data?.items ?? [];
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
