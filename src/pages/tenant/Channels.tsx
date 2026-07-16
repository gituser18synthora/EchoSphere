import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listChannels } from "@/services/api";
import { CardSkeleton, EmptyState, ErrorState, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import type { ChannelConfig, ChannelType } from "@/types/domain";

const meta: Record<ChannelType, { icon: IconName; name: string }> = {
  voice: { icon: "phone", name: "Voice" },
  whatsapp: { icon: "whatsapp", name: "WhatsApp" },
  web: { icon: "monitor", name: "Web widget" },
  mobile: { icon: "smartphone", name: "Mobile SDK" },
};

export default function Channels() {
  const botsQ = useAsync(listBots, []);
  const [botId, setBotId] = useState("");
  const navigate = useNavigate();

  const bots = (botsQ.data ?? []).filter((b) => b.status !== "archived");
  const selectedId = botId || bots[0]?.id || "";
  const chQ = useAsync(() => (selectedId ? listChannels(selectedId) : Promise.resolve([] as ChannelConfig[])), [selectedId]);

  if (botsQ.loading) {
    return (
      <>
        <PageHead />
        <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>
      </>
    );
  }

  if (botsQ.error) {
    return (
      <>
        <PageHead />
        <ErrorState message={botsQ.error} onRetry={botsQ.reload} />
      </>
    );
  }

  return (
    <>
      <PageHead />
      {bots.length === 0 ? (
        <div className="card">
          <EmptyState icon="phone" title="No bots to deploy" body="Create a bot first — its channels appear here once configured." />
        </div>
      ) : (
        <>
          <div className="filter-bar">
            <select className="select" value={selectedId} onChange={(e) => setBotId(e.target.value)} aria-label="Select bot">
              {bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
          </div>
          {chQ.loading && <CardSkeleton rows={4} />}
          {chQ.error && <ErrorState message={chQ.error} onRetry={chQ.reload} />}
          {!chQ.loading && !chQ.error && ((chQ.data ?? []).length === 0 ? (
            <div className="card">
              <EmptyState icon="plug" title="No channels configured" body="Open this bot in Studio to configure voice, WhatsApp, web or mobile." />
            </div>
          ) : (
            <div className="card">
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr><th>Channel</th><th>Status</th><th>Endpoint</th><th>Workflow</th><th>Last test</th></tr>
                  </thead>
                  <tbody>
                    {(chQ.data ?? []).map((c) => (
                      <tr key={c.type} className="row-click" onClick={() => navigate(`/t/bots/${selectedId}/channels`)}>
                        <td><span className="row gap-8"><Icon name={meta[c.type].icon} size={14} /><span className="t-strong">{meta[c.type].name}</span></span></td>
                        <td><StatusChip status={c.status} /></td>
                        <td className="t-sub">{c.detail || "—"}</td>
                        <td className="t-sub">{c.workflow || "—"}</td>
                        <td className="t-sub">{c.lastTest ? `${new Date(c.lastTest.at).toLocaleDateString("en-US", { month: "short", day: "numeric" })} · ${c.lastTest.ok ? "passed" : "failed"}` : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
          <p className="t-micro mt-12">Click a row to configure this bot's channels in Studio. A failed channel never receives traffic — calls fall back to the voice line where configured.</p>
        </>
      )}
    </>
  );
}

function PageHead() {
  return (
    <div className="page-head">
      <div className="page-head-titles">
        <h1 className="page-title">Channels</h1>
        <p className="page-sub">Deployment status of every bot across voice, WhatsApp, web and mobile</p>
      </div>
    </div>
  );
}
