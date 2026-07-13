import { useAsync } from "@/hooks/useAsync";
import { listInvoices, listTenants } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Button, KpiCard, StatusChip, CardSkeleton } from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";

export default function Billing() {
  const q = useAsync(listInvoices, []);
  const tenantsQ = useAsync(listTenants, []);
  const { toast } = useApp();

  const mrr = tenantsQ.data?.reduce((s, t) => s + t.mrr, 0) ?? 0;
  const open = q.data?.filter((i) => i.status === "open").reduce((s, i) => s + i.amount, 0) ?? 0;
  const pastDue = q.data?.filter((i) => i.status === "past_due").reduce((s, i) => s + i.amount, 0) ?? 0;

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Billing</h1>
          <p className="page-sub">Invoices and collections across all tenants</p>
        </div>
        <div className="page-actions">
          <Button icon="download" onClick={() => toast("Export queued — backend job API pending (TODO_BACKEND #6)", "info")}>Export CSV</Button>
        </div>
      </div>

      <div className="grid grid-4">
        {tenantsQ.loading || q.loading ? Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />) : (
          <>
            <KpiCard label="MRR" value={`$${fmtNum(mrr)}`} delta={4.2} icon="dollar" />
            <KpiCard label="Collected (Jun)" value={`$${fmtNum((q.data ?? []).filter((i) => i.status === "paid").reduce((s, i) => s + i.amount, 0))}`} icon="check-circle" />
            <KpiCard label="Outstanding" value={`$${fmtNum(open)}`} icon="clock" />
            <KpiCard label="Past due" value={`$${fmtNum(pastDue)}`} icon="alert" />
          </>
        )}
      </div>

      <div className="card mt-16">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
          empty={{ icon: "card", title: "No invoices" }}
          columns={[
            { key: "id", header: "Invoice", sortValue: (i) => i.id, render: (i) => <code style={{ fontSize: 12 }}>{i.id}</code> },
            { key: "tenant", header: "Tenant", sortValue: (i) => i.tenant, render: (i) => <span className="t-strong">{i.tenant}</span> },
            { key: "period", header: "Period" },
            { key: "amount", header: "Amount", align: "right", sortValue: (i) => i.amount, render: (i) => <span className="t-num">${i.amount.toLocaleString()}</span> },
            { key: "status", header: "Status", sortValue: (i) => i.status, render: (i) => <StatusChip status={i.status} /> },
            { key: "issued", header: "Issued", render: (i) => <span className="t-sub">{new Date(i.issuedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}</span> },
            {
              key: "act", header: "", width: 120,
              render: (i) => i.status === "past_due"
                ? <Button size="sm" variant="danger-ghost" onClick={() => toast(`Dunning reminder sent for ${i.id}`)}>Send reminder</Button>
                : <Button size="sm" variant="ghost" onClick={() => toast("PDF generation pending backend export job (TODO_BACKEND #6)", "info")}>PDF</Button>,
            },
          ]}
        />
      </div>
    </>
  );
}
