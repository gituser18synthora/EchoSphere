import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { getWorkflow, listBots } from "@/services/api";
import { CardSkeleton, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";

export default function TenantWorkflows() {
  const botsQ = useAsync(listBots, []);
  const wfQ = useAsync(() => getWorkflow("bot-101"), []);
  const navigate = useNavigate();

  const entries = (botsQ.data ?? []).filter((b) => b.status !== "archived").map((b, i) => ({
    bot: b,
    wf: {
      name: i === 0 ? "Booking journey" : `${b.useCase} journey`,
      version: i === 0 ? wfQ.data?.version ?? 12 : 3 + i,
      nodes: i === 0 ? wfQ.data?.nodes.length ?? 10 : 6 + i,
      issues: i === 0 ? wfQ.data?.issues.length ?? 2 : 0,
      status: b.status === "draft" ? "draft" : "approved",
    },
  }));

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Workflows</h1>
          <p className="page-sub">Conversation journeys per bot — open one to edit on the canvas</p>
        </div>
      </div>

      {botsQ.loading && <div className="grid grid-3">{Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>}

      <div className="grid grid-3">
        {entries.map(({ bot, wf }) => (
          <button key={bot.id} className="card card-pad card-clickable col gap-12" style={{ textAlign: "left" }}
            onClick={() => navigate(`/t/bots/${bot.id}/workflows`)}>
            <div className="row gap-12">
              <span className="icon-tile brand"><Icon name="workflow" size={16} /></span>
              <div className="grow">
                <div className="t-strong" style={{ fontSize: 13.5 }}>{wf.name}</div>
                <div className="t-micro">{bot.name}</div>
              </div>
            </div>
            <div className="row gap-6 wrap">
              <StatusChip status={wf.status} />
              <span className="tag t-num">v{wf.version}</span>
              <span className="tag t-num">{wf.nodes} nodes</span>
              {wf.issues > 0 && <span className="chip chip-warning"><Icon name="alert" size={11} />{wf.issues} warnings</span>}
            </div>
            <span className="t-micro row gap-4">Open in Studio <Icon name="arrow-right" size={12} /></span>
          </button>
        ))}
      </div>
    </>
  );
}
