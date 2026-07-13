import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getTenantAnalytics, listBots, listConversations } from "@/services/api";
import { Button, CardSkeleton, ErrorState, Health, KpiCard, StatusChip } from "@/components/ui";
import { ChartCard, Donut, LineChart, Legend } from "@/components/charts";
import { Icon } from "@/components/Icon";

export default function TenantDashboard() {
  const navigate = useNavigate();
  const a = useAsync(() => getTenantAnalytics(30), []);
  const botsQ = useAsync(listBots, []);
  const convQ = useAsync(listConversations, []);

  if (a.error) return <ErrorState message={a.error} onRetry={a.reload} />;

  const liveBots = (botsQ.data ?? []).filter((b) => b.status === "published");
  const needsAttention = (botsQ.data ?? []).filter((b) => b.health === "warning" || b.health === "serious" || b.health === "critical");

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Good morning, Priya</h1>
          <p className="page-sub">Meridian Health Group · {liveBots.length} bots live · last 30 days</p>
        </div>
        <div className="page-actions">
          <Button icon="headphones" onClick={() => navigate("/t/conversations")}>Review conversations</Button>
          <Button variant="primary" icon="plus" onClick={() => navigate("/t/bots")}>Create bot</Button>
        </div>
      </div>

      <div className="grid grid-6">
        {a.loading
          ? Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={1} />)
          : a.data!.kpis.map((k) => <KpiCard key={k.label} {...k} />)}
      </div>

      <div className="grid grid-2 mt-16" style={{ gridTemplateColumns: "1.6fr 1fr" }}>
        <ChartCard
          title="Calls & containment"
          sub="Daily calls vs calls resolved without a human"
          legend={<Legend shape="line" items={[{ label: "Total calls", color: "var(--series-1)" }, { label: "Contained", color: "var(--series-2)" }]} />}
        >
          {a.data ? (
            <LineChart
              data={a.data.callsSeries} x="t"
              series={[{ key: "calls", label: "Total calls", area: true }, { key: "contained", label: "Contained", color: "var(--series-2)" }]}
              height={240}
            />
          ) : <CardSkeleton rows={6} />}
        </ChartCard>
        <ChartCard title="Caller sentiment" sub="Share of calls, last 30 days">
          {a.data ? <Donut data={a.data.sentimentSplit.map((s, i) => ({ ...s, color: [`var(--viz-good)`, `var(--status-neutral)`, `var(--viz-critical)`][i] }))} centerValue="58%" centerLabel="positive" /> : <CardSkeleton rows={6} />}
        </ChartCard>
      </div>

      <div className="grid grid-2 mt-16">
        {/* Bot fleet */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Your VoiceBots</span>
            <Button variant="ghost" size="sm" onClick={() => navigate("/t/bots")}>View all</Button>
          </div>
          <div className="col" style={{ padding: 14, gap: 8 }}>
            {botsQ.loading && <CardSkeleton rows={4} />}
            {(botsQ.data ?? []).slice(0, 4).map((b) => (
              <button key={b.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, textAlign: "left" }} onClick={() => navigate(`/t/bots/${b.id}/overview`)}>
                <span className="icon-tile brand" style={{ width: 32, height: 32 }}><Icon name="bot" size={15} /></span>
                <span className="grow">
                  <span className="t-strong" style={{ fontSize: 13, display: "block" }}>{b.name}</span>
                  <span className="t-micro">{b.callsToday} calls today · {b.containment ? `${b.containment}% contained` : "not live"}</span>
                </span>
                <Health level={b.health} label="" />
                <StatusChip status={b.status} />
              </button>
            ))}
          </div>
        </div>

        {/* Action center */}
        <div className="card">
          <div className="card-header">
            <span className="card-title">Needs your attention</span>
            <span className="chip chip-warning"><span className="chip-dot" />{(a.data?.recommendations.length ?? 0) + needsAttention.length} items</span>
          </div>
          <div className="col" style={{ padding: 14, gap: 8 }}>
            {a.loading && <CardSkeleton rows={4} />}
            {a.data?.recommendations.slice(0, 3).map((r) => (
              <button key={r.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, textAlign: "left", alignItems: "flex-start" }} onClick={() => navigate(r.link)}>
                <span className={`icon-tile ${r.impact === "high" ? "critical" : "warning"}`} style={{ width: 32, height: 32 }}>
                  <Icon name={r.impact === "high" ? "alert" : "info"} size={15} />
                </span>
                <span className="grow">
                  <span className="t-strong" style={{ fontSize: 13, display: "block" }}>{r.title}</span>
                  <span className="t-micro" style={{ display: "block", marginTop: 2 }}>{r.detail}</span>
                </span>
                <Icon name="chevron-right" size={14} style={{ color: "var(--ink-3)", flexShrink: 0, marginTop: 4 }} />
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Recent escalations */}
      <div className="card mt-16">
        <div className="card-header">
          <span className="card-title">Recent escalations</span>
          <Button variant="ghost" size="sm" onClick={() => navigate("/t/conversations")}>Open Conversation Review</Button>
        </div>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Time</th><th>Bot</th><th>Intent</th><th>Escalation reason</th><th>Sentiment</th><th></th></tr>
            </thead>
            <tbody>
              {(convQ.data ?? []).filter((c) => !c.contained).slice(0, 3).map((c) => (
                <tr key={c.id} className="row-click" onClick={() => navigate("/t/conversations")}>
                  <td className="t-num t-sub">{new Date(c.startedAt).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}</td>
                  <td className="t-strong">{c.bot}</td>
                  <td><code style={{ fontSize: 12 }}>{c.intents[0]}</code></td>
                  <td className="t-sub" style={{ maxWidth: 340 }}>{c.escalationReason}</td>
                  <td><StatusChip status={c.sentiment} /></td>
                  <td><Icon name="chevron-right" size={14} style={{ color: "var(--ink-3)" }} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
