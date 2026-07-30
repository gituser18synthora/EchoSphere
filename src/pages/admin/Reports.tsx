import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { getPlatformAnalytics } from "@/services/api";
import { CardSkeleton, ErrorState, Tabs } from "@/components/ui";
import { BarChart, ChartCard, Donut, HBarList, Legend, LineChart, fmtNum } from "@/components/charts";
import { ReportExportControls } from "@/components/ReportExportControls";
import type { ReportType } from "@/services/reportDownload";

const tabs: { id: ReportType; label: string }[] = [
  { id: "usage", label: "Usage" },
  { id: "revenue", label: "Revenue" },
  { id: "ai_cost", label: "AI Cost" },
];

export default function Reports() {
  const [tab, setTab] = useState<ReportType>("usage");
  const [range, setRange] = useState(30);
  const a = useAsync(() => getPlatformAnalytics(range), [range]);

  if (a.error) return <ErrorState message={a.error} onRetry={a.reload} />;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Reports</h1>
          <p className="page-sub">Exportable platform reporting</p>
        </div>
        <div className="page-actions">
          <div className="segmented" role="group" aria-label="Date range">
            {[7, 30, 90].map((d) => (
              <button key={d} aria-pressed={range === d} onClick={() => setRange(d)}>{d}d</button>
            ))}
          </div>
          <ReportExportControls reportType={tab} filters={{ days: range }} />
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={(value) => setTab(value as ReportType)} />
      <div className="mt-16">
        {!a.data ? <div className="grid grid-2"><CardSkeleton rows={6} /><CardSkeleton rows={6} /></div> : (
          <>
            {tab === "usage" && (
              <div className="grid grid-2">
                <ChartCard title="Daily calls" sub={`Last ${range} days`} legend={<Legend items={[{ label: "Calls", color: "var(--series-1)" }]} />}>
                  <BarChart data={a.data.callsSeries} x="t" series={[{ key: "calls", label: "Calls" }]} maxTicks={8} />
                </ChartCard>
                <ChartCard title="Top tenants by call volume" sub="This month">
                  <HBarList data={a.data.topTenantsByCalls} />
                </ChartCard>
              </div>
            )}
            {tab === "revenue" && (
              <div className="grid grid-2">
                <ChartCard title="Revenue run-rate" sub={`Daily USD · last ${range} days`} legend={<Legend shape="line" items={[{ label: "Revenue", color: "var(--series-1)" }]} />}>
                  <LineChart data={a.data.revVsCost} x="t" yFmt={(v) => `$${fmtNum(v)}`} series={[{ key: "revenue", label: "Revenue", area: true }]} />
                </ChartCard>
                <ChartCard title="MRR by plan" sub="Share of monthly recurring revenue">
                  <Donut
                    data={a.data.mrrByPlan}
                    centerValue={`$${fmtNum(a.data.mrrByPlan.reduce((sum, plan) => sum + plan.value, 0))}`}
                    centerLabel="MRR"
                  />
                </ChartCard>
              </div>
            )}
            {tab === "ai_cost" && (
              <div className="grid grid-2">
                <ChartCard title="AI cost run-rate" sub={`Daily USD · last ${range} days`} legend={<Legend shape="line" items={[{ label: "AI cost", color: "var(--series-3)" }]} />}>
                  <LineChart data={a.data.revVsCost} x="t" yFmt={(v) => `$${fmtNum(v)}`} series={[{ key: "aiCost", label: "AI cost", color: "var(--series-3)", area: true }]} />
                </ChartCard>
                <ChartCard title="AI cost by provider" sub="This month, USD">
                  <HBarList data={a.data.aiCostByProvider} valueFmt={(v) => `$${fmtNum(v)}`} color="var(--series-3)" />
                </ChartCard>
              </div>
            )}
          </>
        )}
      </div>
    </>
  );
}
