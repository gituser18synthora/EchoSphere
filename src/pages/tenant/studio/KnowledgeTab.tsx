import { useEffect, useRef, useState } from "react";
import type { DocumentState, KnowledgeSource, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  archiveKnowledge, createKnowledge, deleteDocument, getDocumentStatus, getUploadConfig,
  listKnowledge, listKnowledgeDocuments, listKnowledgeGaps, reindexDocument,
  resyncKnowledge, retryDocument, uploadKnowledgeDocument,
} from "@/services/api";
import { Button, Drawer, Modal, Progress, StatusChip, CardSkeleton, Field, Callout } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { RetrievalTester } from "@/components/RetrievalTester";
import { Icon, type IconName } from "@/components/Icon";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";

const typeIcon: Record<string, IconName> = { document: "file", url: "link", faq: "message", connector: "plug" };

function daysSince(iso: string): number | null {
  if (!iso || iso === "—") return null;
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return null;
  return Math.max(0, Math.round((Date.now() - d) / 86400000));
}

const NO_UPLOAD_PERMISSION = "You don't have permission to upload documents";

export default function KnowledgeTab({ bot }: { bot: VoiceBot }) {
  const { toast, hasPermission } = useApp();
  const canUpload = hasPermission("upload_knowledge_documents") || hasPermission("knowledge.manage");
  const q = useAsync(() => listKnowledge(bot.id), [bot.id]);
  const gapsQ = useAsync(() => listKnowledgeGaps(bot.id), [bot.id]);
  const [preview, setPreview] = useState<KnowledgeSource | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);

  const sources = q.data ?? [];
  const indexed = sources.filter((s) => s.status === "indexed").length;
  const avgQuality = sources.filter((s) => s.quality > 0).reduce((a, s, _, arr) => a + s.quality / arr.length, 0);

  const resync = async (s: KnowledgeSource) => {
    try {
      await resyncKnowledge(s.id);
      toast(`Re-sync queued for “${s.name}” — indexing usually completes in a few minutes`);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Re-sync failed", "error");
    }
  };

  const remove = async (s: KnowledgeSource) => {
    try {
      await archiveKnowledge(s.id);
      toast(`“${s.name}” archived — chunks removed from the index`);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Could not archive source", "error");
    }
  };

  return (
    <div className="col gap-16">
      <div className="row-between wrap">
        <div className="row gap-16 wrap">
          <MiniStat label="Sources" value={String(sources.length)} />
          <MiniStat label="Indexed" value={`${indexed}/${sources.length}`} />
          <MiniStat label="Chunks" value={fmtNum(sources.reduce((a, s) => a + s.chunks, 0))} />
          <MiniStat label="Avg quality" value={avgQuality ? `${avgQuality.toFixed(0)}%` : "—"} />
        </div>
        <div className="row gap-6">
          <Button
            icon="plug"
            disabled={!flags.knowledgeConnectors}
            title={flags.knowledgeConnectors ? undefined : "Connector OAuth flows pending backend (TODO_BACKEND #5)"}
            onClick={() => {}}
          >
            Connect source
          </Button>
          <Button
            variant="primary" icon="upload"
            disabled={!canUpload}
            title={canUpload ? undefined : NO_UPLOAD_PERMISSION}
            onClick={() => setUploadOpen(true)}
          >
            Add knowledge
          </Button>
        </div>
      </div>

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={sources}
          onRowClick={(s) => setPreview(s)}
          empty={{
            icon: "book", title: "No knowledge yet",
            body: "Upload documents, add URLs or curate FAQs. The bot answers only from indexed sources.",
            action: (
              <Button
                variant="primary" icon="upload"
                disabled={!canUpload}
                title={canUpload ? undefined : NO_UPLOAD_PERMISSION}
                onClick={() => setUploadOpen(true)}
              >
                Add knowledge
              </Button>
            ),
          }}
          columns={[
            {
              key: "name", header: "Source", sortValue: (s) => s.name,
              render: (s) => (
                <div className="row gap-12">
                  <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name={typeIcon[s.type]} size={14} /></span>
                  <div><div className="t-strong">{s.name}</div><div className="t-micro">{s.detail}</div></div>
                </div>
              ),
            },
            { key: "scope", header: "Scope", sortValue: (s) => s.scope, render: (s) => <span className="tag" style={{ textTransform: "capitalize" }}>{s.scope}</span> },
            { key: "status", header: "Index status", sortValue: (s) => s.status, render: (s) => <StatusChip status={s.status} /> },
            { key: "chunks", header: "Chunks", align: "right", sortValue: (s) => s.chunks, render: (s) => <span className="t-num">{s.chunks ? fmtNum(s.chunks) : "—"}</span> },
            {
              key: "quality", header: "Index health", width: 140, sortValue: (s) => s.quality,
              render: (s) => s.quality ? (
                <div className="row gap-8">
                  <Progress value={s.quality} tone={s.quality > 85 ? "good" : s.quality > 65 ? "warning" : "critical"} />
                  <span className="t-num t-micro">{s.quality}%</span>
                </div>
              ) : <span className="t-micro">—</span>,
            },
            { key: "usage", header: "Hits (30d)", align: "right", sortValue: (s) => s.usage30d, render: (s) => <span className="t-num">{fmtNum(s.usage30d)}</span> },
            {
              key: "sync", header: "Last sync", sortValue: (s) => s.lastSync,
              render: (s) => <span className="t-sub">{s.lastSync === "—" ? "—" : new Date(s.lastSync).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span>,
            },
            {
              key: "act", header: "", width: 110,
              render: (s) => (s.status === "stale" || s.status === "failed")
                ? <Button size="sm" icon="refresh" onClick={(e) => { e.stopPropagation(); void resync(s); }}>Re-sync</Button>
                : null,
            },
          ]}
        />
      </div>

      {/* Knowledge gaps */}
      <div className="card">
        <div className="card-header">
          <div className="col gap-2">
            <span className="card-title">Knowledge gaps</span>
            <span className="t-micro">Questions callers asked that no indexed source could answer</span>
          </div>
        </div>
        {gapsQ.loading ? <div style={{ padding: 16 }}><CardSkeleton rows={3} /></div> : (
          <div className="col" style={{ padding: 16, gap: 8 }}>
            {(gapsQ.data ?? []).length === 0 && <span className="t-sub">No open gaps — retrieval is covering caller questions.</span>}
            {(gapsQ.data ?? []).map((g) => (
              <div key={g.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <span className="icon-tile warning" style={{ width: 30, height: 30 }}><Icon name="search" size={14} /></span>
                <div className="grow">
                  <div className="t-strong" style={{ fontSize: 13 }}>“{g.question}”</div>
                  <div className="t-micro">Asked {g.frequency}× in 30 days · suggestion: {g.suggestedSource}</div>
                </div>
                <Button size="sm" disabled={!canUpload} title={canUpload ? undefined : NO_UPLOAD_PERMISSION} onClick={() => { setUploadOpen(true); }}>Add answer</Button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Source preview drawer */}
      <Drawer
        open={!!preview}
        onClose={() => setPreview(null)}
        title={preview?.name ?? ""}
        sub={preview ? `${preview.detail} · ${preview.chunks} chunks · quality ${preview.quality || "—"}%` : ""}
        headerExtra={preview && <StatusChip status={preview.status} />}
        footer={preview && (
          <>
            {(preview.status === "stale" || preview.status === "failed") && (
              <Button icon="refresh" onClick={() => { void resync(preview); setPreview(null); }}>Re-sync now</Button>
            )}
            <Button variant="danger-ghost" icon="trash" onClick={() => { void remove(preview); setPreview(null); }}>
              Remove from bot
            </Button>
          </>
        )}
      >
        {preview && (
          <div className="col gap-16">
            {preview.status === "failed" && (
              <Callout tone="critical" title="Indexing failed">
                The last sync could not process this source. Re-upload it or retry the sync.
              </Callout>
            )}
            {preview.status === "stale" && (
              <Callout tone="warning" title={daysSince(preview.lastSync) !== null ? `Content is ${daysSince(preview.lastSync)} days old` : "Content is stale"}>
                Retrieval still works, but answers may be outdated.
                {preview.usage30d > 0 && <> {fmtNum(preview.usage30d)} calls used this source in the last 30 days.</>}
              </Callout>
            )}
            <div>
              <span className="t-label">Source details</span>
              <div className="col gap-6 mt-8">
                <Row k="Type" v={preview.type} />
                <Row k="Scope" v={preview.scope} />
                <Row k="Size" v={preview.sizeKb ? `${fmtNum(preview.sizeKb)} KB` : "—"} />
                <Row k="Indexed chunks" v={preview.chunks ? fmtNum(preview.chunks) : "—"} />
                <Row k="Last sync" v={preview.lastSync === "—" ? "never" : new Date(preview.lastSync).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} />
              </div>
            </div>
            <div>
              <span className="t-label">Retrieval performance</span>
              <div className="col gap-6 mt-8">
                <Row k="Hits (30 days)" v={fmtNum(preview.usage30d)} />
                <Row k="Index health" v={preview.quality ? `${preview.quality}%` : "not indexed"} />
                <Row k="Est. similarity" v={preview.quality ? (0.5 + preview.quality / 250).toFixed(2) : "—"} />
              </div>
            </div>
            <SourceDocuments sourceId={preview.id} onChanged={q.reload} />
            <div>
              <span className="t-label">Test retrieval</span>
              <div className="mt-8"><RetrievalTester kbIds={[preview.id]} /></div>
            </div>
          </div>
        )}
      </Drawer>

      <UploadModal
        open={uploadOpen}
        botId={bot.id}
        onClose={() => setUploadOpen(false)}
        onAdded={q.reload}
      />
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="row-between" style={{ padding: "7px 0", borderBottom: "1px solid var(--hairline)" }}>
      <span className="t-sub">{k}</span><span className="t-strong t-num">{v}</span>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <span className="col" style={{ gap: 0 }}>
      <span className="t-micro">{label}</span>
      <span className="t-strong t-num" style={{ fontSize: 16 }}>{value}</span>
    </span>
  );
}

/* ---------- Document status helpers ---------- */

const docChip: Record<DocumentState, { status: string; label?: string }> = {
  pending: { status: "pending" },
  processing: { status: "indexing", label: "processing" },
  ready: { status: "indexed", label: "ready" },
  failed: { status: "failed" },
  cancelled: { status: "cancelled" },
  archived: { status: "archived" },
};

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 180000;

/* ---------- Source documents (preview drawer) ----------
   Same visibility as the drawer's existing archive action. */

function SourceDocuments({ sourceId, onChanged }: { sourceId: string; onChanged: () => void }) {
  const { toast } = useApp();
  const docsQ = useAsync(() => listKnowledgeDocuments(sourceId), [sourceId]);
  const [busyId, setBusyId] = useState<string | null>(null);

  const act = async (documentId: string, fn: (id: string) => Promise<unknown>, done: string) => {
    setBusyId(documentId);
    try {
      await fn(documentId);
      toast(done);
      docsQ.reload();
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Action failed", "error");
    } finally {
      setBusyId(null);
    }
  };

  const docs = docsQ.data ?? [];
  return (
    <div>
      <span className="t-label">Documents</span>
      <div className="col gap-8 mt-8">
        {docsQ.loading && <CardSkeleton rows={2} />}
        {docsQ.error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{docsQ.error}</span>}
        {!docsQ.loading && !docsQ.error && docs.length === 0 && (
          <span className="t-sub">No documents uploaded to this source yet.</span>
        )}
        {docs.map((d) => (
          <div key={d.documentId} className="col gap-6 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
            <div className="row-between gap-8">
              <span className="t-strong truncate" style={{ fontSize: 12.5 }}>{d.fileName}</span>
              <StatusChip status={docChip[d.status].status} label={docChip[d.status].label} />
            </div>
            {(d.status === "pending" || d.status === "processing") && (
              <div className="row gap-8">
                <Progress value={d.progress} />
                <span className="t-micro t-num" style={{ whiteSpace: "nowrap" }}>{d.stage || "queued"} · {Math.round(d.progress)}%</span>
              </div>
            )}
            {d.failureReason && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{d.failureReason}</span>}
            <div className="row-between">
              <span className="t-micro">
                {d.chunkCount ? `${fmtNum(d.chunkCount)} chunks` : "—"}
                {d.pageCount ? ` · ${d.pageCount} pages` : ""}
                {d.attempts > 1 ? ` · ${d.attempts} attempts` : ""}
              </span>
              <span className="row gap-6">
                {(d.status === "failed" || d.status === "cancelled") && (
                  <Button size="sm" icon="refresh" busy={busyId === d.documentId}
                    onClick={() => void act(d.documentId, retryDocument, `Retry queued for “${d.fileName}”`)}>
                    Retry
                  </Button>
                )}
                {d.status === "ready" && (
                  <Button size="sm" icon="refresh" busy={busyId === d.documentId}
                    onClick={() => void act(d.documentId, reindexDocument, `Re-index queued for “${d.fileName}”`)}>
                    Re-index
                  </Button>
                )}
                {d.status !== "archived" && (
                  <Button size="sm" variant="danger-ghost" icon="trash" busy={busyId === d.documentId}
                    onClick={() => void act(d.documentId, deleteDocument, `“${d.fileName}” archived — chunks removed from the index`)}>
                    Delete
                  </Button>
                )}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------- Add knowledge modal ---------- */

type FileRowStatus = "selected" | "invalid" | "uploading" | "queued" | "processing" | "ready" | "failed";

type FileRow = {
  id: number;
  file: File;
  ext: string;
  status: FileRowStatus;
  duplicate?: boolean;
  stage?: string;
  progress?: number;
  documentId?: string;
  error?: string;
};

const fileRowChip: Record<FileRowStatus, { status: string; label: string }> = {
  selected: { status: "pending", label: "selected" },
  invalid: { status: "critical", label: "invalid" },
  uploading: { status: "indexing", label: "uploading" },
  queued: { status: "pending", label: "queued" },
  processing: { status: "indexing", label: "processing" },
  ready: { status: "indexed", label: "ready" },
  failed: { status: "failed", label: "failed" },
};

const extIcon = (ext: string): IconName => {
  if (ext === "csv" || ext === "xlsx") return "chart";
  if (ext === "json") return "database";
  if (ext === "pptx") return "layers";
  return "file";
};

const fmtFileSize = (bytes: number) =>
  bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;

function UploadModal({ open, botId, onClose, onAdded }: {
  open: boolean; botId: string; onClose: () => void; onAdded: () => void;
}) {
  const { toast } = useApp();
  const configQ = useAsync(getUploadConfig, []);
  const config = configQ.data;

  const [mode, setMode] = useState<"document" | "url" | "faq">("document");
  const [name, setName] = useState("");
  const [nameErr, setNameErr] = useState("");
  const [description, setDescription] = useState("");
  const [files, setFiles] = useState<FileRow[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [createdSourceId, setCreatedSourceId] = useState<string | null>(null);
  const [metaBusy, setMetaBusy] = useState(false);
  const [url, setUrl] = useState("");
  const [urlErr, setUrlErr] = useState("");
  const [faqQuestion, setFaqQuestion] = useState("");
  const [faqAnswer, setFaqAnswer] = useState("");
  const rowSeq = useRef(0);
  const sessionRef = useRef(0);
  const submitGuard = useRef(false);
  const fileRef = useRef<HTMLInputElement>(null);

  /* Reset on open; bumping the session abandons in-flight polls once the modal closes. */
  useEffect(() => {
    sessionRef.current += 1;
    if (!open) return;
    setMode("document");
    setName(""); setNameErr(""); setDescription("");
    setFiles([]); setDragActive(false);
    setSubmitting(false); setCreatedSourceId(null); setMetaBusy(false);
    setUrl(""); setUrlErr(""); setFaqQuestion(""); setFaqAnswer("");
    submitGuard.current = false;
  }, [open]);

  const patchRow = (id: number, p: Partial<FileRow>) =>
    setFiles((rows) => rows.map((r) => (r.id === id ? { ...r, ...p } : r)));

  /* ----- File selection + client-side validation ----- */

  const addFiles = (list: FileList | File[]) => {
    if (!config) return;
    const allowed = config.allowedExtensions.map((e) => e.replace(/^\./, "").toLowerCase());
    const next = Array.from(list).map((file): FileRow => {
      const ext = (/\.([^.]+)$/.exec(file.name)?.[1] ?? "").toLowerCase();
      let error = "";
      if (!allowed.includes(ext)) {
        error = `Unsupported file type${ext ? ` (.${ext})` : ""} — allowed: ${allowed.map((a) => `.${a}`).join(", ")}`;
      } else if (file.size > config.maxFileMb * 1024 * 1024) {
        error = `File is larger than the ${config.maxFileMb} MB limit`;
      }
      return { id: ++rowSeq.current, file, ext, status: error ? "invalid" : "selected", error: error || undefined };
    });
    if (next.length) setFiles((rows) => [...rows, ...next]);
  };

  /* ----- Ingestion polling ----- */

  const pollDoc = async (rowId: number, documentId: string, session: number): Promise<"ready" | "failed"> => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (sessionRef.current !== session) return "failed";
      try {
        const st = await getDocumentStatus(documentId);
        if (sessionRef.current !== session) return "failed";
        if (st.status === "ready") {
          patchRow(rowId, { status: "ready", stage: st.stage, progress: 100 });
          return "ready";
        }
        if (st.status === "failed" || st.status === "cancelled") {
          patchRow(rowId, { status: "failed", stage: st.stage, error: st.failureReason || `Processing ${st.status}` });
          return "failed";
        }
        patchRow(rowId, { status: st.status === "pending" ? "queued" : "processing", stage: st.stage, progress: st.progress });
      } catch (e) {
        patchRow(rowId, { status: "failed", error: e instanceof Error ? e.message : "Status check failed" });
        return "failed";
      }
      await sleep(POLL_INTERVAL_MS);
    }
    patchRow(rowId, { status: "failed", error: "Still processing after 3 minutes — retry, or track it from the source panel." });
    return "failed";
  };

  /* ----- Document submission: one KB, then sequential uploads ----- */

  const submit = async () => {
    if (submitGuard.current) return;
    const kbName = name.trim();
    if (!kbName) { setNameErr("Knowledge base name is required"); return; }
    const pending = files.filter((f) => f.status === "selected");
    if (pending.length === 0) return;
    submitGuard.current = true;
    setSubmitting(true);
    setNameErr("");
    const session = sessionRef.current;
    try {
      let sourceId = createdSourceId;
      if (!sourceId) {
        try {
          const source = await createKnowledge({
            name: kbName, type: "document", detail: description.trim() || undefined, scope: "bot", botId,
            sizeKb: Math.max(1, Math.round(pending.reduce((a, f) => a + f.file.size, 0) / 1024)),
          });
          sourceId = source.id;
          if (sessionRef.current !== session) return;
          setCreatedSourceId(sourceId);
          onAdded();
        } catch (e) {
          if (sessionRef.current === session) {
            setNameErr(e instanceof Error ? e.message : "Could not create the knowledge base");
          }
          return;
        }
      }
      const polls: Promise<"ready" | "failed">[] = [];
      for (const row of pending) {
        if (sessionRef.current !== session) return;
        patchRow(row.id, { status: "uploading", progress: 0, error: undefined });
        try {
          const up = await uploadKnowledgeDocument(sourceId, row.file);
          if (sessionRef.current !== session) return;
          if (up.duplicate) {
            patchRow(row.id, { status: "ready", duplicate: true, documentId: up.documentId, progress: 100 });
            polls.push(Promise.resolve<"ready" | "failed">("ready"));
          } else {
            patchRow(row.id, { status: "queued", documentId: up.documentId, stage: up.status || "queued", progress: 0 });
            polls.push(pollDoc(row.id, up.documentId, session));
          }
        } catch (e) {
          patchRow(row.id, { status: "failed", error: e instanceof Error ? e.message : "Upload failed" });
          polls.push(Promise.resolve<"ready" | "failed">("failed"));
        }
      }
      await Promise.all(polls);
      if (sessionRef.current === session) onAdded();
    } finally {
      submitGuard.current = false;
      if (sessionRef.current === session) setSubmitting(false);
    }
  };

  const retryRow = async (row: FileRow) => {
    if (!row.documentId) return;
    const session = sessionRef.current;
    patchRow(row.id, { status: "queued", error: undefined, progress: 0, stage: "retrying" });
    try {
      await retryDocument(row.documentId);
    } catch (e) {
      if (sessionRef.current === session) {
        patchRow(row.id, { status: "failed", error: e instanceof Error ? e.message : "Retry failed" });
      }
      return;
    }
    const result = await pollDoc(row.id, row.documentId, session);
    if (sessionRef.current === session && result === "ready") onAdded();
  };

  /* ----- URL / FAQ flow — createKnowledge as before, now with the explicit KB name ----- */

  const addMetaSource = async (type: "url" | "faq", detail: string, clear: () => void) => {
    if (metaBusy) return;
    const kbName = name.trim();
    if (!kbName) { setNameErr("Knowledge base name is required"); return; }
    setMetaBusy(true);
    setNameErr("");
    try {
      await createKnowledge({ name: kbName, type, detail, scope: "bot", botId });
      toast(`“${kbName}” added — indexing started`);
      onAdded();
      clear();
      setName("");
      setDescription("");
    } catch (e) {
      setNameErr(e instanceof Error ? e.message : "Could not create the knowledge base");
    } finally {
      setMetaBusy(false);
    }
  };

  const addUrl = () => {
    if (!/^https?:\/\/[^\s]+\.[^\s]+/.test(url)) { setUrlErr("Enter a full URL, e.g. https://example.com/help"); return; }
    void addMetaSource("url", url, () => setUrl(""));
  };

  const addFaq = () => {
    if (!faqQuestion.trim() || !faqAnswer.trim()) { toast("Enter both a question and an answer", "error"); return; }
    void addMetaSource("faq", "Curated Q&A pair", () => { setFaqQuestion(""); setFaqAnswer(""); });
  };

  /* ----- Derived submission summary ----- */

  const validSelected = files.filter((f) => f.status === "selected");
  const startedRows = files.filter((f) => f.status !== "selected" && f.status !== "invalid");
  const readyRows = startedRows.filter((f) => f.status === "ready");
  const allDone = !submitting && startedRows.length > 0 && validSelected.length === 0 &&
    startedRows.every((f) => f.status === "ready" || f.status === "failed");

  return (
    <Modal open={open} onClose={onClose} title="Add knowledge" sub="New sources index into the draft version; publishing makes them live." wide
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          {mode === "document" && (
            <Button
              variant="primary" icon="upload" busy={submitting}
              disabled={submitting || !name.trim() || validSelected.length === 0}
              onClick={() => void submit()}
            >
              Create knowledge base
            </Button>
          )}
        </>
      }>
      <div className="col gap-16">
        <div className="segmented" role="group" aria-label="Source type">
          {(["document", "url", "faq"] as const).map((m) => (
            <button key={m} aria-pressed={mode === m} onClick={() => setMode(m)} style={{ textTransform: "capitalize" }}>{m === "faq" ? "FAQ pairs" : `${m}s`}</button>
          ))}
        </div>

        {/* Knowledge base */}
        <div className="col gap-8">
          <span className="t-label">Knowledge base</span>
          <Field label="Knowledge base name" required error={nameErr}>
            <input
              className="input" value={name} maxLength={255}
              placeholder="e.g. Product manuals"
              disabled={mode === "document" && !!createdSourceId}
              aria-invalid={!!nameErr}
              onChange={(e) => { setName(e.target.value); setNameErr(""); }}
            />
          </Field>
          <Field label="Description" hint="Optional — shown under the source name in the list.">
            <input
              className="input" value={description} maxLength={255}
              placeholder="What this knowledge covers"
              disabled={mode === "document" && !!createdSourceId}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
        </div>

        {/* Files (document mode only) */}
        {mode === "document" && (
          <div className="col gap-8">
            <span className="t-label">Files</span>
            {configQ.error ? (
              <Callout tone="critical" title="Could not load upload settings">
                <div className="col gap-8" style={{ alignItems: "flex-start" }}>
                  <span>{configQ.error}</span>
                  <Button size="sm" icon="refresh" onClick={configQ.reload}>Retry</Button>
                </div>
              </Callout>
            ) : (
              <>
                <div
                  className={`dropzone${dragActive ? " dropzone-active" : ""}`}
                  onDragEnter={(e) => { e.preventDefault(); if (config) setDragActive(true); }}
                  onDragOver={(e) => { e.preventDefault(); if (config) setDragActive(true); }}
                  onDragLeave={(e) => {
                    if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
                    setDragActive(false);
                  }}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDragActive(false);
                    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
                  }}
                >
                  <span className="dropzone-icon"><Icon name="upload" size={20} /></span>
                  <span className="t-strong" style={{ fontSize: 13 }}>Drag and drop files here</span>
                  <span className="t-micro">or</span>
                  <Button icon="file" disabled={!config} onClick={() => fileRef.current?.click()}>Choose files</Button>
                  <input
                    ref={fileRef}
                    type="file"
                    multiple
                    accept={config?.accept}
                    style={{ display: "none" }}
                    aria-label="Choose files"
                    onChange={(e) => { if (e.target.files?.length) addFiles(e.target.files); e.target.value = ""; }}
                  />
                </div>
                <span className="t-micro">
                  {config
                    ? `Supported: ${config.allowedExtensions.join(", ")} · up to ${config.maxFileMb} MB per file`
                    : "Loading upload settings…"}
                </span>
              </>
            )}

            {files.length > 0 && (
              <div className="col gap-8">
                {files.map((f) => (
                  <div key={f.id} className="file-row">
                    <span className="icon-tile neutral" style={{ width: 30, height: 30, flexShrink: 0 }}>
                      <Icon name={extIcon(f.ext)} size={14} />
                    </span>
                    <div className="file-row-main">
                      <div className="row gap-8" style={{ minWidth: 0 }}>
                        <span className="file-row-name" title={f.file.name}>{f.file.name}</span>
                        <span className="tag" style={{ flexShrink: 0 }}>{(f.ext || "?").toUpperCase()}</span>
                        <span className="t-micro t-num" style={{ whiteSpace: "nowrap", flexShrink: 0 }}>{fmtFileSize(f.file.size)}</span>
                      </div>
                      {(f.status === "uploading" || f.status === "queued" || f.status === "processing") && (
                        <div className="row gap-8">
                          <Progress value={f.progress ?? 0} />
                          <span className="t-micro t-num" style={{ whiteSpace: "nowrap" }}>
                            {f.status === "uploading" ? "uploading" : f.stage || "queued"} · {Math.round(f.progress ?? 0)}%
                          </span>
                        </div>
                      )}
                      {f.error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{f.error}</span>}
                    </div>
                    <span className="row gap-6" style={{ flexShrink: 0 }}>
                      <StatusChip
                        status={fileRowChip[f.status].status}
                        label={f.status === "ready" && f.duplicate ? "ready · duplicate" : fileRowChip[f.status].label}
                      />
                      {(f.status === "selected" || f.status === "invalid") && (
                        <Button
                          size="sm" variant="ghost" icon="x"
                          aria-label={`Remove ${f.file.name}`} title="Remove"
                          onClick={() => setFiles((rows) => rows.filter((r) => r.id !== f.id))}
                        />
                      )}
                      {f.status === "failed" && f.documentId && (
                        <Button size="sm" icon="refresh" onClick={() => void retryRow(f)}>Retry</Button>
                      )}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {allDone && (
              readyRows.length === startedRows.length ? (
                <Callout tone="good" title="Knowledge base ready">
                  All {startedRows.length} document{startedRows.length === 1 ? "" : "s"} indexed — “{name.trim()}” is ready for retrieval.
                </Callout>
              ) : (
                <Callout tone="warning" title="Partially indexed">
                  {readyRows.length} of {startedRows.length} documents ready — retry the failed files above, or close and retry later from the source panel.
                </Callout>
              )
            )}
          </div>
        )}

        {mode === "url" && (
          <Field label="Page or sitemap URL" error={urlErr} hint="The crawler follows same-domain links up to depth 2.">
            <div className="row gap-8">
              <input className="input" value={url} onChange={(e) => { setUrl(e.target.value); setUrlErr(""); }} placeholder="https://example.com/services" aria-invalid={!!urlErr} />
              <Button variant="primary" busy={metaBusy} onClick={addUrl}>Add</Button>
            </div>
          </Field>
        )}

        {mode === "faq" && (
          <div className="col gap-8">
            <Field label="Question"><input className="input" value={faqQuestion} onChange={(e) => setFaqQuestion(e.target.value)} placeholder="Do you validate parking?" /></Field>
            <Field label="Answer"><textarea className="textarea" value={faqAnswer} onChange={(e) => setFaqAnswer(e.target.value)} placeholder="Yes — parking is validated for up to 2 hours at all locations." /></Field>
            <Button variant="primary" busy={metaBusy} style={{ alignSelf: "flex-start" }} onClick={addFaq}>Add FAQ pair</Button>
          </div>
        )}
      </div>
    </Modal>
  );
}
