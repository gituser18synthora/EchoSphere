import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getTenantAnalytics, getUsageSummary, type UsageSummary } from "@/services/api";
import { CardSkeleton, ErrorState, KpiCard } from "@/components/ui";
import { ChartCard, Donut, HBarList, Legend, LineChart, fmtNum } from "@/components/charts";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { ReportExportControls } from "@/components/ReportExportControls";
import { CurrencySelect, useDisplayCurrency } from "@/components/CurrencyDisplay";
import { isCostLabel } from "@/services/money";
import type { ReportType } from "@/services/reportDownload";

const CAPABILITY_LABELS: [key: string, label: string][] = [
  ["llm", "LLM"], ["embedding", "Embeddings"], ["stt", "Speech to text"],
  ["tts", "Text to speech"], ["telephony", "Telephony"],
];

function usageQuantity(summary: UsageSummary, key: string): string {
  const u = summary.capabilities[key];
  if (!u) return "—";
  if (key === "llm") return `${(u.totalTokens || u.inputTokens + u.outputTokens).toLocaleString()} tokens`;
  if (key === "embedding") return `${u.totalTokens.toLocaleString()} tokens`;
  if (key === "stt") return `${(u.audioSeconds / 60).toFixed(1)} min`;
  if (key === "tts") return `${u.characters.toLocaleString()} chars`;
  return `${(u.audioSeconds / 60).toFixed(1)} min`;
}

export default function Analytics() {
  const [range, setRange] = useState(30);
  const [reportType, setReportType] = useState<ReportType>("usage");
  const navigate = useNavigate();
  const { user, hasPermission } = useApp();
  // Server-enforced: without costs.view the analytics response carries no
  // cost KPIs/series and /usage/summary is 403 — so it is never requested.
  const showCosts = hasPermission("costs.view");
  const a = useAsync(() => getTenantAnalytics(range), [range]);
  const usage = useAsync(
    () => (showCosts ? getUsageSummary(range) : Promise.resolve(null)),
    [range, showCosts],
  );
  const money = useDisplayCurrency(showCosts);

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
          <label className="row gap-6">
            <span className="t-micro">Report</span>
            <select
              className="select"
              aria-label="Report type"
              value={reportType}
              onChange={(event) => setReportType(event.target.value as ReportType)}
              style={{ minWidth: 112 }}
            >
              <option value="usage">Usage</option>
              {showCosts && <option value="ai_cost">AI Cost</option>}
            </select>
          </label>
          <ReportExportControls reportType={reportType} filters={{ days: range }} />
        </div>
      </div>

      <div className="grid grid-6">
        {a.loading || !a.data
          ? Array.from({ length: showCosts ? 6 : 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />)
          : a.data.kpis
              // Backend already omits these without costs.view — never render
              // a financial card even from a stale payload.
              .filter((k) => showCosts || !isCostLabel(k.label))
              .map((k) => <KpiCard key={k.label} {...k} />)}
      </div>

      {showCosts && usage.data && (
        <div className="card mt-16">
          <div className="card-header">
            <div className="col gap-2">
              <span className="card-title">AI &amp; API usage</span>
              <span className="t-micro">
                Current period · last {usage.data.period.days} days · costs stored in {usage.data.baseCurrency}
              </span>
            </div>
            <label className="row gap-6">
              <span className="t-micro">Display currency</span>
              <CurrencySelect state={money} />
            </label>
          </div>
          <div className="grid grid-6" style={{ padding: 16, gap: 10 }}>
            {CAPABILITY_LABELS.map(([key, label]) => (
              <div key={key} className="col gap-2 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <span className="t-micro">{label}</span>
                <span className="t-strong">{usageQuantity(usage.data!, key)}</span>
                <span className="t-sub" style={{ fontSize: 12 }}>
                  {money.dual(usage.data!.capabilities[key]?.costUsd ?? 0, { precise: true })}
                </span>
              </div>
            ))}
            <div className="col gap-2 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
              <span className="t-micro">Total AI/API cost</span>
              <span className="t-strong">{money.dual(usage.data.totalCostUsd, { precise: true })}</span>
              {usage.data.missingPriceEvents > 0 && (
                <span className="t-micro" style={{ color: "var(--warning, #b7791f)" }}>
                  Pricing unavailable for {usage.data.missingPriceEvents} event{usage.data.missingPriceEvents === 1 ? "" : "s"}
                </span>
              )}
            </div>
          </div>
          {usage.data.byProviderModel.length > 0 && (
            <div style={{ padding: "0 16px 16px" }}>
              <table className="table" style={{ width: "100%" }}>
                <thead>
                  <tr>
                    <th>Capability</th><th>Provider</th><th>Model</th>
                    <th style={{ textAlign: "right" }}>Requests</th>
                    <th style={{ textAlign: "right" }}>Usage</th>
                    <th style={{ textAlign: "right" }}>Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.data.byProviderModel.map((row) => (
                    <tr key={`${row.capability}:${row.provider}:${row.model}`}>
                      <td><span className="tag">{row.capability.toUpperCase()}</span></td>
                      <td>{row.provider}</td>
                      <td className="t-micro">{row.model || "—"}</td>
                      <td style={{ textAlign: "right" }} className="t-num">{row.requests.toLocaleString()}</td>
                      <td style={{ textAlign: "right" }} className="t-micro">
                        {row.totalTokens > 0 ? `${row.totalTokens.toLocaleString()} tokens`
                          : row.characters > 0 ? `${row.characters.toLocaleString()} chars`
                          : row.audioSeconds > 0 ? `${(row.audioSeconds / 60).toFixed(1)} min` : "—"}
                      </td>
                      <td style={{ textAlign: "right" }} className="t-num">
                        {row.missingPriceEvents > 0 && row.costUsd === 0
                          ? <span className="t-micro" title="No provider price configured">Pricing unavailable</span>
                          : money.dual(row.costUsd, { precise: true })}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

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

          <div className={showCosts ? "grid grid-2 mt-16" : "grid mt-16"}>
            <ChartCard title="Top intents" sub="With period-over-period trend">
              <HBarList data={a.data.topIntents.map((t) => ({ label: t.label, value: t.value }))} trend={a.data.topIntents.map((t) => t.trend)} />
            </ChartCard>
            {showCosts && (
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
            )}
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
