import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getPlatformAnalytics, getPlatformHealth, listAlerts, listTenants } from "@/services/api";
import { KpiCard, CardSkeleton, ErrorState, StatusChip, Health, Button } from "@/components/ui";
import { ChartCard, LineChart, Donut, HBarList, Legend, Sparkline, fmtNum } from "@/components/charts";
import { Icon } from "@/components/Icon";

export default function AdminDashboard() {
  const navigate = useNavigate();
  const analytics = useAsync(() => getPlatformAnalytics(30), []);
  const health = useAsync(getPlatformHealth, []);
  const alerts = useAsync(listAlerts, []);
  const tenants = useAsync(listTenants, []);

  if (analytics.error) return <ErrorState message={analytics.error} onRetry={analytics.reload} />;

  const a = analytics.data;
  const totalCalls = a ? a.callVol.reduce((s, v) => s + v, 0) : 0;
  const mrr = tenants.data?.reduce((s, t) => s + t.mrr, 0) ?? 0;
  const aiCost = tenants.data?.reduce((s, t) => s + t.aiCostMonth, 0) ?? 0;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Platform Dashboard</h1>
          <p className="page-sub">Cross-tenant health, growth and risk — last 30 days</p>
        </div>
        <div className="page-actions">
          <Button icon="download" onClick={() => navigate("/admin/reports")}>Reports</Button>
          <Button variant="primary" icon="rocket" onClick={() => navigate("/admin/onboarding")}>Onboard tenant</Button>
        </div>
      </div>

      {/* KPI row */}
      <div className="grid grid-5">
        {analytics.loading || tenants.loading ? (
          Array.from({ length: 5 }).map((_, i) => <CardSkeleton key={i} rows={1} />)
        ) : (
          <>
            <KpiCard label="Active tenants" value="47" delta={6.8} icon="building" spark={[38, 39, 39, 41, 42, 43, 44, 47]} />
            <KpiCard label="Live VoiceBots" value="128" delta={9.4} icon="bot" spark={[102, 105, 110, 112, 118, 121, 125, 128]} />
            <KpiCard label="Calls (30d)" value={fmtNum(totalCalls)} delta={12.1} icon="phone" spark={a!.callVol.slice(-14)} />
            <KpiCard label="MRR" value={`$${fmtNum(mrr)}`} delta={4.2} icon="dollar" spark={a!.revenue.slice(-14)} />
            <KpiCard label="AI cost (30d)" value={`$${fmtNum(aiCost * 4.4)}`} delta={7.9} intent="down-good" icon="cpu" spark={a!.aiCost.slice(-14)} />
          </>
        )}
      </div>

      {/* Health strip */}
      <div className="card mt-16">
        <div className="card-header">
          <span className="card-title">Platform health</span>
          <Button variant="ghost" size="sm" icon="activity" onClick={() => navigate("/admin/monitoring")}>Open monitoring</Button>
        </div>
        <div className="grid grid-4" style={{ padding: 16, gap: 12 }}>
          {health.loading && Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />)}
          {health.data?.map((m) => (
            <div key={m.name} className="card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <div className="row-between">
                <span className="t-sub t-strong">{m.name}</span>
                <Health level={m.status} label="" />
              </div>
              <div className="row-between mt-8">
                <span className="t-num" style={{ fontSize: 15, fontWeight: 650 }}>{m.value}</span>
                <Sparkline data={m.spark} width={64} height={20} color={m.status === "critical" ? "var(--viz-critical)" : m.status === "warning" ? "var(--viz-warning)" : "var(--series-2)"} />
              </div>
              <div className="t-micro mt-4">target {m.target}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="grid grid-2 mt-16">
        <ChartCard
          title="Call volume across tenants"
          sub="Daily completed calls, all channels"
          legend={<Legend shape="line" items={[{ label: "Calls", color: "var(--series-1)" }]} />}
        >
          {a ? <LineChart data={a.callsSeries} x="t" series={[{ key: "calls", label: "Calls", area: true }]} /> : <CardSkeleton rows={5} />}
        </ChartCard>
        <ChartCard
          title="Revenue vs AI cost"
          sub="Daily run-rate, USD"
          legend={<Legend shape="line" items={[{ label: "Revenue", color: "var(--series-1)" }, { label: "AI cost", color: "var(--series-3)" }]} />}
        >
          {a ? (
            <LineChart
              data={a.revVsCost}
              x="t"
              yFmt={(v) => `$${fmtNum(v)}`}
              series={[{ key: "revenue", label: "Revenue", area: true }, { key: "aiCost", label: "AI cost", color: "var(--series-3)" }]}
            />
          ) : <CardSkeleton rows={5} />}
        </ChartCard>
      </div>

      <div className="grid grid-3 mt-16">
        <ChartCard title="Top tenants by calls" sub="This month">
          {a ? <HBarList data={a.topTenantsByCalls} /> : <CardSkeleton rows={5} />}
        </ChartCard>
        <ChartCard title="Tenant plan mix" sub="47 tenants">
          {a ? <Donut data={a.planMix} centerValue="47" centerLabel="tenants" /> : <CardSkeleton rows={5} />}
        </ChartCard>
        <div className="card">
          <div className="card-header">
            <span className="card-title">Critical alerts</span>
            <Button variant="ghost" size="sm" onClick={() => navigate("/admin/monitoring")}>View all</Button>
          </div>
          <div className="col" style={{ padding: "10px 16px 16px", gap: 10 }}>
            {alerts.loading && <CardSkeleton rows={3} />}
            {alerts.data?.filter((al) => al.status !== "resolved").slice(0, 4).map((al) => (
              <button key={al.id} className="row gap-12" style={{ textAlign: "left", padding: "8px 10px", borderRadius: 10, border: "1px solid var(--hairline)" }} onClick={() => navigate("/admin/monitoring")}>
                <span className={`health-dot ${al.severity}`} style={{ marginTop: 2 }} />
                <span className="grow">
                  <span style={{ fontSize: 12.5, fontWeight: 550, lineHeight: 1.35, display: "block" }}>{al.title}</span>
                  <span className="t-micro">{al.source}</span>
                </span>
                <StatusChip status={al.status} />
              </button>
            ))}
            {alerts.data && alerts.data.filter((al) => al.status !== "resolved").length === 0 && (
              <div className="row gap-6 t-sub"><Icon name="check-circle" size={15} /> No open alerts</div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
