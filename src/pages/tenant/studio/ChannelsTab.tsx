import type { ChannelType, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import { listChannels, simulateAction } from "@/services/api";
import { Button, CardSkeleton, ErrorState, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const channelMeta: Record<ChannelType, { icon: IconName; name: string; desc: string }> = {
  voice: { icon: "phone", name: "Voice", desc: "Inbound PSTN / SIP calls" },
  whatsapp: { icon: "whatsapp", name: "WhatsApp", desc: "Business messaging" },
  web: { icon: "monitor", name: "Web widget", desc: "Chat + voice on your site" },
  mobile: { icon: "smartphone", name: "Mobile SDK", desc: "In-app voice assistant" },
};

export default function ChannelsTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listChannels(bot.id), [bot.id]);
  const { toast } = useApp();

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading) return <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}</div>;

  const all: ChannelType[] = ["voice", "whatsapp", "web", "mobile"];
  const configs = all.map((t) => q.data?.find((c) => c.type === t) ?? {
    type: t, botId: bot.id, status: "not_configured" as const, detail: "Not configured", workflow: "—",
  });

  const test = async (name: string) => {
    await simulateAction("channel-test");
    toast(`${name} test started — results appear here in ~30s`);
    q.reload();
  };

  return (
    <div className="grid grid-2">
      {configs.map((c) => {
        const meta = channelMeta[c.type];
        return (
          <div key={c.type} className="card card-pad col gap-12">
            <div className="row-between">
              <div className="row gap-12">
                <span className={`icon-tile ${c.status === "live" ? "good" : c.status === "failed" ? "critical" : c.status === "testing" ? "warning" : "neutral"}`}>
                  <Icon name={meta.icon} size={17} />
                </span>
                <div>
                  <span className="t-strong" style={{ fontSize: 14 }}>{meta.name}</span>
                  <div className="t-micro">{meta.desc}</div>
                </div>
              </div>
              <StatusChip status={c.status} />
            </div>

            <div className="col gap-6" style={{ fontSize: 12.5 }}>
              <div className="row-between" style={{ padding: "6px 0", borderBottom: "1px solid var(--hairline)" }}>
                <span className="t-sub">Endpoint</span>
                <code style={{ fontSize: 12 }}>{c.detail}</code>
              </div>
              <div className="row-between" style={{ padding: "6px 0", borderBottom: "1px solid var(--hairline)" }}>
                <span className="t-sub">Routes to</span>
                <span className="t-strong">{c.workflow}</span>
              </div>
              <div className="row-between" style={{ padding: "6px 0" }}>
                <span className="t-sub">Last test</span>
                {c.lastTest ? (
                  <span className={`row gap-4 ${c.lastTest.ok ? "t-good" : "t-bad"}`} style={{ fontWeight: 600, fontSize: 12 }}>
                    <Icon name={c.lastTest.ok ? "check-circle" : "x-circle"} size={13} />
                    {new Date(c.lastTest.at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                  </span>
                ) : <span className="t-micro">never</span>}
              </div>
            </div>

            {c.lastTest && !c.lastTest.ok && (
              <div className="callout callout-critical" style={{ padding: "9px 11px", fontSize: 12 }}>
                <Icon name="x-circle" size={13} />
                <div className="callout-body">{c.lastTest.message}</div>
              </div>
            )}

            <div className="row gap-6">
              {c.status === "not_configured" ? (
                <Button variant="primary" size="sm" icon="plus" onClick={() => toast(`${meta.name} setup started — follow the connection guide`, "info")}>Configure</Button>
              ) : (
                <>
                  <Button size="sm" icon="play" onClick={() => test(meta.name)}>Run test</Button>
                  <Button size="sm" variant="ghost" icon="settings" onClick={() => toast(`${meta.name} configuration opened`, "info")}>Settings</Button>
                  {c.status === "failed" || (c.lastTest && !c.lastTest.ok) ? (
                    <Button size="sm" variant="danger-ghost" icon="refresh" onClick={() => test(meta.name)}>Retry</Button>
                  ) : null}
                </>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
