import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { getPlatformHealth, listAlerts, simulateAction } from "@/services/api";
import { Button, CardSkeleton, Health, StatusChip, Tabs, EmptyState } from "@/components/ui";
import { Sparkline } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import type { HealthMetric } from "@/types/domain";

const tabs = [
  { id: "platform", label: "Platform Health" },
  { id: "ai", label: "AI Health" },
  { id: "telephony", label: "Telephony Health" },
  { id: "alerts", label: "Alerts" },
];

const groups: Record<string, string[]> = {
  platform: ["API gateway", "Call orchestration", "Recording storage"],
  ai: ["STT latency", "LLM latency", "TTS latency", "Embedding queue"],
  telephony: ["SIP trunks", "Call orchestration"],
};

export default function Monitoring() {
  const [tab, setTab] = useState("platform");
  const health = useAsync(getPlatformHealth, []);
  const alertsQ = useAsync(listAlerts, []);
  const { toast } = useApp();
  const [acked, setAcked] = useState<Record<string, boolean>>({});

  const metricCards = (names: string[]) =>
    (health.data ?? []).filter((m) => names.includes(m.name));

  const renderMetrics = (metrics: HealthMetric[]) => (
    <div className="grid grid-3">
      {health.loading && Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} rows={2} />)}
      {metrics.map((m) => (
        <div key={m.name} className="card card-pad col gap-8">
          <div className="row-between">
            <span className="t-strong" style={{ fontSize: 13.5 }}>{m.name}</span>
            <Health level={m.status} />
          </div>
          <span className="kpi-value t-num" style={{ fontSize: 21 }}>{m.value}</span>
          <div className="row-between">
            <span className="t-micro">target {m.target}</span>
            <Sparkline data={m.spark} width={110} height={26}
              color={m.status === "critical" ? "var(--viz-critical)" : m.status === "warning" ? "var(--viz-warning)" : "var(--series-2)"} />
          </div>
        </div>
      ))}
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Monitoring</h1>
          <p className="page-sub">Real-time platform, AI and telephony signals · 24h window</p>
        </div>
        <div className="page-actions">
          <Button icon="refresh" onClick={() => { health.reload(); alertsQ.reload(); toast("Metrics refreshed"); }}>Refresh</Button>
        </div>
      </div>
      <Tabs
        tabs={tabs.map((t) => t.id === "alerts" ? { ...t, count: (alertsQ.data ?? []).filter((a) => a.status !== "resolved").length } : t)}
        active={tab}
        onChange={setTab}
      />
      <div className="mt-16">
        {tab !== "alerts" && renderMetrics(metricCards(groups[tab] ?? []))}
        {tab === "alerts" && (
          <div className="card">
            {alertsQ.loading ? <div style={{ padding: 16 }}><CardSkeleton rows={4} /></div>
              : (alertsQ.data ?? []).length === 0 ? <EmptyState icon="check-circle" title="No alerts" body="All systems nominal." />
              : (
                <div className="col" style={{ padding: 16, gap: 10 }}>
                  {(alertsQ.data ?? []).map((a) => (
                    <div key={a.id} className="row gap-12 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, alignItems: "flex-start" }}>
                      <span className={`health-dot ${a.severity}`} style={{ marginTop: 5 }} />
                      <div className="grow">
                        <div style={{ fontSize: 13, fontWeight: 600 }}>{a.title}</div>
                        <div className="t-micro mt-4">{a.source} · {new Date(a.time).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })} · scope: {a.scope}</div>
                      </div>
                      <StatusChip status={acked[a.id] ? "acknowledged" : a.status} />
                      {a.status === "open" && !acked[a.id] && (
                        <Button size="sm" onClick={async () => { await simulateAction("ack"); setAcked((x) => ({ ...x, [a.id]: true })); toast("Alert acknowledged — on-call notified"); }}>
                          Acknowledge
                        </Button>
                      )}
                    </div>
                  ))}
                </div>
              )}
          </div>
        )}
      </div>
    </>
  );
}
