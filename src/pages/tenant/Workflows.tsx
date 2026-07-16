import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listBots, listWorkflows } from "@/services/api";
import { CardSkeleton, EmptyState, ErrorState, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";

export default function TenantWorkflows() {
  const botsQ = useAsync(listBots, []);
  const wfQ = useAsync(listWorkflows, []);
  const navigate = useNavigate();

  const loading = botsQ.loading || wfQ.loading;
  const workflows = wfQ.data ?? [];

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Workflows</h1>
          <p className="page-sub">Conversation journeys per bot — open one to edit on the canvas</p>
        </div>
      </div>

      {wfQ.error && <ErrorState message={wfQ.error} onRetry={wfQ.reload} />}

      {!wfQ.error && loading && <div className="grid grid-3">{Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} rows={4} />)}</div>}

      {!wfQ.error && !loading && workflows.length === 0 && (
        <div className="card">
          <EmptyState icon="workflow" title="No workflows yet" body="Create a bot to start designing its conversation journey on the canvas." />
        </div>
      )}

      {!wfQ.error && !loading && workflows.length > 0 && (
        <div className="grid grid-3">
          {workflows.map((wf) => {
            const bot = botsQ.data?.find((b) => b.id === wf.botId);
            return (
              <button key={wf.id} className="card card-pad card-clickable col gap-12" style={{ textAlign: "left" }}
                onClick={() => navigate(`/t/bots/${wf.botId}/workflows`)}>
                <div className="row gap-12">
                  <span className="icon-tile brand"><Icon name="workflow" size={16} /></span>
                  <div className="grow">
                    <div className="t-strong" style={{ fontSize: 13.5 }}>{wf.name}</div>
                    <div className="t-micro">{bot?.name ?? wf.botId}</div>
                  </div>
                </div>
                <div className="row gap-6 wrap">
                  <StatusChip status={wf.status} />
                  <span className="tag t-num">v{wf.version}</span>
                  <span className="tag t-num">{wf.nodes.length} nodes</span>
                  {wf.issues.length > 0 && <span className="chip chip-warning"><Icon name="alert" size={11} />{wf.issues.length} warnings</span>}
                </div>
                <span className="t-micro row gap-4">Open in Studio <Icon name="arrow-right" size={12} /></span>
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}
