import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { getPlatformAnalytics, getPlatformUsage, listTenants } from "@/services/api";
import { CardSkeleton, ErrorState } from "@/components/ui";
import { BarChart, ChartCard, HBarList, Legend, fmtNum } from "@/components/charts";
import { DataTable } from "@/components/DataTable";
import { CurrencySelect, useDisplayCurrency } from "@/components/CurrencyDisplay";

export default function UsageReport() {
  const [range, setRange] = useState(30);
  const a = useAsync(() => getPlatformAnalytics(30), []);
  const tenantsQ = useAsync(listTenants, []);
  const usage = useAsync(() => getPlatformUsage(range), [range]);
  const money = useDisplayCurrency();

  if (a.error) return <ErrorState message={a.error} onRetry={a.reload} />;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Usage</h1>
          <p className="page-sub">Consumption across tenants — calls, minutes and AI spend</p>
        </div>
        <div className="page-actions">
          <div className="segmented" role="group" aria-label="Date range">
            {[7, 30, 90].map((d) => <button key={d} aria-pressed={range === d} onClick={() => setRange(d)}>{d}d</button>)}
          </div>
          <label className="row gap-6">
            <span className="t-micro">Display currency</span>
            <CurrencySelect state={money} />
          </label>
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
        <div className="card-header">
          <div className="col gap-2">
            <span className="card-title">Metered AI/API usage by tenant</span>
            <span className="t-micro">
              Last {range} days · total {usage.data ? money.dual(usage.data.totalCostUsd, { precise: true }) : "…"}
              {usage.data && usage.data.missingPriceEvents > 0
                ? ` · pricing unavailable for ${usage.data.missingPriceEvents} events`
                : ""}
            </span>
          </div>
        </div>
        <DataTable
          loading={usage.loading} error={usage.error} onRetry={usage.reload}
          rows={usage.data?.byTenant ?? []}
          empty={{ icon: "chart", title: "No metered usage yet" }}
          columns={[
            { key: "tenant", header: "Tenant", sortValue: (r) => r.tenant, render: (r) => <span className="t-strong">{r.tenant}</span> },
            { key: "requests", header: "Requests", align: "right", sortValue: (r) => r.requests, render: (r) => <span className="t-num">{fmtNum(r.requests)}</span> },
            { key: "tokens", header: "Tokens", align: "right", sortValue: (r) => r.totalTokens, render: (r) => <span className="t-num">{fmtNum(r.totalTokens)}</span> },
            { key: "characters", header: "TTS chars", align: "right", sortValue: (r) => r.characters, render: (r) => <span className="t-num">{fmtNum(r.characters)}</span> },
            { key: "audio", header: "Audio min", align: "right", sortValue: (r) => r.audioSeconds, render: (r) => <span className="t-num">{(r.audioSeconds / 60).toFixed(1)}</span> },
            { key: "cost", header: "Cost", align: "right", sortValue: (r) => r.costUsd, render: (r) => <span className="t-num">{money.dual(r.costUsd, { precise: true })}</span> },
          ]}
        />
      </div>

      <div className="card mt-16">
        <div className="card-header">
          <span className="card-title">Usage by provider &amp; model</span>
        </div>
        <DataTable
          loading={usage.loading} error={usage.error} onRetry={usage.reload}
          rows={usage.data?.byProviderModel ?? []}
          empty={{ icon: "chart", title: "No metered usage yet" }}
          columns={[
            { key: "capability", header: "Capability", render: (r) => <span className="tag">{r.capability.toUpperCase()}</span> },
            { key: "provider", header: "Provider", sortValue: (r) => r.provider, render: (r) => <span className="t-strong">{r.provider}</span> },
            { key: "model", header: "Model", render: (r) => <span className="t-micro">{r.model || "—"}</span> },
            { key: "requests", header: "Requests", align: "right", sortValue: (r) => r.requests, render: (r) => <span className="t-num">{fmtNum(r.requests)}</span> },
            {
              key: "cost", header: "Cost", align: "right", sortValue: (r) => r.costUsd,
              render: (r) => r.missingPriceEvents > 0 && r.costUsd === 0
                ? <span className="t-micro" title="No provider price configured">Pricing unavailable</span>
                : <span className="t-num">{money.dual(r.costUsd, { precise: true })}</span>,
            },
          ]}
        />
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
