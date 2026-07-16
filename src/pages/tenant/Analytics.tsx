import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getTenantAnalytics } from "@/services/api";
import { Button, CardSkeleton, ErrorState, KpiCard } from "@/components/ui";
import { ChartCard, Donut, HBarList, Legend, LineChart, fmtNum } from "@/components/charts";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

export default function Analytics() {
  const [range, setRange] = useState(30);
  const a = useAsync(() => getTenantAnalytics(range), [range]);
  const navigate = useNavigate();
  const { user, toast } = useApp();

  if (a.error) return <ErrorState message={a.error} onRetry={a.reload} />;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Analytics</h1>
          <p className="page-sub">All bots · {user?.tenantName ?? ""}</p>
        </div>
        <div className="page-actions">
          <div className="segmented" role="group" aria-label="Date range">
            {[7, 30, 90].map((d) => <button key={d} aria-pressed={range === d} onClick={() => setRange(d)}>{d}d</button>)}
          </div>
          <Button icon="download" onClick={() => toast("Export queued — backend job API pending (TODO_BACKEND #6)", "info")}>Export</Button>
        </div>
      </div>

      <div className="grid grid-6">
        {a.loading || !a.data
          ? Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={1} />)
          : a.data.kpis.map((k) => <KpiCard key={k.label} {...k} />)}
      </div>

      {a.data && (
        <>
          <div className="grid grid-2 mt-16" style={{ gridTemplateColumns: "1.6fr 1fr" }}>
            <ChartCard
              title="Calls & containment"
              sub={`Daily · last ${range} days`}
              legend={<Legend shape="line" items={[{ label: "Total calls", color: "var(--series-1)" }, { label: "Contained", color: "var(--series-2)" }]} />}
            >
              <LineChart data={a.data.callsSeries} x="t" height={240}
                series={[{ key: "calls", label: "Total calls", area: true }, { key: "contained", label: "Contained", color: "var(--series-2)" }]} />
            </ChartCard>
            <ChartCard title="Language mix" sub="Share of calls">
              <Donut data={a.data.languageMix} centerValue={`${a.data.languageMix[0].value}%`} centerLabel={a.data.languageMix[0].label} />
            </ChartCard>
          </div>

          <div className="grid grid-2 mt-16">
            <ChartCard title="Top intents" sub="With period-over-period trend">
              <HBarList data={a.data.topIntents.map((t) => ({ label: t.label, value: t.value }))} trend={a.data.topIntents.map((t) => t.trend)} />
            </ChartCard>
            <ChartCard
              title="Cost breakdown"
              sub="Daily USD by component"
              legend={<Legend items={[
                { label: "LLM", color: "var(--series-1)" }, { label: "TTS", color: "var(--series-2)" },
                { label: "STT", color: "var(--series-3)" }, { label: "Telephony", color: "var(--series-4)" },
              ]} />}
            >
              <LineChart data={a.data.costSeries} x="t" height={230} yFmt={(v) => `$${fmtNum(v)}`}
                series={[
                  { key: "llm", label: "LLM" }, { key: "tts", label: "TTS" },
                  { key: "stt", label: "STT" }, { key: "telephony", label: "Telephony" },
                ]} />
            </ChartCard>
          </div>

          <div className="card mt-16">
            <div className="card-header">
              <div className="col gap-2">
                <span className="card-title">Recommendations</span>
                <span className="t-micro">Generated from escalation causes, confidence trends and knowledge gaps</span>
              </div>
            </div>
            <div className="grid grid-2" style={{ padding: 16, gap: 10 }}>
              {a.data.recommendations.map((r) => (
                <button key={r.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, textAlign: "left", alignItems: "flex-start" }}
                  onClick={() => navigate(r.link)}>
                  <span className={`icon-tile ${r.impact === "high" ? "critical" : r.impact === "medium" ? "warning" : "neutral"}`} style={{ width: 32, height: 32 }}>
                    <Icon name="sparkles" size={15} />
                  </span>
                  <span className="grow">
                    <span className="row gap-8">
                      <span className="t-strong" style={{ fontSize: 13 }}>{r.title}</span>
                      <span className={`chip chip-${r.impact === "high" ? "critical" : r.impact === "medium" ? "warning" : "neutral"}`}>{r.impact} impact</span>
                    </span>
                    <span className="t-micro" style={{ display: "block", marginTop: 3 }}>{r.detail}</span>
                  </span>
                  <Icon name="chevron-right" size={14} style={{ color: "var(--ink-3)", marginTop: 6 }} />
                </button>
              ))}
            </div>
          </div>
        </>
      )}
    </>
  );
}
