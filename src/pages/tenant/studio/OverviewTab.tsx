import { useNavigate } from "react-router-dom";
import type { VoiceBot } from "@/types/domain";
import { Icon } from "@/components/Icon";
import { KpiCard, Progress, Timeline } from "@/components/ui";
import { fmtNum } from "@/components/charts";

export default function OverviewTab({ bot }: { bot: VoiceBot }) {
  const navigate = useNavigate();
  const done = bot.readiness.filter((r) => r.done).length;
  const pct = (done / bot.readiness.length) * 100;

  return (
    <div className="grid" style={{ gridTemplateColumns: "1.5fr 1fr", gap: 20 }}>
      <div className="col gap-16">
        <div className="card card-pad col gap-8">
          <span className="t-label">Business goal</span>
          <p className="t-body" style={{ fontSize: 14 }}>{bot.description}</p>
          <div className="row gap-16 mt-8 wrap">
            <Meta icon="user" label="Owner" value={bot.owner} />
            <Meta icon="globe" label="Languages" value={bot.languages.join(", ")} />
            <Meta icon="target" label="Use case" value={bot.useCase} />
            <Meta icon="clock" label="Updated" value={new Date(bot.updatedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })} />
          </div>
        </div>

        {bot.status === "published" && (
          <div className="grid grid-4" style={{ gap: 12 }}>
            <KpiCard label="Calls today" value={fmtNum(bot.callsToday)} icon="phone" />
            <KpiCard label="Containment" value={`${bot.containment}%`} icon="check-circle" />
            <KpiCard label="CSAT" value={`${bot.csat.toFixed(1)} / 5`} icon="star" />
            <KpiCard label="Cost / call" value={`$${bot.avgCostPerCall.toFixed(2)}`} icon="dollar" />
          </div>
        )}

        <div className="card">
          <div className="card-header"><span className="card-title">Recent activity</span></div>
          <div style={{ padding: "16px 20px" }}>
            <Timeline
              items={[
                { icon: "edit", tone: "brand", title: <>Escalation prompt edited — v6 <b>pending approval</b></>, meta: "Dana Okafor · Jul 2, 4:45 PM" },
                { icon: "refresh", tone: "warning", title: <>Knowledge source “Insurance Providers Page” flagged <b>stale</b></>, meta: "System · Jul 1, 6:00 AM" },
                { icon: "rocket", tone: "good", title: <>Version {bot.liveVersion ?? bot.version} published</>, meta: `${bot.owner} · ${bot.publishedAt ? new Date(bot.publishedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" }) : "—"}` },
                { icon: "check-circle", tone: "good", title: "Regression suite passed 8/8 scenarios", meta: "Scheduled run · Jun 24, 7:00 AM" },
              ]}
            />
          </div>
        </div>
      </div>

      <div className="col gap-16">
        <div className="card">
          <div className="card-header">
            <span className="card-title">Readiness checklist</span>
            <span className="t-micro t-num">{done}/{bot.readiness.length}</span>
          </div>
          <div className="col" style={{ padding: 16, gap: 10 }}>
            <Progress value={pct} tone={pct === 100 ? "good" : pct > 50 ? undefined : "warning"} />
            <div className="col gap-4 mt-8">
              {bot.readiness.map((r) => (
                <button
                  key={r.id}
                  className="row-between"
                  style={{ padding: "7px 8px", borderRadius: 8, textAlign: "left" }}
                  onClick={() => navigate(`/t/bots/${bot.id}/${r.studioTab}`)}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-3)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                >
                  <span className="row gap-8" style={{ fontSize: 13 }}>
                    {r.done
                      ? <Icon name="check-circle" size={15} style={{ color: "var(--status-good)" }} />
                      : <Icon name="clock" size={15} style={{ color: "var(--viz-warning)" }} />}
                    <span style={{ color: r.done ? "var(--ink-2)" : "var(--ink)", fontWeight: r.done ? 450 : 600 }}>{r.label}</span>
                  </span>
                  <Icon name="chevron-right" size={13} style={{ color: "var(--ink-3)" }} />
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="card card-pad col gap-10">
          <span className="card-title">Suggested next steps</span>
          {[
            { t: "Fix the 2 failing regression scenarios", tab: "testing", icon: "play" as const },
            { t: "Approve the pending escalation prompt", tab: "prompts", icon: "edit" as const },
            { t: "Re-sync the stale insurance source", tab: "knowledge", icon: "refresh" as const },
          ].map((s) => (
            <button key={s.t} className="row gap-10 card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10, textAlign: "left" }}
              onClick={() => navigate(`/t/bots/${bot.id}/${s.tab}`)}>
              <Icon name={s.icon} size={14} style={{ color: "var(--brand-500)" }} />
              <span style={{ fontSize: 12.5, fontWeight: 550 }}>{s.t}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function Meta({ icon, label, value }: { icon: Parameters<typeof Icon>[0]["name"]; label: string; value: string }) {
  return (
    <span className="col" style={{ gap: 2 }}>
      <span className="t-micro row gap-4"><Icon name={icon} size={12} />{label}</span>
      <span className="t-strong" style={{ fontSize: 13 }}>{value}</span>
    </span>
  );
}
