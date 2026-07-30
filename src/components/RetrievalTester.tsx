import { useState } from "react";
import type { KnowledgeSource, SearchTestResult, SearchTestSource } from "@/types/domain";
import { searchTest } from "@/services/api";
import { Button, Callout, MultiSelect, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";

/** Retrieval test console — used in the source drawer (fixed KB) and on the
    Knowledge Hub page (selectable KBs; empty selection = all tenant KBs).
    Shows every score the pipeline produced plus its stage diagnostics, so a
    zero-result query is explainable instead of silent. */
export function RetrievalTester({ kbIds, kbOptions }: {
  /** Fixed KB scope (drawer). Omit to let the user pick from kbOptions. */
  kbIds?: string[];
  /** Selectable sources (hub). Empty selection searches all tenant KBs. */
  kbOptions?: KnowledgeSource[];
}) {
  const [query, setQuery] = useState("");
  const [selectedKbs, setSelectedKbs] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<SearchTestResult | null>(null);

  const kbNameById = new Map((kbOptions ?? []).map((s) => [s.id, s.name]));
  const effectiveKbs = kbIds ?? (selectedKbs.length ? selectedKbs : undefined);

  const run = async () => {
    const trimmed = query.trim();
    if (!trimmed || running) return;
    setRunning(true);
    setError(null);
    try {
      setResult(await searchTest({ query: trimmed, kbIds: effectiveKbs }));
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : "Retrieval test failed");
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="col gap-8">
      {kbOptions && (
        <MultiSelect
          options={kbOptions.map((s) => ({ value: s.id, label: s.name, sub: `${s.chunks} chunks · ${s.status}` }))}
          selected={selectedKbs}
          onChange={setSelectedKbs}
          placeholder="All knowledge bases"
          searchPlaceholder="Filter knowledge bases…"
        />
      )}
      <textarea
        className="textarea"
        rows={2}
        value={query}
        placeholder="Ask a question this knowledge should answer…"
        aria-label="Retrieval test query"
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void run(); }}
      />
      <Button icon="search" busy={running} disabled={!query.trim()} style={{ alignSelf: "flex-start" }} onClick={() => void run()}>
        Run retrieval test
      </Button>

      {error && (
        <Callout tone="critical" title="Retrieval request failed">
          <div className="col gap-8" style={{ alignItems: "flex-start" }}>
            <span>{error}</span>
            <Button size="sm" icon="refresh" onClick={() => void run()}>Try again</Button>
          </div>
        </Callout>
      )}

      {result && !error && (
        <>
          <div className="row gap-8 wrap">
            <StatusChip status={result.answerable ? "good" : "warning"} label={result.answerable ? "Answerable" : "Not answerable"} />
            <span className="t-micro t-num">
              confidence {result.confidence.toFixed(2)} · {Math.round(result.durationMs)}ms
              {result.kbIds.length > 0 && ` · ${result.kbIds.length} KB${result.kbIds.length === 1 ? "" : "s"} searched`}
            </span>
          </div>

          {!result.answerable && (
            <Callout tone="warning" title={zeroReasonTitle(result)}>
              {zeroReasonBody(result)}
            </Callout>
          )}

          {result.sources.map((s) => (
            <SourceCard key={s.chunkId} source={s} kbName={kbNameById.get(s.kbId)} answerable={result.answerable} />
          ))}

          {result.diagnostics && <DiagnosticsPanel diag={result.diagnostics} />}
        </>
      )}
    </div>
  );
}

function zeroReasonTitle(result: SearchTestResult): string {
  switch (result.skippedReason) {
    case "no_authorized_knowledge_bases": return "No searchable knowledge bases";
    case "empty_query": return "Empty query";
    case "no_matching_chunks": return "No chunks matched";
    case "below_confidence_threshold": return "Matches found, but below the relevance thresholds";
    default: return "No results";
  }
}

function zeroReasonBody(result: SearchTestResult): string {
  const d = result.diagnostics;
  switch (result.skippedReason) {
    case "no_authorized_knowledge_bases":
      return "None of the selected knowledge bases is indexed and searchable. Upload documents or wait for indexing to finish.";
    case "no_matching_chunks":
      return d && d.kbCount > 0
        ? `Searched ${d.kbCount} knowledge base${d.kbCount === 1 ? "" : "s"} — neither semantic nor keyword search found any candidate chunk. The knowledge bases may be empty, or the query uses terms that appear nowhere in the indexed content.`
        : "Neither semantic nor keyword search found any candidate chunk.";
    case "below_confidence_threshold":
      return "Candidates were found but none cleared the relevance gate. The closest ones are shown below with the scores that rejected them.";
    default:
      return "The query returned no usable chunks.";
  }
}

function SourceCard({ source: s, kbName, answerable }: {
  source: SearchTestSource; kbName?: string; answerable: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const meta = Object.entries(s.meta ?? {}).filter(([k]) => k !== "file_name");
  return (
    <div className="col gap-6 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, opacity: answerable || s.passedGate ? 1 : 0.75 }}>
      <div className="row-between gap-8">
        <span className="row gap-8" style={{ minWidth: 0 }}>
          {s.rank != null && <span className="tag t-num" style={{ flexShrink: 0 }}>#{s.rank}</span>}
          <span className="t-strong truncate" style={{ fontSize: 12.5 }}>{s.documentName ?? s.documentId}</span>
        </span>
        {!answerable && !s.passedGate && <StatusChip status="warning" label="below threshold" />}
      </div>
      <div className="t-micro" style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <span>KB: {kbName ?? s.kbId}</span>
        <span className="t-num">final {s.score.toFixed(3)}</span>
        <span className="t-num">semantic {s.vectorScore != null ? s.vectorScore.toFixed(3) : "—"}</span>
        <span className="t-num">BM25 {s.keywordScore != null ? s.keywordScore.toFixed(3) : "—"}</span>
        {s.rerankScore != null && <span className="t-num">rerank {s.rerankScore.toFixed(3)}</span>}
        <span className="t-num">chunk {s.chunkIndex}{s.pageNumber != null ? ` · p.${s.pageNumber}` : ""}</span>
        {s.section && <span>§ {s.section}</span>}
      </div>
      <p className="t-sub" style={{ fontSize: 12, margin: 0, whiteSpace: "pre-wrap" }}>{s.text}</p>
      {meta.length > 0 && (
        <>
          <button
            type="button"
            className="t-micro row gap-4"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
            style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: "var(--ink-3)", alignSelf: "flex-start" }}
          >
            <Icon name={expanded ? "chevron-down" : "chevron-right"} size={12} /> metadata
          </button>
          {expanded && (
            <div className="col gap-2" style={{ paddingLeft: 16 }}>
              {meta.map(([k, v]) => (
                <span key={k} className="t-micro"><span className="t-strong">{k}</span>: {typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function DiagnosticsPanel({ diag }: { diag: NonNullable<SearchTestResult["diagnostics"]> }) {
  const [open, setOpen] = useState(false);
  const timings = Object.entries(diag.timingsMs ?? {});
  return (
    <div className="col gap-6">
      <button
        type="button"
        className="t-micro row gap-4"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: "var(--ink-3)", alignSelf: "flex-start" }}
      >
        <Icon name={open ? "chevron-down" : "chevron-right"} size={12} /> pipeline diagnostics
      </button>
      {open && (
        <div className="t-micro col gap-2 card-pad-sm" style={{ border: "1px dashed var(--hairline)", borderRadius: 10 }}>
          <span className="t-num">
            candidates: semantic {diag.denseCandidates} · keyword {diag.keywordCandidates} · merged {diag.mergedCandidates}
            {diag.afterGate != null && ` · passed gate ${diag.afterGate}`}
            {(diag.reranked ?? 0) > 0 && ` · reranked ${diag.reranked}`} · returned {diag.returned}
          </span>
          <span className="t-num">
            fusion: {diag.fusionMethod ?? "—"} (semantic {diag.semanticWeight ?? "—"} / BM25 {diag.bm25Weight ?? "—"})
            · min score {diag.minScore ?? "—"} · min keyword rank {diag.minKeywordRank ?? "—"}
          </span>
          <span className="t-num">embedder: {diag.embedder ?? "—"}{diag.embedError ? ` (failed: ${diag.embedError} — keyword-only)` : ""}</span>
          {timings.length > 0 && (
            <span className="t-num">timings: {timings.map(([k, v]) => `${k} ${v}ms`).join(" · ")}</span>
          )}
        </div>
      )}
    </div>
  );
}
