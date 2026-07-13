import { useAsync } from "@/hooks/useAsync";
import { listIntegrations, simulateAction } from "@/services/api";
import { Button, CardSkeleton, StatusChip } from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const catIcon: Record<string, IconName> = {
  Healthcare: "activity", Support: "headphones", CRM: "database",
  Notifications: "bell", "Contact Center": "phone",
};

export default function Integrations() {
  const q = useAsync(listIntegrations, []);
  const { toast } = useApp();

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Integrations</h1>
          <p className="page-sub">Connected systems your bots read from and write to</p>
        </div>
      </div>

      {q.loading && <div className="grid grid-3">{Array.from({ length: 6 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}</div>}

      <div className="grid grid-3">
        {q.data?.map((ig) => (
          <div key={ig.id} className="card card-pad col gap-12">
            <div className="row-between">
              <span className={`icon-tile ${ig.status === "connected" ? "good" : ig.status === "error" ? "critical" : "neutral"}`}>
                <Icon name={catIcon[ig.category] ?? "plug"} size={16} />
              </span>
              <StatusChip status={ig.status} />
            </div>
            <div>
              <div className="t-strong" style={{ fontSize: 14 }}>{ig.name}</div>
              <div className="t-micro">{ig.category}</div>
            </div>
            <p className="t-sub" style={{ fontSize: 12.5, minHeight: 38 }}>{ig.description}</p>
            {ig.status === "error" && (
              <div className="callout callout-critical" style={{ padding: "8px 10px", fontSize: 12 }}>
                <Icon name="x-circle" size={13} />
                <div className="callout-body">OAuth token expired Jun 30. Reconnect to resume case sync.</div>
              </div>
            )}
            <div className="row gap-6">
              {ig.status === "connected" && (
                <>
                  <Button size="sm" variant="ghost" icon="settings" onClick={() => toast(`${ig.name} settings opened`, "info")}>Configure</Button>
                  <Button size="sm" variant="danger-ghost" onClick={async () => { await simulateAction("disconnect"); toast(`${ig.name} disconnected — dependent bots flagged in readiness checks`); }}>Disconnect</Button>
                </>
              )}
              {ig.status === "error" && (
                <Button size="sm" variant="primary" icon="refresh" onClick={async () => { await simulateAction("reconnect"); toast(`${ig.name} reconnected`); q.reload(); }}>Reconnect</Button>
              )}
              {ig.status === "available" && (
                <Button size="sm" variant="primary" icon="plus" onClick={() => toast(`${ig.name} connection wizard requires the OAuth backend (TODO_BACKEND #5)`, "info")}>Connect</Button>
              )}
            </div>
            {ig.connectedAt && <span className="t-micro">Connected {new Date(ig.connectedAt).toLocaleDateString("en-US", { month: "short", year: "numeric" })}</span>}
          </div>
        ))}
      </div>
    </>
  );
}
