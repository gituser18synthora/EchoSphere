import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listSubscriptions } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Progress, StatusChip } from "@/components/ui";
import { ExportControls } from "@/components/ExportControls";
import { fmtNum } from "@/components/charts";
import { downloadOperationalExport } from "@/services/exportDownload";

export default function Subscriptions() {
  const q = useAsync(listSubscriptions, []);
  const navigate = useNavigate();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [plan, setPlan] = useState("");

  const plans = useMemo(
    () => [...new Set((q.data ?? []).map((subscription) => subscription.plan))].sort(),
    [q.data],
  );
  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (q.data ?? []).filter((subscription) => (
      (!needle
        || subscription.id.toLowerCase().includes(needle)
        || subscription.tenant.toLowerCase().includes(needle)
        || subscription.plan.toLowerCase().includes(needle))
      && (!status || subscription.status === status)
      && (!plan || subscription.plan === plan)
    ));
  }, [q.data, search, status, plan]);

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Subscriptions</h1>
          <p className="page-sub">Plan limits, consumption and renewal state per tenant</p>
        </div>
        <div className="page-actions">
          <ExportControls
            buttonLabel="Export"
            onDownload={(format) => downloadOperationalExport(
              "subscriptions",
              format,
              {
                search: search.trim() || undefined,
                status: status || undefined,
                plan: plan || undefined,
              },
            )}
          />
        </div>
      </div>
      <div className="filter-bar">
        <div className="search-box">
          <input
            className="input"
            aria-label="Search subscriptions"
            placeholder="Search subscription, tenant or plan…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <select
          className="select"
          aria-label="Filter subscriptions by status"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
        >
          <option value="">All statuses</option>
          <option value="active">Active</option>
          <option value="trial">Trial</option>
          <option value="past_due">Past due</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <select
          className="select"
          aria-label="Filter subscriptions by plan"
          value={plan}
          onChange={(event) => setPlan(event.target.value)}
        >
          <option value="">All plans</option>
          {plans.map((code) => <option key={code} value={code}>{code}</option>)}
        </select>
      </div>
      <div className="card">
        <DataTable
          loading={q.loading}
          error={q.error}
          onRetry={q.reload}
          rows={rows}
          rowKey={(s) => s.id}
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
                const pct = s.minutesIncluded
                  ? (s.minutesUsed / s.minutesIncluded) * 100
                  : 0;
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
