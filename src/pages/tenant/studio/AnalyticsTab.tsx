import type { VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { getTenantAnalytics } from "@/services/api";
import { CardSkeleton, EmptyState, ErrorState, KpiCard } from "@/components/ui";
import { ChartCard, HBarList, Legend, LineChart, fmtNum } from "@/components/charts";

export default function AnalyticsTab({ bot }: { bot: VoiceBot }) {
  const a = useAsync(() => getTenantAnalytics(30), [bot.id]);

  if (bot.status === "draft" || bot.status === "in_review") {
    return (
      <EmptyState
        icon="trend"
        title="No analytics yet"
        body="This bot hasn't taken live calls. Analytics appear within minutes of the first published call."
      />
    );
  }
  if (a.error) return <ErrorState message={a.error} onRetry={a.reload} />;
  if (a.loading || !a.data) return <div className="grid grid-2"><CardSkeleton rows={6} /><CardSkeleton rows={6} /></div>;

  return (
    <div className="col gap-16">
      <div className="grid grid-4">
        <KpiCard label="Calls this month" value={fmtNum(bot.callsMonth)} delta={9.1} icon="phone" />
        <KpiCard label="Containment" value={`${bot.containment}%`} delta={2.4} icon="check-circle" />
        <KpiCard label="CSAT" value={`${bot.csat.toFixed(1)} / 5`} delta={1.2} icon="star" />
        <KpiCard label="Cost / call" value={`$${bot.avgCostPerCall.toFixed(2)}`} delta={-3.8} intent="down-good" icon="dollar" />
      </div>
      <div className="grid grid-2">
        <ChartCard
          title="Calls & containment"
          sub="Daily, last 30 days"
          legend={<Legend shape="line" items={[{ label: "Calls", color: "var(--series-1)" }, { label: "Contained", color: "var(--series-2)" }]} />}
        >
          <LineChart data={a.data.callsSeries} x="t" height={210}
            series={[{ key: "calls", label: "Calls", area: true }, { key: "contained", label: "Contained", color: "var(--series-2)" }]} />
        </ChartCard>
        <ChartCard title="Top intents" sub="Detected this month, with 30-day trend">
          <HBarList data={a.data.topIntents.map((t) => ({ label: t.label, value: t.value }))} trend={a.data.topIntents.map((t) => t.trend)} />
        </ChartCard>
      </div>
      <div className="grid grid-2">
        <ChartCard
          title="Cost breakdown"
          sub="Daily USD by component"
          legend={<Legend items={[
            { label: "LLM", color: "var(--series-1)" }, { label: "TTS", color: "var(--series-2)" },
            { label: "STT", color: "var(--series-3)" }, { label: "Telephony", color: "var(--series-4)" },
          ]} />}
        >
          <LineChart data={a.data.costSeries} x="t" height={210} yFmt={(v) => `$${fmtNum(v)}`}
            series={[
              { key: "llm", label: "LLM" }, { key: "tts", label: "TTS" },
              { key: "stt", label: "STT" }, { key: "telephony", label: "Telephony" },
            ]} />
        </ChartCard>
        <ChartCard title="Knowledge usage" sub="Retrieval hits by source, 30 days">
          <HBarList data={a.data.knowledgeUsage} color="var(--series-2)" />
        </ChartCard>
      </div>
    </div>
  );
}
