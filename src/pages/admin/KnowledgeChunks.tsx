/* Super Admin → Knowledge Management → Chunk Review.
   Inspect uploaded documents and the chunks generated from them across every
   tenant. All data comes from the secured /admin/knowledge/review APIs with
   server-side filtering, sorting and pagination — nothing is computed here and
   embeddings are never fetched. */

import { useEffect, useState } from "react";
import { useApp } from "@/state/AppContext";
import { Icon, type IconName } from "@/components/Icon";
import {
  Button, Callout, ConfirmModal, Drawer, EmptyState, KpiCard, MenuButton,
  Progress, StatusChip, Toggle, type MenuAction,
} from "@/components/ui";
import { DataTable, type Column } from "@/components/DataTable";
import { JsonView } from "@/components/JsonView";
import { useAsync } from "@/hooks/useAsync";
import * as api from "@/services/api";
import type {
  ChunkWarnings, ReviewChunk, ReviewChunkDetail, ReviewDocument,
  ReviewDocumentDetail, ReviewFacets, RetrievalTestResult,
} from "@/types/domain";

const PAGE_SIZES = [25, 50, 100, 200];

/* ── small helpers ─────────────────────────────────────────────────────── */

function useDebounced<T>(value: T, ms = 350): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

const fmtBytes = (n: number): string => {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
};
const fmtDate = (iso: string | null): string =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

const WARNING_META: { key: keyof ChunkWarnings; label: string; icon: IconName; tone: string }[] = [
  { key: "flaggedForReview", label: "Flagged", icon: "star", tone: "chip-warning" },
  { key: "promptInjection", label: "Injection", icon: "shield", tone: "chip-critical" },
  { key: "emptyChunk", label: "Empty", icon: "alert", tone: "chip-critical" },
  { key: "shortChunk", label: "Short", icon: "alert", tone: "chip-warning" },
  { key: "missingPage", label: "No page", icon: "file", tone: "chip-neutral" },
  { key: "missingSection", label: "No section", icon: "file", tone: "chip-neutral" },
  { key: "ocr", label: "OCR", icon: "eye", tone: "chip-info" },
  { key: "table", label: "Table", icon: "layers", tone: "chip-info" },
  { key: "fromImage", label: "Image", icon: "eye", tone: "chip-info" },
];

function WarningBadges({ warnings, extraPii }: { warnings: ChunkWarnings; extraPii?: boolean }) {
  const active = WARNING_META.filter((w) => warnings[w.key]);
  if (!active.length && !extraPii) return <span className="t-micro t-sub">—</span>;
  return (
    <span className="row gap-4" style={{ flexWrap: "wrap" }}>
      {active.map((w) => (
        <span key={w.key} className={`chip ${w.tone}`} title={w.label}>
          <Icon name={w.icon} size={11} /> {w.label}
        </span>
      ))}
      {extraPii && (
        <span className="chip chip-critical" title="Possible PII detected">
          <Icon name="lock" size={11} /> PII
        </span>
      )}
    </span>
  );
}

function Pagination({
  page, pageSize, total, onPage, onPageSize,
}: {
  page: number; pageSize: number; total: number;
  onPage: (p: number) => void; onPageSize: (s: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);
  return (
    <div className="row-between" style={{ padding: "10px 4px" }}>
      <span className="t-micro t-sub">
        {from}–{to} of {total.toLocaleString()}
      </span>
      <div className="row gap-8">
        <select
          className="select"
          value={pageSize}
          onChange={(e) => onPageSize(Number(e.target.value))}
          aria-label="Rows per page"
        >
          {PAGE_SIZES.map((s) => (
            <option key={s} value={s}>{s} / page</option>
          ))}
        </select>
        <Button size="sm" variant="secondary" icon="chevron-left" disabled={page <= 1}
          onClick={() => onPage(page - 1)}>Prev</Button>
        <span className="t-micro" style={{ alignSelf: "center" }}>
          Page {page} / {totalPages}
        </span>
        <Button size="sm" variant="secondary" disabled={page >= totalPages}
          onClick={() => onPage(page + 1)}>Next</Button>
      </div>
    </div>
  );
}

/* ── page ──────────────────────────────────────────────────────────────── */

export default function KnowledgeChunks() {
  const { hasPermission } = useApp();
  const canReview = hasPermission("review_knowledge_chunks");
  const facetsQ = useAsync(() => api.reviewFacets(), []);
  const [activeDoc, setActiveDoc] = useState<ReviewDocument | null>(null);
  const [detailDocId, setDetailDocId] = useState<string | null>(null);
  const [chunkId, setChunkId] = useState<string | null>(null);
  const [retrievalScope, setRetrievalScope] = useState<{ documentId?: string; kbId?: string; label: string } | null>(null);

  if (!canReview) {
    return (
      <div className="col gap-16">
        <div className="page-head">
          <div className="page-head-titles">
            <h1 className="page-title">Chunk Review</h1>
          </div>
        </div>
        <Callout tone="critical" title="Access denied">
          You need the <code>review_knowledge_chunks</code> permission to view knowledge documents
          and chunks. Ask a platform administrator for access.
        </Callout>
      </div>
    );
  }

  const facets = facetsQ.data;

  return (
    <div className="col gap-16">
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Knowledge Management · Chunk Review</h1>
          <p className="page-sub">
            Inspect uploaded documents and the chunks generated from them across every tenant —
            verify boundaries, quality and retrieval, and curate safely.
          </p>
        </div>
      </div>

      {activeDoc ? (
        <ChunkExplorer
          doc={activeDoc}
          onBack={() => setActiveDoc(null)}
          languages={facets?.languages ?? []}
          onOpenChunk={setChunkId}
          onTestRetrieval={() =>
            setRetrievalScope({ documentId: activeDoc.documentId, kbId: activeDoc.kbId, label: activeDoc.fileName })
          }
        />
      ) : (
        <DocumentsPanel
          facets={facets}
          facetsLoading={facetsQ.loading}
          onReviewChunks={setActiveDoc}
          onOpenDetail={setDetailDocId}
          onTestRetrieval={(d) =>
            setRetrievalScope({ documentId: d.documentId, kbId: d.kbId, label: d.fileName })
          }
        />
      )}

      {detailDocId && (
        <DocumentDetailDrawer
          documentId={detailDocId}
          onClose={() => setDetailDocId(null)}
          onReviewChunks={(d) => { setDetailDocId(null); setActiveDoc(d); }}
          onTestRetrieval={(d) => setRetrievalScope({ documentId: d.documentId, kbId: d.kbId, label: d.fileName })}
        />
      )}
      {chunkId && <ChunkDetailDrawer chunkId={chunkId} onNavigate={setChunkId} onClose={() => setChunkId(null)} />}
      {retrievalScope && (
        <RetrievalTestDrawer scope={retrievalScope} onClose={() => setRetrievalScope(null)} />
      )}
    </div>
  );
}

/* ── documents panel ───────────────────────────────────────────────────── */

interface DocFilters {
  tenantId: string; kbId: string; fileType: string; status: string;
  ingestionStatus: string; failedOnly: boolean; includeArchived: boolean;
  uploadedFrom: string; uploadedTo: string;
}
const EMPTY_DOC_FILTERS: DocFilters = {
  tenantId: "", kbId: "", fileType: "", status: "", ingestionStatus: "",
  failedOnly: false, includeArchived: false, uploadedFrom: "", uploadedTo: "",
};

function DocumentsPanel({
  facets, facetsLoading, onReviewChunks, onOpenDetail, onTestRetrieval,
}: {
  facets: ReviewFacets | null;
  facetsLoading: boolean;
  onReviewChunks: (d: ReviewDocument) => void;
  onOpenDetail: (id: string) => void;
  onTestRetrieval: (d: ReviewDocument) => void;
}) {
  const { toast } = useApp();
  const [filters, setFilters] = useState<DocFilters>(EMPTY_DOC_FILTERS);
  const [searchRaw, setSearchRaw] = useState("");
  const search = useDebounced(searchRaw);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [sortBy, setSortBy] = useState("createdAt");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [confirmArchive, setConfirmArchive] = useState<ReviewDocument | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => { setPage(1); }, [filters, search, pageSize, sortBy, sortDir]);

  const q = useAsync(
    () => api.reviewDocuments({
      ...filters, search, page, pageSize, sortBy, sortDir,
      fileType: filters.fileType || undefined, status: filters.status || undefined,
      ingestionStatus: filters.ingestionStatus || undefined,
      tenantId: filters.tenantId || undefined, kbId: filters.kbId || undefined,
      // Temporarily hidden from the Knowledge Chunks filter UI — the uploaded
      // date-range filter is not sent while hidden. Backend support is kept
      // for possible future re-enablement.
      uploadedFrom: undefined, uploadedTo: undefined,
    }),
    [filters, search, page, pageSize, sortBy, sortDir],
  );

  const runAction = async (
    label: string, id: string, fn: () => Promise<unknown>,
  ) => {
    setBusyId(id);
    try { await fn(); toast(label, "good"); q.reload(); }
    catch (e) { toast(e instanceof Error ? e.message : "Action failed", "error"); }
    finally { setBusyId(null); }
  };
  const downloadOriginal = async (document: ReviewDocument) => {
    if (busyId === document.documentId) return;
    setBusyId(document.documentId);
    try {
      await api.downloadReviewDocument(document.documentId, document.fileName);
      toast(`Downloaded ${document.fileName}`, "good");
    } catch (error) {
      toast(error instanceof Error ? error.message : "Download failed", "error");
    } finally {
      setBusyId(null);
    }
  };

  const setF = (patch: Partial<DocFilters>) => setFilters((f) => ({ ...f, ...patch }));
  const reset = () => { setFilters(EMPTY_DOC_FILTERS); setSearchRaw(""); };

  const columns: Column<ReviewDocument>[] = [
    {
      key: "fileName", header: "Document",
      render: (d) => (
        <div className="col" style={{ gap: 2 }}>
          <span className="t-strong row gap-4">
            <Icon name="file" size={13} /> {d.fileName}
          </span>
          <span className="t-micro t-sub">.{d.fileExt} · {fmtBytes(d.sizeBytes)}</span>
        </div>
      ),
    },
    {
      key: "tenant", header: "Tenant",
      render: (d) => (
        <div className="col" style={{ gap: 2 }}>
          <span className="t-body">{d.tenantName ?? "—"}</span>
          {d.tenantCode && <span className="t-micro t-sub">{d.tenantCode}</span>}
        </div>
      ),
    },
    { key: "kbName", header: "Knowledge Base", render: (d) => <span className="t-body">{d.kbName ?? d.kbId}</span> },
    { key: "status", header: "Status", render: (d) => <StatusChip status={d.status} /> },
    { key: "chunkCount", header: "Chunks", align: "right", render: (d) => <span className="t-num">{d.chunkCount}</span> },
    { key: "pageCount", header: "Pages", align: "right", render: (d) => <span className="t-num">{d.pageCount}</span> },
    {
      key: "uploadedAt", header: "Uploaded",
      render: (d) => (
        <div className="col" style={{ gap: 2 }}>
          <span className="t-body">{fmtDate(d.uploadedAt)}</span>
          {d.uploadedByName && <span className="t-micro t-sub">{d.uploadedByName}</span>}
        </div>
      ),
    },
    {
      key: "actions", header: "", align: "right", width: 44,
      render: (d) => {
        const actions: (MenuAction | "sep")[] = [
          { label: "View details", icon: "eye", onClick: () => onOpenDetail(d.documentId) },
          { label: "Review chunks", icon: "database", onClick: () => onReviewChunks(d) },
          { label: "Test retrieval", icon: "search", onClick: () => onTestRetrieval(d) },
          "sep",
          {
            label: "Retry ingestion", icon: "refresh",
            disabled: !["failed", "cancelled"].includes(d.status),
            onClick: () => runAction("Ingestion retried", d.documentId, () => api.retryReviewDocument(d.documentId)),
          },
          {
            label: "Reindex document", icon: "redo",
            onClick: () => runAction("Reindex queued", d.documentId, () => api.reindexReviewDocument(d.documentId)),
          },
          {
            label: busyId === d.documentId ? "Downloading original…" : "Download original",
            icon: "download",
            disabled: d.isDeleted || busyId === d.documentId,
            onClick: () => void downloadOriginal(d),
          },
          "sep",
          {
            label: d.isDeleted ? "Already archived" : "Archive / delete", icon: "trash", danger: true,
            disabled: d.isDeleted,
            onClick: () => setConfirmArchive(d),
          },
        ];
        return <MenuButton actions={actions} />;
      },
    },
  ];

  return (
    <div className="col gap-12">
      {/* filters — labeled cells in one aligned, responsive grid */}
      <div className="card card-pad col gap-12">
        <div className="filter-grid">
          <div className="filter-cell filter-cell-wide">
            <span className="filter-label">Search</span>
            <input
              className="input" placeholder="File name or document ID…"
              aria-label="Search documents"
              value={searchRaw} onChange={(e) => setSearchRaw(e.target.value)}
            />
          </div>
          <div className="filter-cell">
            <span className="filter-label">Tenant</span>
            <select className="select" aria-label="Filter by tenant" value={filters.tenantId}
              onChange={(e) => setF({ tenantId: e.target.value, kbId: "" })}>
              <option value="">All tenants</option>
              {facets?.tenants.map((t) => (
                <option key={t.id} value={t.id}>{t.name}{t.code ? ` (${t.code})` : ""}</option>
              ))}
            </select>
          </div>
          <div className="filter-cell">
            <span className="filter-label">File type</span>
            <select className="select" aria-label="Filter by file type" value={filters.fileType}
              onChange={(e) => setF({ fileType: e.target.value })}>
              <option value="">All types</option>
              {facets?.fileTypes.map((t) => <option key={t} value={t}>.{t}</option>)}
            </select>
          </div>
          <div className="filter-cell">
            <span className="filter-label">Status</span>
            <select className="select" aria-label="Filter by status" value={filters.status}
              onChange={(e) => setF({ status: e.target.value })}>
              <option value="">All statuses</option>
              {facets?.uploadStatuses.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className="filter-cell">
            <span className="filter-label">Ingestion</span>
            <select className="select" aria-label="Filter by ingestion state" value={filters.ingestionStatus}
              onChange={(e) => setF({ ingestionStatus: e.target.value })}>
              <option value="">All ingestion states</option>
              {facets?.ingestionStatuses.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          {/* Temporarily hidden from the Knowledge Chunks filter UI.
              Keep backend support for possible future re-enablement.
          <div className="filter-cell">
            <span className="filter-label">Uploaded from</span>
            <input type="date" className="input" value={filters.uploadedFrom}
              onChange={(e) => setF({ uploadedFrom: e.target.value })} />
          </div>
          <div className="filter-cell">
            <span className="filter-label">Uploaded to</span>
            <input type="date" className="input" value={filters.uploadedTo}
              onChange={(e) => setF({ uploadedTo: e.target.value })} />
          </div>
          */}
        </div>
        <div className="row gap-12" style={{ flexWrap: "wrap", alignItems: "center" }}>
          <label className="filter-cell-toggle row gap-6" style={{ alignItems: "center" }}>
            <Toggle checked={filters.failedOnly} onChange={(v) => setF({ failedOnly: v })} label="Failed only" />
            Failed / incomplete only
          </label>
          <label className="filter-cell-toggle row gap-6" style={{ alignItems: "center" }}>
            <Toggle checked={filters.includeArchived} onChange={(v) => setF({ includeArchived: v })} label="Include archived" />
            Include archived
          </label>
          <div className="row gap-8" style={{ marginLeft: "auto", flexWrap: "wrap" }}>
            <select className="select" value={sortBy} onChange={(e) => setSortBy(e.target.value)} aria-label="Sort by">
              <option value="createdAt">Sort: Uploaded</option>
              <option value="fileName">Sort: Name</option>
              <option value="chunkCount">Sort: Chunks</option>
              <option value="pageCount">Sort: Pages</option>
              <option value="sizeBytes">Sort: Size</option>
              <option value="status">Sort: Status</option>
            </select>
            <Button size="sm" variant="secondary" icon={sortDir === "desc" ? "arrow-down" : "arrow-up"}
              onClick={() => setSortDir((d) => (d === "desc" ? "asc" : "desc"))}>
              {sortDir === "desc" ? "Desc" : "Asc"}
            </Button>
            <Button size="sm" variant="ghost" icon="undo" onClick={reset}>Reset</Button>
          </div>
        </div>
      </div>

      <div className="card">
        <DataTable
          columns={columns}
          rows={q.data?.items ?? null}
          loading={q.loading || facetsLoading}
          error={q.error}
          onRetry={q.reload}
          onRowClick={(d) => onOpenDetail(d.documentId)}
          rowKey={(d) => d.documentId}
          empty={{ icon: "file", title: "No documents match", body: "Adjust filters or clear the search to see uploaded documents." }}
          footer={
            q.data ? (
              <Pagination page={page} pageSize={pageSize} total={q.data.meta.total}
                onPage={setPage} onPageSize={setPageSize} />
            ) : null
          }
        />
      </div>

      <ConfirmModal
        open={!!confirmArchive}
        onClose={() => setConfirmArchive(null)}
        danger
        confirmLabel="Archive document"
        title="Archive this document?"
        busy={busyId === confirmArchive?.documentId}
        body={
          <>
            <strong>{confirmArchive?.fileName}</strong> and its {confirmArchive?.chunkCount} chunk(s)
            will be archived and removed from retrieval. This is recoverable by a re-index. Continue?
          </>
        }
        onConfirm={() => {
          const d = confirmArchive!;
          runAction("Document archived", d.documentId, () => api.archiveReviewDocument(d.documentId))
            .finally(() => setConfirmArchive(null));
        }}
      />
    </div>
  );
}

/* ── chunk explorer (per document) ─────────────────────────────────────── */

type ChunkView = "table" | "expanded" | "byPage" | "bySection";

function ChunkExplorer({
  doc, onBack, languages, onOpenChunk, onTestRetrieval,
}: {
  doc: ReviewDocument;
  onBack: () => void;
  languages: string[];
  onOpenChunk: (id: string) => void;
  onTestRetrieval: () => void;
}) {
  const { toast } = useApp();
  const detailQ = useAsync(() => api.getReviewDocument(doc.documentId), [doc.documentId]);
  const [view, setView] = useState<ChunkView>("table");
  const [searchRaw, setSearchRaw] = useState("");
  const search = useDebounced(searchRaw);
  const [status, setStatus] = useState("");
  const [language, setLanguage] = useState("");
  const [pageNumber, setPageNumber] = useState("");
  const [section, setSection] = useState("");
  const [minTokens, setMinTokens] = useState("");
  const [maxTokens, setMaxTokens] = useState("");
  const [hasKeywords, setHasKeywords] = useState(false);
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const sectionSearch = useDebounced(section);
  const [reloadTick, setReloadTick] = useState(0);

  const deps = [search, status, language, pageNumber, sectionSearch, minTokens, maxTokens,
    hasKeywords, flaggedOnly, page, pageSize, reloadTick];
  useEffect(() => { setPage(1); },
    [search, status, language, pageNumber, sectionSearch, minTokens, maxTokens, hasKeywords, flaggedOnly, pageSize]);

  const q = useAsync(
    () => api.reviewChunks({
      documentId: doc.documentId, search, page, pageSize,
      status: status || undefined, language: language || undefined,
      pageNumber: pageNumber ? Number(pageNumber) : undefined,
      section: sectionSearch || undefined,
      minTokens: minTokens ? Number(minTokens) : undefined,
      maxTokens: maxTokens ? Number(maxTokens) : undefined,
      hasKeywords: hasKeywords || undefined,
      flaggedOnly: flaggedOnly || undefined,
    }),
    deps,
  );

  const reload = () => setReloadTick((t) => t + 1);
  const detail = detailQ.data as ReviewDocumentDetail | null;

  const toggleStatus = async (c: ReviewChunk) => {
    const next = c.status === "active" ? "archived" : "active";
    try { await api.setChunkStatus(c.chunkId, next); toast(`Chunk marked ${next}`, "good"); reload(); }
    catch (e) { toast(e instanceof Error ? e.message : "Failed", "error"); }
  };
  const flag = async (c: ReviewChunk) => {
    try {
      const on = !c.warnings.flaggedForReview;
      await api.flagChunk(c.chunkId, on, on ? "Flagged from review console" : undefined);
      toast(on ? "Chunk flagged for review" : "Flag cleared", "good"); reload();
    } catch (e) { toast(e instanceof Error ? e.message : "Failed", "error"); }
  };
  const copy = (c: ReviewChunk) => {
    navigator.clipboard.writeText(c.content).then(
      () => toast("Chunk content copied", "good"),
      () => toast("Copy failed", "error"),
    );
  };
  const reprocess = () =>
    api.reindexReviewDocument(doc.documentId).then(
      () => toast("Reindex queued for this document", "good"),
      (e) => toast(e instanceof Error ? e.message : "Failed", "error"),
    );

  const chunkMenu = (c: ReviewChunk): (MenuAction | "sep")[] => [
    { label: "View full chunk", icon: "eye", onClick: () => onOpenChunk(c.chunkId) },
    { label: "Copy content", icon: "copy", onClick: () => copy(c) },
    "sep",
    {
      label: c.status === "active" ? "Mark inactive" : "Mark active",
      icon: c.status === "active" ? "x-circle" : "check-circle",
      onClick: () => toggleStatus(c),
    },
    { label: c.warnings.flaggedForReview ? "Clear review flag" : "Flag for review", icon: "star", onClick: () => flag(c) },
    { label: "Reprocess document", icon: "redo", onClick: reprocess },
  ];

  const chunks = q.data?.items ?? [];

  const columns: Column<ReviewChunk>[] = [
    { key: "chunkIndex", header: "#", align: "right", width: 56, render: (c) => <span className="t-num">{c.chunkIndex}</span> },
    { key: "pageNumber", header: "Page", align: "right", width: 60, render: (c) => <span className="t-num">{c.pageNumber ?? "—"}</span> },
    {
      key: "section", header: "Section",
      render: (c) => (
        <span className="t-body truncate" style={{ display: "block", maxWidth: 160 }} title={c.section ?? undefined}>
          {c.section || <em className="t-sub">none</em>}
        </span>
      ),
    },
    {
      key: "content", header: "Content preview",
      // Clamped to two lines — the full text lives in the chunk View drawer,
      // so long chunks can never blow up the row height or table layout.
      render: (c) => (
        <span
          className="t-body"
          style={{
            display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical",
            overflow: "hidden", maxWidth: 460, overflowWrap: "anywhere",
          }}
        >
          {c.contentPreview}{c.content.length > c.contentPreview.length ? "…" : ""}
        </span>
      ),
    },
    { key: "tokenCount", header: "Tokens", align: "right", width: 72, render: (c) => <span className="t-num">{c.tokenCount ?? "—"}</span> },
    { key: "status", header: "Status", width: 96, render: (c) => <StatusChip status={c.status} /> },
    { key: "warnings", header: "Flags", render: (c) => <WarningBadges warnings={c.warnings} /> },
    { key: "actions", header: "", align: "right", width: 44, render: (c) => <MenuButton actions={chunkMenu(c)} /> },
  ];

  return (
    <div className="col gap-12">
      <div className="row gap-8">
        <Button variant="ghost" icon="chevron-left" onClick={onBack}>Back to documents</Button>
        <Button variant="secondary" icon="search" onClick={onTestRetrieval} style={{ marginLeft: "auto" }}>Test retrieval</Button>
        <Button variant="secondary" icon="redo" onClick={reprocess}>Reindex document</Button>
      </div>

      {/* document summary */}
      <DocumentSummary doc={doc} detail={detail} loading={detailQ.loading} />

      {/* chunk filters — labeled cells in one aligned, responsive grid */}
      <div className="card card-pad col gap-12">
        <div className="filter-grid">
          <div className="filter-cell filter-cell-wide">
            <span className="filter-label">Search</span>
            <input className="input" placeholder="Content, section, topic, keywords or chunk ID…"
              aria-label="Search chunks"
              value={searchRaw} onChange={(e) => setSearchRaw(e.target.value)} />
          </div>
          <div className="filter-cell">
            <span className="filter-label">Status</span>
            <select className="select" aria-label="Filter by chunk status" value={status}
              onChange={(e) => setStatus(e.target.value)}>
              <option value="">All chunks</option>
              <option value="active">Active</option>
              <option value="archived">Archived</option>
            </select>
          </div>
          <div className="filter-cell">
            <span className="filter-label">Language</span>
            <select className="select" aria-label="Filter by language" value={language}
              onChange={(e) => setLanguage(e.target.value)}>
              <option value="">Any language</option>
              {languages.map((l) => <option key={l} value={l}>{l}</option>)}
            </select>
          </div>
          <div className="filter-cell">
            <span className="filter-label">Page #</span>
            <input className="input" type="number" min={0} placeholder="Any"
              aria-label="Filter by page number"
              value={pageNumber} onChange={(e) => setPageNumber(e.target.value)} />
          </div>
          <div className="filter-cell">
            <span className="filter-label">Section</span>
            <input className="input" placeholder="Contains…" aria-label="Filter by section"
              value={section} onChange={(e) => setSection(e.target.value)} />
          </div>
          <div className="filter-cell">
            <span className="filter-label">Tokens ≥</span>
            <input className="input" type="number" min={0} placeholder="Min" aria-label="Minimum tokens"
              value={minTokens} onChange={(e) => setMinTokens(e.target.value)} />
          </div>
          <div className="filter-cell">
            <span className="filter-label">Tokens ≤</span>
            <input className="input" type="number" min={0} placeholder="Max" aria-label="Maximum tokens"
              value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} />
          </div>
        </div>
        <div className="row gap-12" style={{ flexWrap: "wrap", alignItems: "center" }}>
          <label className="filter-cell-toggle row gap-6" style={{ alignItems: "center" }}>
            <Toggle checked={hasKeywords} onChange={setHasKeywords} label="Has keywords" /> Has keywords
          </label>
          <label className="filter-cell-toggle row gap-6" style={{ alignItems: "center" }}>
            <Toggle checked={flaggedOnly} onChange={setFlaggedOnly} label="Flagged only" /> Flagged for review
          </label>
          <div className="segmented" role="group" aria-label="Chunk view" style={{ marginLeft: "auto" }}>
            {([["table", "Table"], ["expanded", "Expanded"], ["byPage", "By page"], ["bySection", "By section"]] as [ChunkView, string][]).map(
              ([v, label]) => (
                <button key={v} aria-pressed={view === v} onClick={() => setView(v)}>{label}</button>
              ),
            )}
          </div>
        </div>
      </div>

      {/* chunk views */}
      {view === "table" ? (
        <div className="card">
          <DataTable
            columns={columns}
            rows={q.data?.items ?? null}
            loading={q.loading}
            error={q.error}
            onRetry={reload}
            onRowClick={(c) => onOpenChunk(c.chunkId)}
            rowKey={(c) => c.chunkId}
            empty={{ icon: "database", title: "No chunks", body: "This document has no chunks matching the filters." }}
            footer={q.data ? <Pagination page={page} pageSize={pageSize} total={q.data.meta.total} onPage={setPage} onPageSize={setPageSize} /> : null}
          />
        </div>
      ) : (
        <div className="card card-pad col gap-12">
          {q.loading && <p className="t-sub">Loading chunks…</p>}
          {q.error && <Callout tone="critical" title="Failed to load">{q.error}</Callout>}
          {!q.loading && !q.error && chunks.length === 0 && (
            <EmptyState icon="database" title="No chunks" body="No chunks match the current filters." />
          )}
          {!q.loading && chunks.length > 0 && (
            <ChunkGroupedView view={view} chunks={chunks} onOpenChunk={onOpenChunk} menu={chunkMenu} />
          )}
          {q.data && <Pagination page={page} pageSize={pageSize} total={q.data.meta.total} onPage={setPage} onPageSize={setPageSize} />}
        </div>
      )}
    </div>
  );
}

function DocumentSummary({ doc, detail, loading }: { doc: ReviewDocument; detail: ReviewDocumentDetail | null; loading: boolean }) {
  const qy = detail?.quality;
  return (
    <div className="card card-pad col gap-12">
      <div className="row-between">
        <div className="col" style={{ gap: 2 }}>
          <span className="t-strong row gap-6" style={{ fontSize: 15 }}><Icon name="file" size={15} /> {doc.fileName}</span>
          <span className="t-micro t-sub">
            {doc.tenantName}{doc.tenantCode ? ` (${doc.tenantCode})` : ""} · {doc.kbName ?? doc.kbId} · .{doc.fileExt} · {fmtBytes(doc.sizeBytes)}
          </span>
        </div>
        <StatusChip status={doc.status} />
      </div>
      {doc.failureReason && <Callout tone="critical" title="Ingestion failure">{doc.failureReason}</Callout>}
      {["pending", "processing"].includes(doc.status) && (
        <div className="col gap-4">
          <span className="t-micro t-sub">Ingestion {doc.ingestionStage ? `· ${doc.ingestionStage}` : ""}</span>
          <Progress value={doc.ingestionProgress} tone="good" />
        </div>
      )}
      <div className="grid grid-4">
        <KpiCard label="Chunks" value={String(doc.chunkCount)} icon="database" />
        <KpiCard label="Pages" value={String(doc.pageCount)} icon="file" />
        <KpiCard label="Embedding" value={doc.embeddingModel ? `${doc.embeddingDimension}d` : "—"} icon="cpu" />
        {/* Temporarily hidden: "Uploaded" date KPI (kept in the API).
            Size fills the slot so the grid keeps four aligned cards. */}
        <KpiCard label="Size" value={fmtBytes(doc.sizeBytes)} icon="download" />
      </div>
      {loading && <p className="t-micro t-sub">Loading quality summary…</p>}
      {qy && (
        <div className="row gap-6" style={{ flexWrap: "wrap" }}>
          <span className="chip chip-neutral">Active {qy.activeChunks}</span>
          <span className="chip chip-neutral">Archived {qy.archivedChunks}</span>
          <span className="chip chip-info">Tokens {qy.minTokens ?? 0}–{qy.maxTokens ?? 0} (avg {qy.avgTokens ?? 0})</span>
          {qy.shortChunks > 0 && <span className="chip chip-warning">Short {qy.shortChunks}</span>}
          {qy.chunksMissingPage > 0 && <span className="chip chip-neutral">No page {qy.chunksMissingPage}</span>}
          {qy.chunksMissingSection > 0 && <span className="chip chip-neutral">No section {qy.chunksMissingSection}</span>}
          {qy.ocrChunks > 0 && <span className="chip chip-info">OCR {qy.ocrChunks}</span>}
          {qy.tableChunks > 0 && <span className="chip chip-info">Tables {qy.tableChunks}</span>}
          {qy.promptInjectionChunks > 0 && <span className="chip chip-critical">Injection {qy.promptInjectionChunks}</span>}
          {qy.flaggedChunks > 0 && <span className="chip chip-warning">Flagged {qy.flaggedChunks}</span>}
        </div>
      )}
    </div>
  );
}

function ChunkGroupedView({
  view, chunks, onOpenChunk, menu,
}: {
  view: ChunkView; chunks: ReviewChunk[];
  onOpenChunk: (id: string) => void; menu: (c: ReviewChunk) => (MenuAction | "sep")[];
}) {
  if (view === "expanded") {
    return (
      <div className="col gap-12">
        {chunks.map((c) => <ChunkCard key={c.chunkId} c={c} onOpen={onOpenChunk} menu={menu} full />)}
      </div>
    );
  }
  // group by page or section (within the loaded page of results)
  const keyOf = (c: ReviewChunk) =>
    view === "byPage" ? (c.pageNumber != null ? `Page ${c.pageNumber}` : "No page") : (c.section || "No section");
  const groups = new Map<string, ReviewChunk[]>();
  for (const c of chunks) {
    const k = keyOf(c);
    (groups.get(k) ?? groups.set(k, []).get(k)!).push(c);
  }
  return (
    <div className="col gap-16">
      {[...groups.entries()].map(([g, list]) => (
        <div key={g} className="col gap-8">
          <div className="row gap-8" style={{ alignItems: "center" }}>
            <Icon name={view === "byPage" ? "file" : "layers"} size={14} />
            <span className="t-strong">{g}</span>
            <span className="chip chip-neutral">{list.length}</span>
          </div>
          <div className="col gap-8">
            {list.map((c) => <ChunkCard key={c.chunkId} c={c} onOpen={onOpenChunk} menu={menu} />)}
          </div>
        </div>
      ))}
    </div>
  );
}

function ChunkCard({
  c, onOpen, menu, full,
}: {
  c: ReviewChunk; onOpen: (id: string) => void; menu: (c: ReviewChunk) => (MenuAction | "sep")[]; full?: boolean;
}) {
  return (
    <div className="card card-pad col gap-8" style={{ border: "1px solid var(--hairline)" }}>
      <div className="row-between">
        <span className="row gap-6 t-micro t-sub" style={{ flexWrap: "wrap" }}>
          <span className="chip chip-neutral">#{c.chunkIndex}</span>
          {c.pageNumber != null && <span className="tag">p.{c.pageNumber}</span>}
          {c.section && <span className="tag">{c.section}</span>}
          <span className="tag">{c.tokenCount ?? "?"} tok · {c.charCount} ch</span>
          <StatusChip status={c.status} />
        </span>
        <div className="row gap-4">
          <Button size="sm" variant="ghost" icon="eye" onClick={() => onOpen(c.chunkId)}>Open</Button>
          <MenuButton actions={menu(c)} />
        </div>
      </div>
      <p className="t-body" style={{ whiteSpace: "pre-wrap", margin: 0 }}>
        {full ? c.content : c.contentPreview + (c.content.length > c.contentPreview.length ? "…" : "")}
      </p>
      <WarningBadges warnings={c.warnings} />
    </div>
  );
}

/* ── document detail drawer ────────────────────────────────────────────── */

function DocumentDetailDrawer({
  documentId, onClose, onReviewChunks, onTestRetrieval,
}: {
  documentId: string; onClose: () => void;
  onReviewChunks: (d: ReviewDocument) => void;
  onTestRetrieval: (d: ReviewDocument) => void;
}) {
  const { toast } = useApp();
  const q = useAsync(() => api.getReviewDocument(documentId), [documentId]);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const d = q.data;
  const downloadOriginal = async () => {
    if (!d || downloadBusy) return;
    setDownloadBusy(true);
    try {
      await api.downloadReviewDocument(d.documentId, d.fileName);
      toast(`Downloaded ${d.fileName}`, "good");
    } catch (error) {
      toast(error instanceof Error ? error.message : "Download failed", "error");
    } finally {
      setDownloadBusy(false);
    }
  };
  const rows: [string, React.ReactNode][] = d ? [
    ["Document ID", d.documentId],
    ["Tenant", `${d.tenantName ?? "—"}${d.tenantCode ? ` (${d.tenantCode})` : ""}`],
    ["Knowledge base", `${d.kbName ?? "—"} · ${d.kbId}`],
    ["Original filename", d.fileName],
    ["File type", `.${d.fileExt} (${d.mimeType || "unknown"})`],
    ["File size", fmtBytes(d.sizeBytes)],
    ["Upload status", d.uploadStatus],
    ["Ingestion status", `${d.ingestionStatus}${d.ingestionStage ? ` · ${d.ingestionStage}` : ""}`],
    ["Total pages", d.pageCount],
    ["Total chunks", d.chunkCount],
    ["Embedding", d.embeddingModel ? `${d.embeddingModel} · ${d.embeddingDimension}d` : "—"],
    ["Language", d.language ?? "—"],
    ["Uploaded by", d.uploadedByName ?? d.uploadedBy ?? "—"],
    // Temporarily hidden from the detail view (API still returns it):
    // ["Uploaded at", fmtDate(d.uploadedAt)],
    ["Processing completed", fmtDate(d.processingCompletedAt)],
  ] : [];

  return (
    <Drawer open onClose={onClose} wide
      title={d?.fileName ?? "Document"}
      sub={d ? <StatusChip status={d.status} /> : "Loading…"}
      footer={d && (
        <div className="row gap-8">
          <Button variant="primary" icon="database" onClick={() => onReviewChunks(d)}>Review chunks</Button>
          <Button variant="secondary" icon="search" onClick={() => onTestRetrieval(d)}>Test retrieval</Button>
          <Button
            variant="secondary"
            icon="download"
            busy={downloadBusy}
            disabled={!d.hasOriginalFile}
            onClick={() => void downloadOriginal()}
          >
            Download
          </Button>
        </div>
      )}
    >
      {q.loading && <p className="t-sub">Loading…</p>}
      {q.error && <Callout tone="critical" title="Failed to load">{q.error}</Callout>}
      {d && (
        <div className="col gap-16">
          {d.failureReason && <Callout tone="critical" title="Failure reason">{d.failureReason}</Callout>}
          <dl className="detail-grid">
            {rows.map(([k, v]) => (
              <div key={k} className="row-between" style={{ padding: "6px 0", borderBottom: "1px solid var(--hairline)" }}>
                <dt className="t-micro t-sub">{k}</dt>
                <dd className="t-body" style={{ margin: 0, textAlign: "right", maxWidth: "60%" }}>{v}</dd>
              </div>
            ))}
          </dl>
          <div>
            <div className="t-label mb-8">Chunk quality</div>
            <div className="grid grid-4">
              <KpiCard label="Active" value={String(d.quality.activeChunks)} icon="check-circle" />
              <KpiCard label="Archived" value={String(d.quality.archivedChunks)} icon="trash" />
              <KpiCard label="Avg tokens" value={String(d.quality.avgTokens ?? 0)} icon="database" />
              <KpiCard label="Short" value={String(d.quality.shortChunks)} icon="alert" />
              <KpiCard label="No page" value={String(d.quality.chunksMissingPage)} icon="file" />
              <KpiCard label="No section" value={String(d.quality.chunksMissingSection)} icon="file" />
              <KpiCard label="OCR" value={String(d.quality.ocrChunks)} icon="eye" />
              <KpiCard label="Injection" value={String(d.quality.promptInjectionChunks)} icon="shield" />
            </div>
          </div>
        </div>
      )}
    </Drawer>
  );
}

/* ── chunk detail drawer (prev / current / next) ───────────────────────── */

function ChunkDetailDrawer({
  chunkId, onNavigate, onClose,
}: {
  chunkId: string; onNavigate: (id: string) => void; onClose: () => void;
}) {
  const { toast } = useApp();
  const q = useAsync(() => api.getReviewChunk(chunkId), [chunkId]);
  const d = q.data as ReviewChunkDetail | null;

  const copy = () => d && navigator.clipboard.writeText(d.content).then(
    () => toast("Chunk content copied", "good"), () => toast("Copy failed", "error"));

  return (
    <Drawer open onClose={onClose} wide
      title={d ? `Chunk #${d.chunkIndex}` : "Chunk"}
      sub={d ? <span className="row gap-6"><StatusChip status={d.status} />{d.kbName}</span> : "Loading…"}
      headerExtra={d && (
        <div className="row gap-4">
          <Button size="sm" variant="ghost" icon="chevron-left" disabled={!d.prev}
            onClick={() => d.prev && onNavigate(d.prev.chunkId)}>Prev</Button>
          <Button size="sm" variant="ghost" disabled={!d.next}
            onClick={() => d.next && onNavigate(d.next.chunkId)}>Next<Icon name="chevron-right" size={14} /></Button>
        </div>
      )}
      footer={d && (
        <div className="row gap-8">
          <Button variant="secondary" icon="copy" onClick={copy}>Copy content</Button>
        </div>
      )}
    >
      {q.loading && <p className="t-sub">Loading…</p>}
      {q.error && <Callout tone="critical" title="Failed to load">{q.error}</Callout>}
      {d && (
        <div className="col gap-16">
          {/* quality signals */}
          <div className="row gap-6" style={{ flexWrap: "wrap" }}>
            <span className="chip chip-info">{d.quality.tokenCount ?? "?"} tokens</span>
            <span className="chip chip-info">{d.quality.charCount} chars</span>
            <span className="chip chip-neutral">overlap {d.quality.overlapWithPrevChars} ch</span>
            {d.quality.duplicate && <span className="chip chip-warning">duplicate ×{d.quality.duplicateCount}</span>}
            <span className="chip chip-neutral">embedding {d.embeddingGenerated ? "✓" : "✗"}{d.embeddingDimension ? ` ${d.embeddingDimension}d` : ""}</span>
          </div>
          <WarningBadges warnings={d.warnings} extraPii={d.quality.pii} />
          {d.quality.promptInjectionPatterns.length > 0 && (
            <Callout tone="critical" title="Possible prompt injection">
              Matched patterns: {d.quality.promptInjectionPatterns.join("; ")}
            </Callout>
          )}
          {d.quality.pii && (
            <Callout tone="warning" title="Possible PII">
              Detected: {d.quality.piiKinds.join(", ")}. Consider masking before exposure.
            </Callout>
          )}

          {/* ownership + metadata (uploaded date temporarily hidden from this
              view — kept in the API for future re-enablement) */}
          <div className="grid grid-4">
            <Meta label="Tenant" value={d.tenantName ?? d.tenantId ?? "—"} />
            <Meta label="Knowledge Base" value={d.kbName ?? d.kbId} />
            <Meta label="Document" value={d.fileName ?? "—"} />
            <Meta label="Document ID" value={<code className="t-num" style={{ fontSize: 11 }}>{d.documentId}</code>} />
            <Meta label="Chunk index" value={`#${d.chunkIndex}`} />
            <Meta label="Page" value={d.pageNumber ?? "—"} />
            <Meta label="Section" value={d.section ?? "—"} />
            <Meta label="Topic" value={d.topic ?? "—"} />
            <Meta label="Language" value={d.language ?? "—"} />
            <Meta label="Chunk type" value={d.chunkType ?? "—"} />
            <Meta label="Embedding model" value={d.embeddingModel ?? "—"} />
            <Meta label="Vector dimension" value={d.embeddingDimension ? `${d.embeddingDimension}d` : "—"} />
            <Meta label="Created" value={fmtDate(d.createdAt)} />
            <Meta label="Updated" value={fmtDate(d.updatedAt)} />
          </div>
          {d.keywords.length > 0 && (
            <div className="col gap-4">
              <span className="t-label">Keywords</span>
              <div className="row gap-4" style={{ flexWrap: "wrap" }}>
                {d.keywords.map((k, i) => <span key={i} className="tag">{k}</span>)}
              </div>
            </div>
          )}

          {/* prev / current / next context */}
          <div className="col gap-8">
            <span className="t-label">Boundary context (previous · current · next)</span>
            <NeighborBlock label="Previous" n={d.prev} onOpen={onNavigate} />
            <div className="card card-pad" style={{ border: "2px solid var(--brand, #6a5af9)" }}>
              <span className="t-micro t-sub">Current · #{d.current.chunkIndex}</span>
              <p className="t-body" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{d.content}</p>
            </div>
            <NeighborBlock label="Next" n={d.next} onOpen={onNavigate} />
          </div>

          {/* raw metadata — readable JSON tree instead of a raw dump */}
          <details open>
            <summary className="t-label" style={{ cursor: "pointer" }}>Metadata</summary>
            <div style={{ marginTop: 8 }}>
              <JsonView value={d.metadata} />
            </div>
          </details>
        </div>
      )}
    </Drawer>
  );
}

function Meta({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="col" style={{ gap: 2 }}>
      <span className="t-label">{label}</span>
      <span className="t-body">{value}</span>
    </div>
  );
}

function NeighborBlock({ label, n, onOpen }: { label: string; n: ReviewChunkDetail["prev"]; onOpen: (id: string) => void }) {
  if (!n) return <div className="card card-pad t-micro t-sub">{label}: none (boundary of document)</div>;
  return (
    <button className="card card-pad col gap-4" style={{ textAlign: "left", cursor: "pointer", width: "100%" }}
      onClick={() => onOpen(n.chunkId)}>
      <span className="t-micro t-sub">{label} · #{n.chunkIndex}{n.pageNumber != null ? ` · p.${n.pageNumber}` : ""}</span>
      <span className="t-body" style={{ opacity: 0.85 }}>{n.content.slice(0, 220)}{n.content.length > 220 ? "…" : ""}</span>
    </button>
  );
}

/* ── retrieval test drawer ─────────────────────────────────────────────── */

function RetrievalTestDrawer({
  scope, onClose,
}: {
  scope: { documentId?: string; kbId?: string; label: string }; onClose: () => void;
}) {
  const { toast } = useApp();
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(8);
  const [result, setResult] = useState<RetrievalTestResult | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!query.trim()) return;
    setBusy(true);
    try {
      const r = await api.reviewRetrievalTest({
        query, topK,
        kbIds: scope.kbId ? [scope.kbId] : undefined,
        documentId: scope.documentId,
      });
      setResult(r);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Retrieval test failed", "error");
    } finally { setBusy(false); }
  };

  return (
    <Drawer open onClose={onClose} wide title="Test retrieval" sub={scope.label}>
      <div className="col gap-16">
        <div className="row gap-8">
          <input className="input" style={{ flex: 1 }} placeholder="Enter a query to retrieve against this document/KB…"
            value={query} autoFocus
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run()} />
          <select className="select" value={topK} onChange={(e) => setTopK(Number(e.target.value))} aria-label="Top K">
            {[3, 5, 8, 12, 20].map((k) => <option key={k} value={k}>top {k}</option>)}
          </select>
          <Button variant="primary" icon="search" busy={busy} onClick={run}>Run</Button>
        </div>

        {result && (
          <>
            <div className="row gap-6" style={{ flexWrap: "wrap" }}>
              <span className={`chip ${result.answerable ? "chip-good" : "chip-warning"}`}>
                {result.answerable ? "Answerable" : "Below threshold"}
              </span>
              <span className="chip chip-info">confidence {result.confidence}</span>
              <span className="chip chip-neutral">threshold {result.threshold}</span>
              <span className="chip chip-neutral">{result.durationMs} ms</span>
              <span className="chip chip-neutral">{result.results.length} candidates</span>
            </div>
            {result.results.length === 0 ? (
              <EmptyState icon="search" title="No candidates" body="No chunks were retrieved for this query." />
            ) : (
              <div className="col gap-8">
                {result.results.map((r) => (
                  <div key={r.chunkId} className="card card-pad col gap-6" style={{ border: "1px solid var(--hairline)" }}>
                    <div className="row-between">
                      <span className="row gap-6 t-micro t-sub" style={{ flexWrap: "wrap" }}>
                        <span className="chip chip-neutral">#{r.rank}</span>
                        <span className="tag">{r.documentName ?? r.documentId}</span>
                        {r.pageNumber != null && <span className="tag">p.{r.pageNumber}</span>}
                        {r.section && <span className="tag">{r.section}</span>}
                        <span className={`chip ${r.passedThreshold ? "chip-good" : "chip-neutral"}`}>
                          {r.passedThreshold ? "passed" : "below"}
                        </span>
                      </span>
                    </div>
                    <div className="row gap-6 t-micro">
                      <span className="chip chip-info">fused {r.score}</span>
                      <span className="chip chip-info">dense {r.vectorScore}</span>
                      <span className="chip chip-info">keyword {r.keywordScore ?? "—"}</span>
                    </div>
                    <p className="t-body" style={{ whiteSpace: "pre-wrap", margin: 0 }}>{r.text}</p>
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        {!result && <p className="t-sub">Enter a query and run to see retrieved chunks with dense, keyword and fused scores, rank, and whether each passes the answerability threshold.</p>}
      </div>
    </Drawer>
  );
}
