import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listSubscriptions } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Button, Progress, StatusChip } from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";

export default function Subscriptions() {
  const q = useAsync(listSubscriptions, []);
  const navigate = useNavigate();
  const { toast } = useApp();

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Subscriptions</h1>
          <p className="page-sub">Plan limits, consumption and renewal state per tenant</p>
        </div>
        <div className="page-actions">
          <Button icon="download" onClick={() => toast("Export queued — backend job API pending (TODO_BACKEND #6)", "info")}>Export</Button>
        </div>
      </div>
      <div className="card">
        <DataTable
          loading={q.loading}
          error={q.error}
          onRetry={q.reload}
          rows={q.data}
          rowKey={(s) => s.tenantId}
          onRowClick={(s) => navigate(`/admin/tenants/${s.tenantId}`)}
          empty={{ icon: "layers", title: "No subscriptions" }}
          columns={[
            { key: "tenant", header: "Tenant", sortValue: (s) => s.tenant, render: (s) => <span className="t-strong">{s.tenant}</span> },
            { key: "plan", header: "Plan", sortValue: (s) => s.plan, render: (s) => <span className="tag" style={{ textTransform: "capitalize" }}>{s.plan}</span> },
            { key: "status", header: "Status", render: (s) => <StatusChip status={s.status} /> },
            { key: "seats", header: "Seats", align: "right", sortValue: (s) => s.seats, render: (s) => <span className="t-num">{s.seats}</span> },
            { key: "bots", header: "Bot limit", align: "right", render: (s) => <span className="t-num">{s.botLimit}</span> },
            {
              key: "minutes", header: "Minutes used", width: 220, sortValue: (s) => s.minutesUsed / s.minutesIncluded,
              render: (s) => {
                const pct = (s.minutesUsed / s.minutesIncluded) * 100;
                return (
                  <div className="col gap-4">
                    <div className="row-between t-micro t-num">
                      <span>{fmtNum(s.minutesUsed)} / {fmtNum(s.minutesIncluded)}</span>
                      <span>{pct.toFixed(0)}%</span>
                    </div>
                    <Progress value={pct} tone={pct > 90 ? "critical" : pct > 75 ? "warning" : "good"} />
                  </div>
                );
              },
            },
            { key: "mrr", header: "MRR", align: "right", sortValue: (s) => s.mrr, render: (s) => <span className="t-num">${fmtNum(s.mrr)}</span> },
            { key: "renews", header: "Renews", render: (s) => <span className="t-sub">{new Date(s.renewsAt).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</span> },
          ]}
        />
      </div>
    </>
  );
}
