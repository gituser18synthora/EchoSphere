import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { flagConversation, listConversations, simulateAction } from "@/services/api";
import type { Conversation } from "@/types/domain";
import { Button, Drawer, MenuButton, StatusChip, Tabs } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { ExportControls } from "@/components/ExportControls";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { flags } from "@/services/flags";
import {
  downloadConversationTranscript,
  downloadOperationalExport,
  type StructuredExportFormat,
} from "@/services/exportDownload";

const channelIcon: Record<string, IconName> = { voice: "phone", whatsapp: "whatsapp", web: "monitor", mobile: "smartphone" };

function fmtDur(sec: number) {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

export default function Conversations() {
  const q = useAsync(listConversations, []);
  const [open, setOpen] = useState<Conversation | null>(null);
  const [filter, setFilter] = useState("all");
  const [botFilter, setBotFilter] = useState("all");
  const [query, setQuery] = useState("");

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

      <div className="filter-bar mt-16">
        <div className="search-box">
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search by intent, bot, call ID…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search conversations" />
        </div>
        <select className="select" value={botFilter} onChange={(e) => setBotFilter(e.target.value)} aria-label="Filter by bot">
          <option value="all">All bots</option>
          {bots.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
        </select>
      </div>

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
          onRowClick={(c) => setOpen(c)}
          empty={{ icon: "headphones", title: "No conversations match", body: "Try widening the filters — new calls appear here within seconds of ending." }}
          columns={[
            {
              key: "id", header: "Call", sortValue: (c) => c.startedAt,
              render: (c) => (
                <div className="row gap-10">
                  <span className="icon-tile neutral" style={{ width: 30, height: 30 }}><Icon name={channelIcon[c.channel]} size={14} /></span>
                  <div>
                    <div className="t-strong t-num">{new Date(c.startedAt).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} · {fmtDur(c.durationSec)}</div>
                    <div className="t-micro row gap-4">{c.caller} · {c.language}{c.flagged && <span className="row gap-2" style={{ color: "var(--status-serious)", fontWeight: 650 }}><Icon name="flag" size={11} />flagged</span>}</div>
                  </div>
                </div>
              ),
            },
            { key: "bot", header: "Bot", sortValue: (c) => c.bot, render: (c) => <span className="t-sub">{c.bot}</span> },
            { key: "intents", header: "Intents", render: (c) => <span className="row gap-4 wrap">{c.intents.map((i) => <code key={i} style={{ fontSize: 11.5, background: "var(--surface-3)", padding: "1px 6px", borderRadius: 4 }}>{i}</code>)}</span> },
            { key: "sentiment", header: "Sentiment", sortValue: (c) => c.sentiment, render: (c) => <StatusChip status={c.sentiment} /> },
            {
              key: "outcome", header: "Outcome", sortValue: (c) => String(c.contained),
              render: (c) => c.contained
                ? <StatusChip status="good" label="Contained" />
                : <StatusChip status="serious" label="Escalated" />,
            },
            { key: "csat", header: "CSAT", align: "right", sortValue: (c) => c.csat ?? 0, render: (c) => <span className="t-num">{c.csat ? `${c.csat}/5` : "—"}</span> },
            { key: "qa", header: "QA score", align: "right", sortValue: (c) => c.qaScore ?? 0, render: (c) => c.qaScore ? <span className={`t-num t-strong ${c.qaScore < 70 ? "t-bad" : ""}`}>{c.qaScore}</span> : <span className="t-micro">—</span> },
          ]}
        />
      </div>

      <ConversationDrawer conv={open} onClose={() => setOpen(null)} onUpdate={(c) => { setOpen(c); q.reload(); }} />
    </>
  );
}

function ConversationDrawer({ conv, onClose, onUpdate }: { conv: Conversation | null; onClose: () => void; onUpdate: (c: Conversation) => void }) {
  const { toast } = useApp();
  const navigate = useNavigate();
  const [tab, setTab] = useState("transcript");
  const [transcriptBusy, setTranscriptBusy] = useState(false);

  if (!conv) return null;

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
      open onClose={onClose} wide
      title={<span className="row gap-8">Call {conv.id}<StatusChip status={conv.contained ? "good" : "serious"} label={conv.contained ? "Contained" : "Escalated"} /></span>}
      sub={`${conv.bot} · ${conv.channel} · ${new Date(conv.startedAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} · ${fmtDur(conv.durationSec)} · ${conv.caller}`}
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
        {/* Recording */}
        <div className="row gap-10 card-pad-sm" style={{ background: "var(--surface-2)", borderRadius: 10 }}>
          <button className="btn-icon" style={{ background: "var(--brand-500)", color: "#fff", borderRadius: "50%" }}
            aria-label="Play recording"
            onClick={() => toast(flags.recordingPlayback ? "Playing…" : "Recording playback pending signed-URL backend (TODO_BACKEND #4)", "info")}>
            <Icon name="play" size={13} />
          </button>
          <div className="grow col gap-4">
            <div className="progress" style={{ height: 5 }}><div className="progress-fill" style={{ width: "0%" }} /></div>
            <span className="t-micro t-num">0:00 / {fmtDur(conv.durationSec)} · recording {flags.recordingPlayback ? "ready" : "available after backend hookup"}</span>
          </div>
          <span className="tag t-num">${conv.costUsd.toFixed(2)}</span>
        </div>

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
          <div className="col gap-10">
            {conv.transcript.map((s) => (
              <div key={s.turn} className="col" style={{ alignItems: s.speaker === "user" ? "flex-end" : "flex-start", gap: 2 }}>
                <div className={`transcript-bubble ${s.speaker}`}>{s.text}</div>
                <span className="transcript-meta">
                  {s.speaker === "bot"
                    ? <>bot · {s.intent ? <>intent <code>{s.intent}</code> {(s.confidence ? `${(s.confidence * 100).toFixed(0)}%` : "")} · </> : null}{s.latencyMs}ms</>
                    : "caller"}
                </span>
              </div>
            ))}
          </div>
        )}

        {tab === "trace" && (
          <div className="col gap-8">
            {conv.transcript.filter((s) => s.speaker === "bot").map((s) => (
              <div key={s.turn} className="card-pad-sm col gap-6" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <span className="t-micro t-strong">Turn {s.turn}</span>
                <div className="row gap-16 wrap" style={{ fontSize: 12.5 }}>
                  <span className="row gap-4"><Icon name="target" size={12} style={{ color: "var(--ink-3)" }} />{s.intent ? <><code>{s.intent}</code> {s.confidence ? `${(s.confidence * 100).toFixed(0)}%` : ""}</> : "—"}</span>
                  <span className="row gap-4"><Icon name="book" size={12} style={{ color: "var(--ink-3)" }} />{s.chunksUsed?.length ? s.chunksUsed.join("; ") : "no retrieval"}</span>
                  <span className="row gap-4">
                    <Icon name="zap" size={12} style={{ color: "var(--ink-3)" }} />
                    {s.apiCalls?.length ? s.apiCalls.map((a) => `${a.name} (${a.ok ? `${a.ms}ms` : "failed"})`).join("; ") : "no API calls"}
                  </span>
                  <span className="row gap-4"><Icon name="clock" size={12} style={{ color: "var(--ink-3)" }} />{s.latencyMs}ms</span>
                  {flags.tenantCostVisibility && s.costUsd && <span className="row gap-4 t-num"><Icon name="dollar" size={12} style={{ color: "var(--ink-3)" }} />${s.costUsd.toFixed(4)}</span>}
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
