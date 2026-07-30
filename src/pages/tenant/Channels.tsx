import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots } from "@/services/api";
import { Button, CardSkeleton, EmptyState, ErrorState } from "@/components/ui";
import ChannelsTab from "@/pages/tenant/studio/ChannelsTab";

export default function Channels() {
  const botsQ = useAsync(listBots, []);
  const [botId, setBotId] = useState("");
  const navigate = useNavigate();

  const bots = (botsQ.data ?? []).filter((b) => b.status !== "archived");
  const selectedId = botId || bots[0]?.id || "";
  const bot = bots.find((b) => b.id === selectedId);

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
          <div className="filter-bar row-between">
            <select className="select" value={selectedId} onChange={(e) => setBotId(e.target.value)} aria-label="Select bot">
              {bots.map((b) => <option key={b.id} value={b.id}>{b.name}</option>)}
            </select>
            {bot && (
              <Button variant="ghost" icon="external" onClick={() => navigate(`/t/bots/${bot.id}/channels`)}>
                Open in Studio
              </Button>
            )}
          </div>
          {bot && <ChannelsTab bot={bot} />}
          <p className="t-micro mt-12">
            Configure each channel's provider, credentials (as environment references) and routing here,
            then run a connection test. A deactivated or failed channel never receives live traffic.
          </p>
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
        <p className="page-sub">Configure and deploy every bot across voice, WhatsApp, web, mobile and SMS</p>
      </div>
    </div>
  );
}
