import { useMemo, useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listInvoices, listTenants } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Button, KpiCard, StatusChip, CardSkeleton } from "@/components/ui";
import { ExportControls } from "@/components/ExportControls";
import { fmtNum } from "@/components/charts";
import { useApp } from "@/state/AppContext";
import {
  downloadInvoicePdf,
  downloadOperationalExport,
} from "@/services/exportDownload";

export default function Billing() {
  const q = useAsync(listInvoices, []);
  const tenantsQ = useAsync(listTenants, []);
  const { toast } = useApp();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("");
  const [pdfBusy, setPdfBusy] = useState<Set<string>>(() => new Set());

  const mrr = tenantsQ.data?.reduce((s, t) => s + t.mrr, 0) ?? 0;
  const open = q.data?.filter((i) => i.status === "open").reduce((s, i) => s + i.amount, 0) ?? 0;
  const pastDue = q.data?.filter((i) => i.status === "past_due").reduce((s, i) => s + i.amount, 0) ?? 0;
  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return (q.data ?? []).filter((invoice) => (
      (!needle
        || invoice.id.toLowerCase().includes(needle)
        || invoice.tenant.toLowerCase().includes(needle)
        || invoice.period.toLowerCase().includes(needle))
      && (!status || invoice.status === status)
    ));
  }, [q.data, search, status]);

  const startPdfDownload = async (invoiceId: string) => {
    if (pdfBusy.has(invoiceId)) return;
    setPdfBusy((current) => new Set(current).add(invoiceId));
    try {
      const filename = await downloadInvoicePdf(invoiceId);
      toast(`Downloaded ${filename}`);
    } catch (error) {
      toast(error instanceof Error ? error.message : "Invoice download failed.", "error");
    } finally {
      setPdfBusy((current) => {
        const next = new Set(current);
        next.delete(invoiceId);
        return next;
      });
    }
  };

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Billing</h1>
          <p className="page-sub">Invoices and collections across all tenants</p>
        </div>
        <div className="page-actions">
          <ExportControls
            buttonLabel="Export"
            onDownload={(format) => downloadOperationalExport(
              "invoices",
              format,
              {
                search: search.trim() || undefined,
                status: status || undefined,
              },
            )}
          />
        </div>
      </div>

      <div className="filter-bar">
        <div className="search-box">
          <input
            className="input"
            aria-label="Search invoices"
            placeholder="Search invoice, tenant or period…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <select
          className="select"
          aria-label="Filter invoices by status"
          value={status}
          onChange={(event) => setStatus(event.target.value)}
        >
          <option value="">All statuses</option>
          <option value="paid">Paid</option>
          <option value="open">Open</option>
          <option value="past_due">Past due</option>
          <option value="void">Void</option>
        </select>
      </div>

      <div className="grid grid-4">
        {tenantsQ.loading || q.loading ? Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={1} />) : (
          <>
            <KpiCard label="MRR" value={`$${fmtNum(mrr)}`} icon="dollar" />
            <KpiCard label="Collected" value={`$${fmtNum((q.data ?? []).filter((i) => i.status === "paid").reduce((s, i) => s + i.amount, 0))}`} icon="check-circle" />
            <KpiCard label="Outstanding" value={`$${fmtNum(open)}`} icon="clock" />
            <KpiCard label="Past due" value={`$${fmtNum(pastDue)}`} icon="alert" />
          </>
        )}
      </div>

      <div className="card mt-16">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={rows}
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
              render: (i) => (
                <div className="row gap-4">
                  {i.status === "past_due" && (
                    <Button
                      size="sm"
                      variant="danger-ghost"
                      onClick={() => toast(`Dunning reminder sent for ${i.id}`)}
                    >
                      Send reminder
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    icon="download"
                    busy={pdfBusy.has(i.id)}
                    onClick={() => void startPdfDownload(i.id)}
                  >
                    PDF
                  </Button>
                </div>
              ),
            },
          ]}
        />
      </div>
    </>
  );
}
