import type { VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { getTenantAnalytics } from "@/services/api";
import { CardSkeleton, EmptyState, ErrorState, KpiCard } from "@/components/ui";
import { ChartCard, HBarList, Legend, LineChart, fmtNum } from "@/components/charts";

export default function AnalyticsTab({ bot }: { bot: VoiceBot }) {
  const a = useAsync(() => getTenantAnalytics(30, bot.id), [bot.id]);

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
        {a.data.kpis.slice(0, 4).map((k, i) => (
          <KpiCard
            key={k.label}
            label={k.label}
            value={k.value}
            delta={k.delta}
            intent={k.intent}
            icon={["phone", "check-circle", "alert", "star"][i] ?? "activity"}
          />
        ))}
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
