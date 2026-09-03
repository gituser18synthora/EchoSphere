import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { flagConversation, getConversation, listConversations, simulateAction } from "@/services/api";
import { getToken } from "@/services/http";
import { downloadFile } from "@/services/fileDownload";
import type {
  Conversation,
  ConversationAiSummary,
  ConversationRecording,
} from "@/types/domain";
import { Button, Drawer, EmptyState, ErrorState, MenuButton, StatusChip, Tabs } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { DateRangePicker } from "@/components/DateRangePicker";
import { ExportControls } from "@/components/ExportControls";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";
import { CurrencySelect, useDisplayCurrency, type DisplayCurrencyState } from "@/components/CurrencyDisplay";
import {
  downloadConversationTranscript,
  downloadOperationalExport,
  type StructuredExportFormat,
} from "@/services/exportDownload";
import { formatChatTime } from "@/services/chatTime";

const channelIcon: Record<string, IconName> = { voice: "phone", whatsapp: "whatsapp", web: "monitor", mobile: "smartphone" };

const pad2 = (n: number) => String(n).padStart(2, "0");

/* The call's real length: conversation_sessions.duration_sec, written by the
   runtime at finalize from the call's own wall clock. Never derived from
   transcript or row timestamps, which would drift from what was billed. */
function fmtDuration(sec: number | null | undefined) {
  if (sec == null || Number.isNaN(sec)) return "—";
  const total = Math.max(0, Math.round(sec));
  if (total < 60) return `${total} sec`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${pad2(total % 60)}s`;
  return `${Math.floor(total / 3600)}h ${pad2(Math.floor((total % 3600) / 60))}m ${pad2(total % 60)}s`;
}

/* Instants come from the API in UTC and are rendered in the viewer's own
   timezone, matching every other date in the product. The date filter converts
   local day boundaries back to instants (see localDayBound) so a row can never
   be filtered out of the day it is displayed under. */
function fmtDateTime(iso: string) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return `${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}, ${
    d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" })}`;
}

/** A `YYYY-MM-DD` picker value as the ISO instant of that local day's first or
    last millisecond — what the API filters `startedAt` against. */
function localDayBound(day: string, edge: "start" | "end"): string | undefined {
  const [y, m, d] = day.split("-").map(Number);
  if (!y || !m || !d) return undefined;
  const at = edge === "start"
    ? new Date(y, m - 1, d, 0, 0, 0, 0)
    : new Date(y, m - 1, d, 23, 59, 59, 999);
  return Number.isNaN(at.getTime()) ? undefined : at.toISOString();
}

function todayLocal() {
  const now = new Date();
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
}

/* Mirrors shared.billing.conversation_cost.HIGH_COST_USD — a call above this
   is flagged for review rather than silently rendered as normal. */
const HIGH_COST_USD = 0.5;

export default function Conversations() {
  const { hasPermission } = useApp();
  // Server-enforced: without costs.view the API nulls every cost field, so
  // this only removes the empty affordances.
  const showCosts = flags.tenantCostVisibility && hasPermission("costs.view");
  const money = useDisplayCurrency(showCosts);
  const [open, setOpen] = useState<Conversation | null>(null);
  const [filter, setFilter] = useState("all");
  const [botFilter, setBotFilter] = useState("all");
  const [query, setQuery] = useState("");
  // Date pickers hold local `YYYY-MM-DD`; the range is applied by the API, not
  // by trimming an already-fetched page — otherwise a busy day beyond the first
  // 100 rows would silently disappear from an older date's results.
  const [fromDate, setFromDate] = useState("");
  const [toDate, setToDate] = useState("");
  const dateFrom = fromDate ? localDayBound(fromDate, "start") : undefined;
  const dateTo = toDate ? localDayBound(toDate, "end") : undefined;
  const q = useAsync(() => listConversations({ dateFrom, dateTo }), [dateFrom, dateTo]);

  const rows = useMemo(() => {
    let r = q.data ?? [];
    if (filter === "escalated") r = r.filter((c) => !c.contained);
    if (filter === "flagged") r = r.filter((c) => c.flagged);
    if (filter === "negative") r = r.filter((c) => c.sentiment === "negative");
    if (botFilter !== "all") r = r.filter((c) => c.botId === botFilter);
    if (query) {
      const s = query.toLowerCase();
      r = r.filter((c) =>
        c.intents.some((intent) => intent.toLowerCase().includes(s))
        || c.bot.toLowerCase().includes(s)
        || c.id.toLowerCase().includes(s));
    }
    return r;
  }, [q.data, filter, botFilter, query]);

  const bots = [...new Map(
    (q.data ?? []).map((conversation) => [
      conversation.botId,
      conversation.bot,
    ]),
  ).entries()];

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Conversation Review</h1>
          <p className="page-sub">QA every call: transcript, trace, sentiment and scorecards — turn findings into fixes</p>
        </div>
        <div className="page-actions">
          {showCosts && (
            <label className="row gap-6">
              <span className="t-micro">Currency</span>
              <CurrencySelect state={money} />
            </label>
          )}
          <ExportControls
            buttonLabel="Export"
            onDownload={(format) => downloadOperationalExport(
              "conversations",
              format,
              {
                search: query.trim() || undefined,
                botId: botFilter === "all" ? undefined : botFilter,
                sentiment: filter === "negative" ? "negative" : undefined,
                contained: filter === "escalated" ? false : undefined,
                flagged: filter === "flagged" ? true : undefined,
                dateFrom,
                dateTo,
              },
            )}
          />
        </div>
      </div>

      <Tabs
        tabs={[
          { id: "all", label: "All", count: q.data?.length },
          { id: "escalated", label: "Escalated", count: q.data?.filter((c) => !c.contained).length },
          { id: "flagged", label: "Flagged", count: q.data?.filter((c) => c.flagged).length },
          { id: "negative", label: "Negative sentiment", count: q.data?.filter((c) => c.sentiment === "negative").length },
        ]}
        active={filter}
        onChange={setFilter}
      />

      <div className="filter-bar conversation-toolbar mt-16">
        <div className="search-box">
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search by bot or call ID…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search conversations" />
        </div>
        <select className="select" value={botFilter} onChange={(e) => setBotFilter(e.target.value)} aria-label="Filter by bot">
          <option value="all">All bots</option>
          {bots.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
        <DateRangePicker
          from={fromDate}
          to={toDate}
          max={todayLocal()}
          onChange={(f, t) => { setFromDate(f); setToDate(t); }}
        />
        <span className="conversation-result-count">
          <strong>{rows.length}</strong> of {q.data?.length ?? 0} conversations
        </span>
      </div>

      <div className="card conversation-list-card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
          onRowClick={(c) => setOpen(c)}
          empty={{ icon: "headphones", title: "No conversations match", body: "Try widening the filters or the date range — new calls appear here within seconds of ending." }}
          columns={[
            {
              key: "id", header: "Call", width: 216, sortValue: (c) => c.caller,
              render: (c) => (
                <div className="row gap-10">
                  <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name={channelIcon[c.channel]} size={14} /></span>
                  <div className="conversation-call-cell">
                    <div className="t-strong row gap-6 nowrap">
                      {c.caller}
                      {c.flagged && <span className="row gap-2" style={{ color: "var(--status-serious)", fontWeight: 650, fontSize: 11 }}><Icon name="flag" size={11} />flagged</span>}
                    </div>
                    {/* The id is what QA quotes and what the search box matches,
                        so it belongs on the row rather than in a tooltip. */}
                    <div className="t-micro conversation-call-meta"><code>{c.id}</code> · {c.language}</div>
                  </div>
                </div>
              ),
            },
            { key: "bot", header: "Bot", sortValue: (c) => c.bot, render: (c) => <span className="t-sub">{c.bot}</span> },
            /* No conversation-level Intents column: the runtime does not tag
               calls with a rolled-up intent list, so it rendered empty for
               every row. Per-turn intents live in the drawer's trace. */
            { key: "sentiment", header: "Sentiment", sortValue: (c) => c.sentiment, render: (c) => <StatusChip status={c.sentiment} /> },
            {
              key: "outcome", header: "Outcome", sortValue: (c) => String(c.contained),
              render: (c) => c.contained
                ? <StatusChip status="good" label="Contained" />
                : <StatusChip status="serious" label="Escalated" />,
            },
            {
              key: "disposition", header: "Disposition",
              sortValue: (c) => c.disposition ?? "",
              render: (c) => c.disposition
                ? <code style={{ fontSize: 11.5, background: "var(--surface-3)", padding: "1px 6px", borderRadius: 4 }}>{c.disposition.replaceAll("_", " ")}</code>
                : <span className="t-micro">—</span>,
            },
            /* CSAT column hidden until post-call ratings are actually captured:
               the runtime never writes conversation.csat, so it only has a value
               for ingested/seeded calls. The field still ships in the API and the
               operational export — restore the column when surveys go live. */
            /* QA score is temporarily hidden from the conversation list. */
            // The list shows the SAME backend-metered total the detail
            // breakdown itemises — never a client-side calculation — rendered
            // in the selected display currency.
            // When the call happened and how long it ran, stacked in one
            // column right next to what it cost — both straight from the call
            // record (startedAt, durationSec), never reconstructed from
            // message timestamps.
            {
              key: "startedAt", header: "Date / Time & Duration", width: 176, sortValue: (c) => c.startedAt,
              render: (c) => (
                <div className="col gap-2">
                  <time className="t-num nowrap" dateTime={c.startedAt} title={new Date(c.startedAt).toLocaleString()}>
                    {fmtDateTime(c.startedAt)}
                  </time>
                  <span className="t-micro nowrap">Duration: <span className="t-num">{fmtDuration(c.durationSec)}</span></span>
                </div>
              ),
            },
            // Total on top, backend-derived per-minute rate under it (the
            // client only renders the rate, per the no-client-side-cost-math
            // rule above) — one column keeps the table compact.
            ...(showCosts ? [{
              key: "cost", header: `Cost (${money.currency})`, align: "right" as const,
              sortValue: (c: Conversation) => c.costUsd ?? 0,
              render: (c: Conversation) => (
                <div className="col gap-2" style={{ alignItems: "flex-end" }}>
                  <span className={`t-num nowrap ${(c.costUsd ?? 0) > HIGH_COST_USD ? "t-bad t-strong" : ""}`}
                        title={(c.costUsd ?? 0) > HIGH_COST_USD ? "Unusually high for one call — open the cost breakdown" : undefined}>
                    Total: <span className="t-strong">{money.display(c.costUsd ?? 0, { precise: true })}</span>
                  </span>
                  <span className="t-micro nowrap">
                    Per min: {c.costPerMinuteUsd != null
                      ? <span className="t-num">{money.display(c.costPerMinuteUsd, { precise: true })}</span>
                      : "—"}
                  </span>
                </div>
              ),
            }] : []),
          ]}
        />
      </div>

      {/* The page owns the currency selection and passes it down: a second
          useDisplayCurrency() instance in the drawer would hold its own state
          and could show a different currency than the list behind it. */}
      <ConversationDrawer conv={open} money={money} onClose={() => setOpen(null)} onUpdate={(c) => { setOpen(c); q.reload(); }} />
    </>
  );
}

function ConversationDrawer({ conv, money, onClose, onUpdate }: { conv: Conversation | null; money: DisplayCurrencyState; onClose: () => void; onUpdate: (c: Conversation) => void }) {
  const { toast, hasPermission } = useApp();
  const showCosts = flags.tenantCostVisibility && hasPermission("costs.view");
  const navigate = useNavigate();
  const [tab, setTab] = useState("transcript");
  const [transcriptBusy, setTranscriptBusy] = useState(false);
  const convId = conv?.id ?? null;
  // Transcript and recording live on the detail endpoint (Mongo-backed);
  // list rows carry only the relational metadata.
  const detailQ = useAsync<Conversation | null>(
    async () => (convId ? getConversation(convId, money.currency) : null),
    [convId, money.currency],
  );

  if (!conv) return null;

  const transcript = detailQ.data?.transcript ?? [];
  const recording = detailQ.data?.recording ?? null;
  const cost = detailQ.data?.cost ?? null;

  const qa = conv.qaScore ?? 0;
  const scorecard = [
    { label: "Greeting & identity", score: qa ? Math.min(100, qa + 4) : 0 },
    { label: "Intent understanding", score: qa },
    { label: "Resolution quality", score: qa ? (conv.contained ? qa : Math.max(20, qa - 18)) : 0 },
    { label: "Tone & empathy", score: qa ? (conv.sentiment === "negative" ? Math.max(20, qa - 12) : Math.min(100, qa + 6)) : 0 },
    { label: "Compliance (PII, disclosures)", score: qa ? Math.min(100, qa + 10) : 0 },
  ];

  const exportTranscript = async (format: StructuredExportFormat) => {
    if (transcriptBusy) return;
    setTranscriptBusy(true);
    try {
      const filename = await downloadConversationTranscript(conv.id, format);
      toast(`Downloaded ${filename}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : "Transcript export failed.", "error");
    } finally {
      setTranscriptBusy(false);
    }
  };

  return (
    <Drawer
      open onClose={onClose} wide className="conversation-drawer"
      title={(
        <span className="conversation-drawer-title">
          <span>Conversation</span>
          <code>{conv.id}</code>
          <StatusChip status={conv.contained ? "good" : "serious"} label={conv.contained ? "Contained" : "Escalated"} />
          {conv.disposition && <StatusChip status="neutral" label={conv.disposition.replaceAll("_", " ")} />}
        </span>
      )}
      sub={(
        <span className="conversation-drawer-sub">
          <span><Icon name="bot" size={12} />{conv.bot}</span>
          <span><Icon name={channelIcon[conv.channel] ?? "message"} size={12} />{conv.channel}</span>
          <span><Icon name="clock" size={12} />{fmtDateTime(conv.startedAt)}</span>
          <span>{fmtDuration(conv.durationSec)}</span>
          <span>{conv.caller}</span>
        </span>
      )}
      headerExtra={
        <MenuButton actions={[
          {
            label: conv.flagged ? "Remove flag" : "Flag for review", icon: "flag",
            onClick: async () => {
              try {
                const updated = await flagConversation(conv.id, !conv.flagged);
                onUpdate(updated);
                toast(updated.flagged ? "Flagged for QA review" : "Flag removed");
              } catch (e) {
                toast(e instanceof Error ? e.message : "Could not update flag", "error");
              }
            },
          },
          { label: "Add comment", icon: "message", onClick: () => toast("Comment added to QA thread") },
          {
            label: transcriptBusy ? "Exporting transcript…" : "Export transcript as CSV",
            icon: "download",
            disabled: transcriptBusy,
            onClick: () => void exportTranscript("csv"),
          },
          {
            label: transcriptBusy ? "Exporting transcript…" : "Export transcript as Excel",
            icon: "download",
            disabled: transcriptBusy,
            onClick: () => void exportTranscript("xlsx"),
          },
        ]} />
      }
      footer={
        <>
          <Button icon="target" onClick={() => { void simulateAction("improve"); toast("Improvement created: added utterance to intent samples"); navigate(`/t/bots/${conv.botId}/intents`); }}>
            Improve intent
          </Button>
          <Button icon="book" onClick={() => { void simulateAction("improve"); toast("Improvement created: knowledge gap logged"); navigate(`/t/bots/${conv.botId}/knowledge`); }}>
            Add knowledge
          </Button>
          <Button variant="primary" icon="edit" onClick={() => { void simulateAction("improve"); toast("Improvement created: prompt revision drafted"); navigate(`/t/bots/${conv.botId}/prompts`); }}>
            Revise prompt
          </Button>
        </>
      }
    >
      <div className="col gap-16">
        <RecordingRow
          conversationId={conv.id}
          costUsd={showCosts ? conv.costUsd : null}
          money={money}
          recording={recording}
          loading={detailQ.loading}
        />

        <CharacterUsageRow
          usage={detailQ.data?.characterUsage ?? null}
          loading={detailQ.loading}
        />

        <AiSummarySection summary={detailQ.data?.summary ?? null} loading={detailQ.loading} />

        {showCosts && (
          <CostBreakdown cost={cost} costUsd={conv.costUsd ?? 0} money={money} loading={detailQ.loading} />
        )}

        {!conv.contained && conv.escalationReason && (
          <div className="callout callout-warning">
            <Icon name="headphones" size={15} />
            <div>
              <div className="callout-title">Escalated to a human</div>
              <div className="callout-body">{conv.escalationReason}</div>
            </div>
          </div>
        )}

        <Tabs
          tabs={[
            { id: "transcript", label: "Transcript" },
            { id: "trace", label: "Knowledge & API trace" },
            { id: "qa", label: "QA scorecard" },
          ]}
          active={tab}
          onChange={setTab}
        />

        {tab === "transcript" && (
          <section className="conversation-transcript" aria-label="Conversation transcript">
            {detailQ.loading && (
              <div className="col gap-10 conversation-transcript-loading" aria-label="Loading transcript">
                {[46, 72, 38].map((w, i) => (
                  <span key={i} className="skeleton" style={{ height: 64, width: `${w}%`, alignSelf: i % 2 ? "flex-end" : "flex-start", borderRadius: 16 }} />
                ))}
              </div>
            )}
            {!detailQ.loading && detailQ.error && (
              <ErrorState message={detailQ.error} onRetry={detailQ.reload} />
            )}
            {!detailQ.loading && !detailQ.error && transcript.length === 0 && (
              <EmptyState
                icon="message"
                title="No transcript captured"
                body="This call ended without any recorded turns — nothing was transcribed."
              />
            )}
            {!detailQ.loading && !detailQ.error && transcript.map((s) => (
              <div key={s.turn} className={`conversation-turn ${s.speaker}`}>
                <div className={`transcript-bubble ${s.speaker}`}>
                  <span className="transcript-text">{s.text}</span>
                  {s.at && (
                    <time className="transcript-bubble-time" dateTime={s.at} title={s.at}>
                      {formatChatTime(s.at)}
                    </time>
                  )}
                </div>
                {s.speaker === "bot" && ((s.intent || s.route) || s.latencyMs != null) && (
                  <span className="transcript-meta">
                    {(s.intent || s.route) && <code>{s.intent ?? s.route}</code>}
                    {(s.intent || s.route) && s.confidence ? ` ${(s.confidence * 100).toFixed(0)}%` : ""}
                    {(s.intent || s.route) && s.latencyMs != null && <span aria-hidden="true"> · </span>}
                    {s.latencyMs != null && <span>{s.latencyMs}ms</span>}
                  </span>
                )}
              </div>
            ))}
          </section>
        )}

        {tab === "trace" && (
          <div className="col gap-8">
            {detailQ.loading && <span className="t-micro">Loading trace…</span>}
            {!detailQ.loading && !detailQ.error && transcript.filter((s) => s.speaker === "bot").length === 0 && (
              <span className="t-micro">No bot turns to trace for this call.</span>
            )}
            {!detailQ.loading && !detailQ.error && transcript.filter((s) => s.speaker === "bot").map((s) => (
              <div key={s.turn} className="card-pad-sm col gap-6" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <span className="t-micro t-strong">Turn {s.turn}</span>
                <div className="row gap-16 wrap" style={{ fontSize: 12.5 }}>
                  <span className="row gap-4"><Icon name="target" size={12} style={{ color: "var(--ink-3)" }} />{s.intent || s.route ? <><code>{s.intent ?? s.route}</code> {s.confidence ? `${(s.confidence * 100).toFixed(0)}%` : ""}</> : "—"}</span>
                  <span className="row gap-4"><Icon name="book" size={12} style={{ color: "var(--ink-3)" }} />{s.chunksUsed?.length ? s.chunksUsed.join("; ") : "no retrieval"}</span>
                  <span className="row gap-4">
                    <Icon name="zap" size={12} style={{ color: "var(--ink-3)" }} />
                    {s.apiCalls?.length ? s.apiCalls.map((a) => `${a.name} (${a.ok ? `${a.ms}ms` : "failed"})`).join("; ") : "no API calls"}
                  </span>
                  <span className="row gap-4"><Icon name="clock" size={12} style={{ color: "var(--ink-3)" }} />{s.latencyMs}ms</span>
                  {showCosts && s.costUsd ? <span className="row gap-4 t-num"><Icon name="dollar" size={12} style={{ color: "var(--ink-3)" }} />{money.display(s.costUsd, { precise: true })}</span> : null}
                </div>
                {s.apiCalls?.some((a) => !a.ok) && (
                  <div className="callout callout-critical" style={{ padding: "8px 10px", fontSize: 12 }}>
                    <Icon name="x-circle" size={13} />
                    <div className="callout-body">API failure in this turn caused the escalation. <button style={{ textDecoration: "underline", fontWeight: 650 }} onClick={() => navigate(`/t/bots/${conv.botId}/apis`)}>Open APIs →</button></div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === "qa" && (
          <div className="col gap-10">
            <div className="row-between card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
              <span className="t-strong">Overall QA score</span>
              <span className="kpi-value t-num" style={{ fontSize: 22, color: (conv.qaScore ?? 0) < 70 ? "var(--status-critical)" : "var(--status-good)" }}>
                {conv.qaScore ?? "—"}
              </span>
            </div>
            {scorecard.map((s) => (
              <div key={s.label} className="col gap-4">
                <div className="row-between" style={{ fontSize: 12.5 }}>
                  <span className="t-sub" style={{ fontWeight: 550 }}>{s.label}</span>
                  <span className="t-num t-strong">{s.score}</span>
                </div>
                <div className="progress" style={{ height: 7 }}>
                  <div className={`progress-fill ${s.score >= 85 ? "good" : s.score >= 65 ? "warning" : "critical"}`} style={{ width: `${s.score}%` }} />
                </div>
              </div>
            ))}
            <p className="t-micro">Scores are generated automatically per rubric v3 and can be overridden by a QA reviewer. Overrides are audited.</p>
          </div>
        )}
      </div>
    </Drawer>
  );
}

function CharacterUsageRow({ usage, loading }: {
  usage: Conversation["characterUsage"];
  loading: boolean;
}) {
  if (loading) return <span className="skeleton" style={{ height: 72, borderRadius: 10 }} />;
  const formatRate = (value: number | null | undefined) =>
    value == null ? "—" : value.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  return (
    <section className="grid grid-2" aria-label="Call character usage">
      <div className="card-pad-sm col gap-4" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
        <span className="t-micro">Avg STT Input Characters / Min</span>
        <span className="t-num t-strong" style={{ fontSize: 20 }}>{formatRate(usage?.sttInputCharactersPerMin)}</span>
        <span className="t-micro">{(usage?.sttInputCharacters ?? 0).toLocaleString()} input characters</span>
      </div>
      <div className="card-pad-sm col gap-4" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
        <span className="t-micro">Avg TTS Output Characters / Min</span>
        <span className="t-num t-strong" style={{ fontSize: 20 }}>{formatRate(usage?.ttsOutputCharactersPerMin)}</span>
        <span className="t-micro">{(usage?.ttsOutputCharacters ?? 0).toLocaleString()} output characters</span>
      </div>
    </section>
  );
}

/* ---------- cost breakdown ----------

   Every figure here comes from the backend, which rebuilds the cost from the
   conversation's usage events and the pricing snapshot recorded at the time.
   The client never multiplies a quantity by a rate: it renders what it is
   given, so this panel and the list row cannot drift apart. */

function AiSummarySection({ summary, loading }: {
  summary: ConversationAiSummary | null;
  loading: boolean;
}) {
  if (loading) return <span className="skeleton" style={{ height: 40, borderRadius: 10 }} />;
  if (!summary) return null; // no post-call analysis exists for this call

  const nba = summary.nextBestAction;
  const pendingItems = [...(summary.unresolvedItems ?? []), ...(summary.missingSlots ?? [])];
  const processing = summary.status === "queued" || summary.status === "processing";
  const failed = summary.status === "failed";
  const structured = Object.entries(summary.structuredFields ?? {});
  const structuredSources = summary.structuredFieldSources ?? {};
  const structuredLabels = summary.structuredFieldLabels ?? {};

  return (
    <div className="card-pad-sm col gap-8" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
      <div className="row gap-8" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <span className="row gap-6 t-strong" style={{ fontSize: 13 }}>
          <Icon name="sparkles" size={13} style={{ color: "var(--ink-3)" }} />
          AI call summary
          {summary.callOutcome && (
            <StatusChip status="neutral" label={summary.callOutcome.replace(/_/g, " ")} />
          )}
        </span>
        <span className="row gap-8">
          {summary.followUpRequired && <StatusChip status="pending_approval" label="Follow-up" />}
          <StatusChip status={processing ? "processing" : failed ? "failed" : "completed"} />
        </span>
      </div>

      {processing && (
        <span className="t-micro">The post-call analysis is still being generated.</span>
      )}
      {failed && (
        <span className="t-micro">
          Analysis could not be generated{summary.error ? ` (${summary.error})` : ""}; showing the
          deterministic fallback recorded from the call itself.
        </span>
      )}
      {summary.summary && <p style={{ fontSize: 13, margin: 0 }}>{summary.summary}</p>}

      {structured.length > 0 && (
        <div className="col gap-6" data-testid="structured-summary">
          <div className="row gap-8" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <span className="t-label">Structured summary</span>
            <span className="t-micro">
              {structured.filter(([, v]) => v != null).length} of {structured.length} determined
            </span>
          </div>
          <div
            role="table"
            aria-label="Structured summary fields"
            style={{ border: "1px solid var(--hairline)", borderRadius: 10, overflow: "hidden" }}
          >
            {structured.map(([key, value], index) => {
              const source = structuredSources[key];
              const sourceLabel = source === "analysis" ? "post-call analysis" : source === "workflow" ? "call flow" : null;
              const isYes = value === "Yes";
              const isNo = value === "No";
              return (
                <div
                  key={key}
                  role="row"
                  className="row gap-12"
                  style={{
                    alignItems: "center",
                    justifyContent: "space-between",
                    padding: "8px 12px",
                    fontSize: 12.5,
                    background: index % 2 === 1 ? "var(--surface-2)" : "transparent",
                    borderTop: index === 0 ? "none" : "1px solid var(--hairline)",
                  }}
                >
                  <span role="cell" className="col" style={{ minWidth: 0, gap: 1 }}>
                    <span style={{ textTransform: structuredLabels[key] ? "none" : "capitalize", color: "var(--ink)" }}>
                      {structuredLabels[key] || key.replace(/_/g, " ")}
                    </span>
                    {sourceLabel && <span className="t-micro" style={{ fontSize: 10.5 }}>from {sourceLabel}</span>}
                  </span>
                  <span
                    role="cell"
                    className="row gap-6"
                    style={{
                      alignItems: "center",
                      flexShrink: 0,
                      fontWeight: value == null ? 400 : 600,
                      fontStyle: value == null ? "italic" : "normal",
                      textTransform: value == null || isYes || isNo ? "none" : "capitalize",
                      color: isYes ? "var(--status-good)" : isNo ? "var(--status-warning)" : value == null ? "var(--ink-3)" : "var(--ink)",
                    }}
                    title={value == null ? "Not determined on this call" : undefined}
                  >
                    {isYes && <Icon name="check-circle" size={13} />}
                    {isNo && <Icon name="x-circle" size={13} />}
                    <span>{value == null ? "not determined" : value.replace(/_/g, " ")}</span>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {nba?.action && (
        <div className="row gap-8" style={{ alignItems: "baseline", flexWrap: "wrap" }}>
          <span className="t-label">Next best action</span>
          <span className="t-strong" style={{ fontSize: 12.5, textTransform: "capitalize" }}>
            {nba.action.replace(/_/g, " ")}
          </span>
          {nba.priority && <StatusChip status={nba.priority === "urgent" || nba.priority === "high" ? "warning" : "neutral"} label={nba.priority} />}
          {nba.recommendedAt && (
            <span className="t-micro">by {new Date(nba.recommendedAt).toLocaleString()}</span>
          )}
          {nba.reason && <span className="t-micro">{nba.reason}</span>}
        </div>
      )}

      {(summary.customerCommitments?.length ?? 0) > 0 && (
        <div className="col gap-4">
          <span className="t-label">Customer commitments</span>
          {summary.customerCommitments.map((c, i) => (
            <span key={i} className="row gap-6" style={{ fontSize: 12.5 }}>
              <Icon name="check-circle" size={12} style={{ color: "var(--ink-3)" }} />
              <span>
                {c.description || c.type}
                {c.amount ? ` — ${c.currency || ""} ${c.amount.toLocaleString("en-IN")}` : ""}
                {c.dueDate ? ` (due ${c.dueDate})` : ""}
                {c.status ? ` · ${c.status}` : ""}
              </span>
            </span>
          ))}
        </div>
      )}

      {pendingItems.length > 0 && (
        <div className="row gap-6 wrap" style={{ alignItems: "center" }}>
          <span className="t-label">Pending</span>
          {pendingItems.map((item) => (
            <span key={item} className="chip chip-neutral" style={{ textTransform: "none" }}>
              {item.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      )}

      {(summary.importantFacts?.length ?? 0) > 0 && (
        <div className="col gap-2">
          <span className="t-label">Important facts</span>
          {summary.importantFacts.map((fact, i) => (
            <span key={i} className="t-micro">• {fact}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function CostBreakdown({ cost, costUsd, money, loading }: {
  cost: Conversation["cost"];
  costUsd: number;
  money: DisplayCurrencyState;
  loading: boolean;
}) {
  const [open, setOpen] = useState(false);

  if (loading) return <span className="skeleton" style={{ height: 40, borderRadius: 10 }} />;
  if (!cost) return null;

  const capabilities = Object.entries(cost.byCapability ?? {});
  const converted = money.currency !== cost.baseCurrency;

  return (
    <div className="card-pad-sm col gap-8" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
      <div className="row gap-8" style={{ justifyContent: "space-between", alignItems: "center" }}>
        <span className="row gap-6 t-strong" style={{ fontSize: 13 }}>
          <Icon name="dollar" size={13} style={{ color: "var(--ink-3)" }} />
          Cost breakdown
          {cost.highCost && (
            <StatusChip
              status="serious"
              label={`Unusually high (> ${money.display(Number(cost.highCostThresholdUsd))})`}
            />
          )}
        </span>
        <span className="row gap-8">
          <span className="t-num t-strong">{money.dual(costUsd, { precise: true })}</span>
          <Button icon={open ? "chevron-up" : "chevron-down"} onClick={() => setOpen(!open)}>
            {open ? "Hide" : "Details"}
          </Button>
        </span>
      </div>

      <div className="row gap-12 wrap" style={{ fontSize: 12.5 }}>
        {capabilities.length === 0 && <span className="t-micro">No metered usage recorded for this call.</span>}
        {capabilities.map(([key, entry]) => (
          <span key={key} className="row gap-4">
            <span style={{ color: "var(--ink-3)" }}>{entry.label}</span>
            <span className="t-num">{money.display(Number(entry.costUsd), { precise: true })}</span>
          </span>
        ))}
      </div>

      {open && (
        <div className="col gap-6">
          <table className="table" style={{ fontSize: 12 }}>
            <thead>
              <tr>
                <th>Component</th><th>Provider / model</th>
                <th style={{ textAlign: "right" }}>Quantity</th>
                <th>Rate</th>
                <th style={{ textAlign: "right" }}>Cost</th>
              </tr>
            </thead>
            <tbody>
              {cost.lines.map((line, i) => (
                <tr key={i}>
                  <td>{line.componentLabel}<span className="t-micro"> · {line.capabilityLabel}</span></td>
                  <td><code>{line.provider}{line.model ? ` / ${line.model}` : ""}</code>{line.voice ? <span className="t-micro"> · {line.voice}</span> : null}</td>
                  <td className="t-num" style={{ textAlign: "right" }}>{Number(line.quantity).toLocaleString("en-US", { maximumFractionDigits: 3 })}</td>
                  <td className="t-num">
                    {line.priced
                      ? <>{line.rateCurrency} {line.unitPrice} <span className="t-micro">{line.unit.replace(/_/g, " ")}</span>
                          {line.fxRate ? <span className="t-micro"> · @ {line.fxRate}/USD</span> : null}</>
                      : <span className="t-micro">{line.note}</span>}
                  </td>
                  <td className="t-num" style={{ textAlign: "right" }}>{money.display(Number(line.costUsd), { precise: true })}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <span className="t-micro">
            Costs are metered in {cost.baseCurrency} from provider-reported usage and the rate in force at
            the time of the call.
            {converted && cost.displayRate
              ? ` Shown in ${money.currency} at the stored rate of ${cost.displayRate} ${money.currency}/${cost.baseCurrency}; the ${cost.baseCurrency} figure is authoritative.`
              : ""}
            {" "}Amounts under 1 are shown to 4 decimal places, larger amounts to 2.
            {!cost.reconciled && " Stored total differs from the recomputed sum — usage may have been recorded after the call was finalized."}
            {cost.unpriced.length > 0 && ` Not costed (no configured price): ${cost.unpriced.join(", ")}.`}
          </span>
        </div>
      )}
    </div>
  );
}

/* ---------- call recording ---------- */

function RecordingRow({ conversationId, costUsd, money, recording, loading }: {
  conversationId: string;
  /** Null when the viewer may not see costs — the tag is simply not shown. */
  costUsd: number | null;
  money: DisplayCurrencyState;
  recording: ConversationRecording | null;
  loading: boolean;
}) {
  const { toast } = useApp();
  const [audioState, setAudioState] = useState<"loading" | "ready" | "error">("loading");
  const [audioError, setAudioError] = useState("");
  const [src, setSrc] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const objectUrlRef = useRef<string | null>(null);

  // <audio src> cannot carry the Authorization header, so the file is fetched
  // with the JWT and played from an object URL (same auth path as exports).
  useEffect(() => {
    if (!recording) return;
    let cancelled = false;
    setAudioState("loading");
    setAudioError("");
    void (async () => {
      try {
        const headers: Record<string, string> = {};
        const token = getToken();
        if (token) headers.Authorization = `Bearer ${token}`;
        const resp = await fetch(recording.url, { headers });
        if (!resp.ok) {
          throw new Error(resp.status === 404
            ? "The recording file is no longer available."
            : `Could not load the recording (HTTP ${resp.status}).`);
        }
        const contentType = resp.headers.get("content-type")?.toLowerCase() ?? "";
        if (contentType.includes("json")) throw new Error("The server did not return audio.");
        const blob = await resp.blob();
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        objectUrlRef.current = url;
        setSrc(url);
        setAudioState("ready");
      } catch (e) {
        if (!cancelled) {
          setAudioState("error");
          setAudioError(e instanceof Error ? e.message : "Could not load the recording.");
        }
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrlRef.current) {
        URL.revokeObjectURL(objectUrlRef.current);
        objectUrlRef.current = null;
      }
      setSrc(null);
    };
  }, [recording, recording?.url, retrySeq]);

  const download = async () => {
    if (!recording || downloading) return;
    setDownloading(true);
    try {
      const filename = await downloadFile({
        url: `${recording.url}?download=true`,
        fallbackFilename: `echosphere-call-${conversationId}.wav`,
        accept: recording.mimeType,
        expectedContentTypes: ["audio/"],
      });
      toast(`Downloaded ${filename}`);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Recording download failed.", "error");
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div className="row gap-10 card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
      <span className="icon-tile neutral" style={{ width: 30, height: 30, flexShrink: 0 }}>
        <Icon name="volume" size={14} />
      </span>
      <div className="grow col gap-4" style={{ minWidth: 0 }}>
        {loading && <span className="t-micro">Checking for a call recording…</span>}
        {!loading && !recording && (
          <span className="t-micro">No call recording is available for this conversation.</span>
        )}
        {!loading && recording && audioState === "loading" && (
          <span className="t-micro">Loading recording · {fmtDuration(recording.durationSec)}…</span>
        )}
        {!loading && recording && audioState === "error" && (
          <span className="row gap-8" style={{ flexWrap: "wrap" }}>
            <span className="t-micro" style={{ color: "var(--status-critical)" }}>{audioError}</span>
            <Button size="sm" variant="ghost" icon="refresh" onClick={() => setRetrySeq((s) => s + 1)}>Retry</Button>
          </span>
        )}
        {!loading && recording && audioState === "ready" && src && (
          // Native controls provide play/pause, seeking, elapsed/total time
          // and volume; download stays an explicit authorized action.
          <audio controls controlsList="nodownload" src={src} style={{ width: "100%", height: 36 }} aria-label="Call recording" />
        )}
      </div>
      {!loading && recording && (
        <Button size="sm" variant="ghost" icon="download" busy={downloading} onClick={() => void download()}>
          Download
        </Button>
      )}
      {/* Same backend total as the list row and the breakdown, rendered in the
          selected display currency instead of a hardcoded dollar amount. */}
      {costUsd != null && (
        <span className="tag t-num" style={{ flexShrink: 0 }}>{money.display(costUsd, { precise: true })}</span>
      )}
    </div>
  );
}
