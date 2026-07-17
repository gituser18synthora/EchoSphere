import { useRef, useState } from "react";
import type { DocumentState, KnowledgeSource, SearchTestResult, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  archiveKnowledge, createKnowledge, deleteDocument, getDocumentStatus,
  listKnowledge, listKnowledgeDocuments, listKnowledgeGaps, reindexDocument,
  resyncKnowledge, retryDocument, searchTest, uploadKnowledgeDocument,
} from "@/services/api";
import { Button, Drawer, EmptyState, Modal, Progress, StatusChip, CardSkeleton, Field, Callout } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
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

export default function KnowledgeTab({ bot }: { bot: VoiceBot }) {
  const { toast } = useApp();
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
          <Button variant="primary" icon="upload" onClick={() => setUploadOpen(true)}>Add knowledge</Button>
        </div>
      </div>

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={sources}
          onRowClick={(s) => setPreview(s)}
          empty={{
            icon: "book", title: "No knowledge yet",
            body: "Upload documents, add URLs or curate FAQs. The bot answers only from indexed sources.",
            action: <Button variant="primary" icon="upload" onClick={() => setUploadOpen(true)}>Add knowledge</Button>,
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
                <Button size="sm" onClick={() => { setUploadOpen(true); }}>Add answer</Button>
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
            <RetrievalTester kbId={preview.id} />
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
const POLL_TIMEOUT_MS = 120000;

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

/* ---------- Retrieval tester (preview drawer) ---------- */

function RetrievalTester({ kbId }: { kbId: string }) {
  const { toast } = useApp();
  const [query, setQuery] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SearchTestResult | null>(null);

  const run = async () => {
    const trimmed = query.trim();
    if (!trimmed || running) return;
    setRunning(true);
    try {
      setResult(await searchTest({ query: trimmed, kbIds: [kbId] }));
    } catch (e) {
      toast(e instanceof Error ? e.message : "Retrieval test failed", "error");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <span className="t-label">Test retrieval</span>
      <div className="col gap-8 mt-8">
        <textarea
          className="textarea"
          rows={2}
          value={query}
          placeholder="Ask a question this source should answer…"
          aria-label="Retrieval test query"
          onChange={(e) => setQuery(e.target.value)}
        />
        <Button icon="search" busy={running} disabled={!query.trim()} style={{ alignSelf: "flex-start" }} onClick={() => void run()}>
          Run retrieval test
        </Button>
        {result && (
          <>
            <div className="row gap-8 wrap">
              <StatusChip status={result.answerable ? "good" : "warning"} label={result.answerable ? "Answerable" : "Not answerable"} />
              <span className="t-micro t-num">confidence {result.confidence.toFixed(2)} · {result.durationMs}ms</span>
            </div>
            {result.skippedReason && (
              <span className="t-micro" style={{ color: "var(--status-warning)" }}>Skipped: {result.skippedReason}</span>
            )}
            {result.sources.length === 0 && <span className="t-sub">No chunks retrieved for this query.</span>}
            {result.sources.map((s) => (
              <div key={s.chunkId} className="col gap-4 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <div className="row-between gap-8">
                  <span className="t-strong truncate" style={{ fontSize: 12.5 }}>{s.documentName}</span>
                  <span className="t-micro t-num" style={{ whiteSpace: "nowrap" }}>
                    {s.pageNumber != null ? `p.${s.pageNumber} · ` : ""}score {s.score.toFixed(2)}
                  </span>
                </div>
                <p className="t-sub" style={{ fontSize: 12, margin: 0 }}>{s.text}</p>
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}

/* ---------- Upload modal ---------- */

type QueueItem = {
  id: number;
  name: string;
  state: "creating" | "uploading" | "processing" | "indexing" | "ready" | "error";
  stage?: string;
  progress?: number;
  documentId?: string;
  error?: string;
};

function UploadModal({ open, botId, onClose, onAdded }: {
  open: boolean; botId: string; onClose: () => void; onAdded: () => void;
}) {
  const { toast } = useApp();
  const [mode, setMode] = useState<"document" | "url" | "faq">("document");
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const queueSeq = useRef(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const [docErr, setDocErr] = useState("");
  const [url, setUrl] = useState("");
  const [urlErr, setUrlErr] = useState("");
  const [faqQuestion, setFaqQuestion] = useState("");
  const [faqAnswer, setFaqAnswer] = useState("");

  const patch = (id: number, p: Partial<QueueItem>) =>
    setQueue((q) => q.map((x) => (x.id === id ? { ...x, ...p } : x)));

  /* URL / FAQ flow — metadata source only, unchanged behaviour. */
  const add = async (name: string, type: "url" | "faq", detail: string) => {
    const id = ++queueSeq.current;
    setQueue((q) => [...q, { id, name, state: "creating" }]);
    try {
      await createKnowledge({ name, type, detail, scope: "bot", botId });
      patch(id, { state: "indexing" });
      toast(`“${name}” added — indexing started`);
      onAdded();
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Upload failed";
      patch(id, { state: "error", error: msg });
      toast(msg, "error");
    }
  };

  /* Document flow — create the source, upload the file, poll ingestion. */
  const pollDoc = async (id: number, documentId: string) => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      let st;
      try {
        st = await getDocumentStatus(documentId);
      } catch (e) {
        patch(id, { state: "error", error: e instanceof Error ? e.message : "Status check failed" });
        return;
      }
      if (st.status === "ready") {
        patch(id, { state: "ready", stage: st.stage, progress: 100 });
        toast(`“${st.fileName}” indexed — ${fmtNum(st.chunkCount)} chunks ready`);
        onAdded();
        return;
      }
      if (st.status === "failed" || st.status === "cancelled") {
        patch(id, { state: "error", stage: st.stage, error: st.failureReason || `Processing ${st.status}` });
        onAdded();
        return;
      }
      patch(id, { state: "processing", stage: st.stage, progress: st.progress });
      await sleep(POLL_INTERVAL_MS);
    }
    patch(id, { state: "error", error: "Still processing after 2 minutes — track it from the source panel and retry if it fails." });
  };

  const uploadDoc = async (file: File) => {
    const id = ++queueSeq.current;
    setQueue((q) => [...q, { id, name: file.name, state: "creating" }]);
    try {
      const source = await createKnowledge({
        name: file.name, type: "document", detail: file.name, scope: "bot", botId,
        sizeKb: Math.max(1, Math.round(file.size / 1024)),
      });
      patch(id, { state: "uploading" });
      const up = await uploadKnowledgeDocument(source.id, file);
      if (up.duplicate) toast(`“${file.name}” was already uploaded — reusing the indexed copy`, "info");
      patch(id, { state: "processing", documentId: up.documentId, stage: up.status, progress: 0 });
      onAdded();
      await pollDoc(id, up.documentId);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Upload failed";
      patch(id, { state: "error", error: msg });
      toast(msg, "error");
    }
  };

  const retryQueued = async (it: QueueItem) => {
    if (!it.documentId) return;
    patch(it.id, { state: "processing", error: undefined, progress: 0, stage: "retrying" });
    try {
      await retryDocument(it.documentId);
    } catch (e) {
      patch(it.id, { state: "error", error: e instanceof Error ? e.message : "Retry failed" });
      toast(e instanceof Error ? e.message : "Retry failed", "error");
      return;
    }
    await pollDoc(it.id, it.documentId);
  };

  const addDocument = () => {
    const file = fileRef.current?.files?.[0];
    if (!file) { setDocErr("Choose a file to upload"); return; }
    void uploadDoc(file);
    if (fileRef.current) fileRef.current.value = "";
  };

  const addUrl = () => {
    if (!/^https?:\/\/[^\s]+\.[^\s]+/.test(url)) { setUrlErr("Enter a full URL, e.g. https://example.com/help"); return; }
    void add(url.replace(/^https?:\/\//, ""), "url", url);
    setUrl("");
  };

  const addFaq = () => {
    if (!faqQuestion.trim() || !faqAnswer.trim()) { toast("Enter both a question and an answer", "error"); return; }
    void add(faqQuestion.trim(), "faq", "Curated Q&A pair");
    setFaqQuestion("");
    setFaqAnswer("");
  };

  return (
    <Modal open={open} onClose={onClose} title="Add knowledge" sub="New sources index into the draft version; publishing makes them live." wide
      footer={<Button variant="primary" onClick={onClose}>Done</Button>}>
      <div className="col gap-16">
        <div className="segmented" role="group" aria-label="Source type">
          {(["document", "url", "faq"] as const).map((m) => (
            <button key={m} aria-pressed={mode === m} onClick={() => setMode(m)} style={{ textTransform: "capitalize" }}>{m === "faq" ? "FAQ pairs" : `${m}s`}</button>
          ))}
        </div>

        {mode === "document" && (
          <Field label="Document file" error={docErr} hint="PDF, DOCX, XLSX, PPTX, TXT, MD, CSV or JSON · up to 25 MB · text is chunked and embedded automatically.">
            <div className="row gap-8">
              <input
                ref={fileRef}
                className="input"
                type="file"
                accept=".pdf,.docx,.txt,.md,.csv,.xlsx,.pptx,.json"
                onChange={() => setDocErr("")}
                aria-invalid={!!docErr}
                aria-label="Document file"
              />
              <Button variant="primary" icon="upload" onClick={addDocument}>Upload</Button>
            </div>
          </Field>
        )}

        {mode === "url" && (
          <Field label="Page or sitemap URL" error={urlErr} hint="The crawler follows same-domain links up to depth 2.">
            <div className="row gap-8">
              <input className="input" value={url} onChange={(e) => { setUrl(e.target.value); setUrlErr(""); }} placeholder="https://example.com/services" aria-invalid={!!urlErr} />
              <Button variant="primary" onClick={addUrl}>Add</Button>
            </div>
          </Field>
        )}

        {mode === "faq" && (
          <div className="col gap-8">
            <Field label="Question"><input className="input" value={faqQuestion} onChange={(e) => setFaqQuestion(e.target.value)} placeholder="Do you validate parking?" /></Field>
            <Field label="Answer"><textarea className="textarea" value={faqAnswer} onChange={(e) => setFaqAnswer(e.target.value)} placeholder="Yes — parking is validated for up to 2 hours at all locations." /></Field>
            <Button variant="primary" style={{ alignSelf: "flex-start" }} onClick={addFaq}>Add FAQ pair</Button>
          </div>
        )}

        {queue.length > 0 && (
          <div className="col gap-8">
            <span className="t-label">Upload queue</span>
            {queue.map((it) => (
              <div key={it.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <Icon name="file" size={15} style={{ color: "var(--ink-3)" }} />
                <div className="grow col gap-4">
                  <span className="t-strong truncate" style={{ fontSize: 12.5, maxWidth: 300 }}>{it.name}</span>
                  {it.state === "processing" && (
                    <div className="row gap-8">
                      <Progress value={it.progress ?? 0} />
                      <span className="t-micro t-num" style={{ whiteSpace: "nowrap" }}>{it.stage || "queued"} · {Math.round(it.progress ?? 0)}%</span>
                    </div>
                  )}
                  {it.error && <span className="t-micro" style={{ color: "var(--status-critical)" }}>{it.error}</span>}
                </div>
                {(it.state === "creating" || it.state === "uploading") && <span className="spinner" />}
                {it.state === "processing" && <StatusChip status="indexing" label="processing" />}
                {it.state === "indexing" && <StatusChip status="indexing" />}
                {it.state === "ready" && <StatusChip status="indexed" label="ready" />}
                {it.state === "error" && (
                  <span className="row gap-6">
                    <StatusChip status="failed" />
                    {it.documentId && (
                      <Button size="sm" icon="refresh" onClick={() => void retryQueued(it)}>Retry</Button>
                    )}
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
        {queue.length === 0 && mode === "document" && (
          <EmptyState icon="upload" title="Queue is empty" body="Uploaded files appear here while they are chunked and embedded." />
        )}
      </div>
    </Modal>
  );
}
