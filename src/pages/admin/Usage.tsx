import { useAsync } from "@/hooks/useAsync";
import { getPlatformAnalytics, listTenants } from "@/services/api";
import { CardSkeleton, ErrorState } from "@/components/ui";
import { BarChart, ChartCard, HBarList, Legend, fmtNum } from "@/components/charts";
import { DataTable } from "@/components/DataTable";

export default function UsageReport() {
  const a = useAsync(() => getPlatformAnalytics(30), []);
  const tenantsQ = useAsync(listTenants, []);

  if (a.error) return <ErrorState message={a.error} onRetry={a.reload} />;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Usage</h1>
          <p className="page-sub">Consumption across tenants — calls, minutes and AI spend</p>
        </div>
      </div>

      <div className="grid grid-2">
        <ChartCard title="Daily platform calls" sub="Last 30 days" legend={<Legend items={[{ label: "Calls", color: "var(--series-1)" }]} />}>
          {a.data ? <BarChart data={a.data.callsSeries} x="t" series={[{ key: "calls", label: "Calls" }]} maxTicks={8} /> : <CardSkeleton rows={5} />}
        </ChartCard>
        <ChartCard title="AI cost by provider" sub="This month, USD">
          {a.data ? <HBarList data={a.data.aiCostByProvider} valueFmt={(v) => `$${fmtNum(v)}`} color="var(--series-3)" /> : <CardSkeleton rows={5} />}
        </ChartCard>
      </div>

      <div className="card mt-16">
        <div className="card-header"><span className="card-title">Per-tenant consumption</span></div>
        <DataTable
          loading={tenantsQ.loading} error={tenantsQ.error} onRetry={tenantsQ.reload}
          rows={(tenantsQ.data ?? []).filter((t) => t.status !== "provisioning")}
          empty={{ icon: "chart", title: "No usage data" }}
          columns={[
            { key: "name", header: "Tenant", sortValue: (t) => t.name, render: (t) => <span className="t-strong">{t.name}</span> },
            { key: "calls", header: "Calls / mo", align: "right", sortValue: (t) => t.callsMonth, render: (t) => <span className="t-num">{fmtNum(t.callsMonth)}</span> },
            { key: "minutes", header: "Minutes / mo", align: "right", sortValue: (t) => t.minutesMonth, render: (t) => <span className="t-num">{fmtNum(t.minutesMonth)}</span> },
            { key: "bots", header: "Bots", align: "right", sortValue: (t) => t.bots, render: (t) => <span className="t-num">{t.bots}</span> },
            { key: "aiCost", header: "AI cost", align: "right", sortValue: (t) => t.aiCostMonth, render: (t) => <span className="t-num">${fmtNum(t.aiCostMonth)}</span> },
            {
              key: "cpc", header: "AI cost / call", align: "right",
              sortValue: (t) => (t.callsMonth ? t.aiCostMonth / t.callsMonth : 0),
              render: (t) => <span className="t-num">{t.callsMonth ? `$${(t.aiCostMonth / t.callsMonth).toFixed(3)}` : "—"}</span>,
            },
          ]}
        />
      </div>
    </>
  );
}
