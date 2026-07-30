import { useAsync } from "@/hooks/useAsync";
import { getKnowledgeDetail } from "@/services/api";
import { Callout, Drawer, ErrorState, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { fmtNum } from "@/components/charts";
import type { DocumentStatus } from "@/types/domain";

/* Read-only Knowledge Base inspection drawer, shared by the admin Knowledge
   page and the tenant detail Knowledge tab. Details are always fetched fresh
   through GET /knowledge/{id} — the backend enforces tenant access, so this
   component can never show a KB the caller isn't allowed to see. */

const typeIcon: Record<string, IconName> = { document: "file", url: "link", faq: "message", connector: "plug" };

const fmtDate = (iso: string | null | undefined): string =>
  iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="row-between" style={{ padding: "6px 0", borderBottom: "1px solid var(--hairline)", gap: 16 }}>
      <span className="t-micro t-sub" style={{ flexShrink: 0 }}>{label}</span>
      <span className="t-body" style={{ textAlign: "right", overflowWrap: "anywhere" }}>{value}</span>
    </div>
  );
}

export function KnowledgeDetailDrawer({ sourceId, onClose }: {
  sourceId: string;
  onClose: () => void;
}) {
  const q = useAsync(() => getKnowledgeDetail(sourceId), [sourceId]);
  const d = q.data;

  return (
    <Drawer open onClose={onClose} wide
      title={d?.name ?? "Knowledge Base"}
      sub={d ? (
        <span className="row gap-8">
          <StatusChip status={d.status} />
          <span className="t-micro">{d.tenantName ?? "—"}{d.tenantId ? ` · ${d.tenantId}` : ""}</span>
        </span>
      ) : "Loading…"}
    >
      {q.loading && <p className="t-sub">Loading…</p>}
      {q.error && <ErrorState message={q.error} onRetry={q.reload} />}
      {d && (
        <div className="col gap-16">
          {d.stats.lastError && (
            <Callout tone="critical" title="Last indexing error">{d.stats.lastError}</Callout>
          )}

          <div className="row gap-6" style={{ flexWrap: "wrap" }}>
            <span className="chip chip-neutral"><Icon name="file" size={11} /> {d.stats.documentCount} documents</span>
            <span className="chip chip-neutral"><Icon name="database" size={11} /> {fmtNum(d.stats.activeChunks)} active chunks</span>
            <span className="chip chip-info">{fmtNum(d.stats.embeddedChunks)} embedded</span>
            {d.stats.failedDocuments > 0 && <span className="chip chip-critical">{d.stats.failedDocuments} failed</span>}
            {d.stats.embeddingModels.map((m) => <span key={m} className="chip chip-info">{m}</span>)}
          </div>

          <div>
            <span className="t-label">Knowledge Base</span>
            <div className="col mt-8">
              <DetailRow label="Name" value={<span className="t-strong">{d.name}</span>} />
              <DetailRow label="ID" value={<code className="t-num">{d.id}</code>} />
              <DetailRow label="Description" value={d.description || "—"} />
              <DetailRow label="Type" value={
                <span className="row gap-6" style={{ justifyContent: "flex-end" }}>
                  <Icon name={typeIcon[d.type] ?? "file"} size={13} />
                  <span style={{ textTransform: "capitalize" }}>{d.type}</span>
                </span>
              } />
              <DetailRow label="Scope" value={<span style={{ textTransform: "capitalize" }}>{d.scope}</span>} />
              <DetailRow label="Status" value={<StatusChip status={d.status} />} />
              <DetailRow label="Index health" value={d.quality ? `${d.quality}%` : "—"} />
            </div>
          </div>

          <div>
            <span className="t-label">Ownership</span>
            <div className="col mt-8">
              <DetailRow label="Tenant" value={d.tenantName ?? "—"} />
              <DetailRow label="Tenant ID" value={d.tenantId ? <code className="t-num">{d.tenantId}</code> : "—"} />
              {d.botId && <DetailRow label="Bot" value={`${d.botName ?? "—"} · ${d.botId}`} />}
              <DetailRow label="Created by" value={d.createdBy ?? "—"} />
            </div>
          </div>

          <div>
            <span className="t-label">Indexing</span>
            <div className="col mt-8">
              <DetailRow label="Source counter" value={`${fmtNum(d.chunks)} chunks · ${fmtNum(d.sizeKb)} KB`} />
              <DetailRow label="Documents" value={`${d.stats.documentCount} (${d.stats.readyDocuments} ready, ${d.stats.failedDocuments} failed)`} />
              <DetailRow label="Active chunks" value={fmtNum(d.stats.activeChunks)} />
              <DetailRow label="Embedded chunks" value={fmtNum(d.stats.embeddedChunks)} />
              <DetailRow label="Embedding models" value={d.stats.embeddingModels.join(", ") || "—"} />
              <DetailRow label="Retrieval hits (30d)" value={fmtNum(d.usage30d)} />
              <DetailRow label="Last sync" value={fmtDate(d.lastSync)} />
              <DetailRow label="Created" value={fmtDate(d.createdAt)} />
              <DetailRow label="Updated" value={fmtDate(d.updatedAt)} />
            </div>
          </div>

          <div>
            <span className="t-label">Documents ({d.stats.documentCount})</span>
            <div className="col gap-8 mt-8">
              {d.documents.length === 0 && (
                <span className="t-sub">No documents uploaded to this Knowledge Base.</span>
              )}
              {d.documents.map((doc: DocumentStatus) => (
                <div key={doc.documentId} className="col gap-4 card-pad-sm"
                  style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                  <div className="row-between gap-8">
                    <span className="t-strong truncate" style={{ fontSize: 12.5 }} title={doc.fileName}>{doc.fileName}</span>
                    <StatusChip status={doc.status === "ready" ? "indexed" : doc.status} label={doc.status} />
                  </div>
                  <div className="row-between">
                    <span className="t-micro">
                      {doc.chunkCount ? `${fmtNum(doc.chunkCount)} chunks` : "no chunks"}
                      {doc.pageCount ? ` · ${doc.pageCount} pages` : ""}
                    </span>
                    <span className="t-micro t-num">{doc.documentId}</span>
                  </div>
                  {doc.failureReason && (
                    <span className="t-micro" style={{ color: "var(--status-critical)" }}>{doc.failureReason}</span>
                  )}
                </div>
              ))}
              {d.stats.documentCount > d.documents.length && (
                <span className="t-micro t-sub">
                  Showing {d.documents.length} of {d.stats.documentCount} — use Chunk Review for the full list.
                </span>
              )}
            </div>
          </div>

          <p className="t-micro t-sub" style={{ margin: 0 }}>
            Inspect individual documents and chunks in <strong>Knowledge Management → Chunk Review</strong>.
          </p>
        </div>
      )}
    </Drawer>
  );
}
