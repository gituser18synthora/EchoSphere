import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listChannels } from "@/services/api";
import { CardSkeleton, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import type { ChannelType } from "@/types/domain";

const meta: Record<ChannelType, { icon: IconName; name: string }> = {
  voice: { icon: "phone", name: "Voice" },
  whatsapp: { icon: "whatsapp", name: "WhatsApp" },
  web: { icon: "monitor", name: "Web widget" },
  mobile: { icon: "smartphone", name: "Mobile SDK" },
};

export default function Channels() {
  const botsQ = useAsync(listBots, []);
  const chQ = useAsync(() => listChannels("bot-101"), []);
  const navigate = useNavigate();

  if (botsQ.loading || chQ.loading) {
    return (
      <>
        <PageHead />
        <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>
      </>
    );
  }

  const bots = (botsQ.data ?? []).filter((b) => b.status !== "archived");

  return (
    <>
      <PageHead />
      <div className="card">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Bot</th>
                {(Object.keys(meta) as ChannelType[]).map((c) => (
                  <th key={c}><span className="row gap-4" style={{ display: "inline-flex" }}><Icon name={meta[c].icon} size={13} />{meta[c].name}</span></th>
                ))}
              </tr>
            </thead>
            <tbody>
              {bots.map((b) => (
                <tr key={b.id} className="row-click" onClick={() => navigate(`/t/bots/${b.id}/channels`)}>
                  <td><div className="t-strong">{b.name}</div><div className="t-micro">{b.status === "published" ? `live ${b.liveVersion}` : b.status.replace("_", " ")}</div></td>
                  {(Object.keys(meta) as ChannelType[]).map((c) => {
                    const cfg = b.id === "bot-101"
                      ? chQ.data?.find((x) => x.type === c)
                      : b.channels.includes(c)
                        ? { status: "live" as const }
                        : undefined;
                    return (
                      <td key={c}>
                        {cfg ? <StatusChip status={cfg.status} /> : <span className="t-micro">—</span>}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <p className="t-micro mt-12">Click a row to configure that bot's channels in Studio. A failed channel never receives traffic — calls fall back to the voice line where configured.</p>
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
