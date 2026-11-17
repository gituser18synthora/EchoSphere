import { useState } from "react";
import { Button, ErrorState, Field, Modal } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { useAsync } from "@/hooks/useAsync";
import { useApp } from "@/state/AppContext";
import * as api from "@/services/api";
import type { AuditEvent, AuditFilters } from "@/types/domain";
import { Paging } from "./Paging";
const initial: AuditFilters = { page: 1, pageSize: 25 };
const time = (value: string) => `${new Date(value).toLocaleString(undefined, { timeZone: "UTC" })} UTC`;

function EventDetail({ event, onClose }: { event: AuditEvent; onClose: () => void }) {
  const q = useAsync(() => api.getAuditEvent(event.id), [event.id]);
  return <Modal open title="Audit event" sub={`${event.action} · ${time(event.time)}`} wide onClose={onClose} footer={<Button onClick={onClose}>Close</Button>}>
    {q.loading && <p role="status">Loading event details…</p>}
    {q.error && <ErrorState message={q.error} onRetry={q.reload} />}
    {q.data && !q.loading && <div className="col gap-12"><dl>
      <dt>Actor</dt><dd>{q.data.actor} ({q.data.actorRole})</dd><dt>Organization</dt><dd>{q.data.tenant ?? "Platform"}</dd>
      <dt>Target</dt><dd>{q.data.target}</dd><dt>Resource type</dt><dd>{q.data.entityType ?? "—"}</dd>
      <dt>IP address</dt><dd>{q.data.ip}</dd></dl><p className="field-hint">Sensitive fields are hidden. An empty snapshot means no value was recorded.</p>
      {([["Before", q.data.previousValue], ["After", q.data.newValue]] as const).map(([title, value]) => <div className="card card-pad" key={title}><strong>{title}</strong>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: 320, overflow: "auto" }}>{value == null ? "No recorded value" : JSON.stringify(value, null, 2)}</pre>
      </div>)}
    </div>}
  </Modal>;
}

export default function AuditLogPanel() {
  const { toast } = useApp();
  const [filters, setFilters] = useState<AuditFilters>({ ...initial });
  const [exporting, setExporting] = useState(false); const [selected, setSelected] = useState<AuditEvent | null>(null);
  const datesInvalid = !!(filters.dateFrom && filters.dateTo && filters.dateFrom > filters.dateTo);
  const q = useAsync(() => datesInvalid ? Promise.reject(new Error("From date must be on or before To date.")) : api.listAuditPage(filters), [filters, datesInvalid]);
  const tenants = useAsync(api.listAllTenants, []);
  const change = (key: keyof AuditFilters, value: string | number) => setFilters((f) => ({ ...f, [key]: value === "" ? undefined : value, page: 1 }));
  const exportCsv = async () => {
    setExporting(true);
    try { await api.downloadAuditExport(filters); toast("Filtered audit history downloaded."); }
    catch (e) { toast(e instanceof Error ? e.message : "Could not export audit history.", "error"); }
    finally { setExporting(false); }
  };
  return <div className="col gap-12">
    <div className="card card-pad col gap-12"><div className="row-between gap-12"><p className="t-sub">Search recorded activity and open an event to inspect its changes.</p>
      <div className="row gap-8"><Button disabled={q.loading} onClick={q.reload}>Refresh</Button><Button disabled={datesInvalid || q.loading} busy={exporting} onClick={() => void exportCsv()}>Export filtered CSV</Button></div></div>
      <div className="row gap-12 wrap">
        <Field label="Search activity"><input className="input" value={filters.search ?? ""} onChange={(e) => change("search", e.target.value)} /></Field>
        <Field label="Actor"><input className="input" placeholder="Name" value={filters.actor ?? ""} onChange={(e) => change("actor", e.target.value)} /></Field>
        <Field label="Action"><input className="input" placeholder="For example, Updated" value={filters.action ?? ""} onChange={(e) => change("action", e.target.value)} /></Field>
        <Field label="Organization"><select className="select" value={filters.tenantId ?? ""} onChange={(e) => change("tenantId", e.target.value)} disabled={tenants.loading}>
          <option value="">All organizations and platform</option><option value="__platform__">Platform only</option>{(tenants.data ?? []).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></Field>
        <Field label="Resource type"><input className="input" placeholder="For example, user" value={filters.entityType ?? ""} onChange={(e) => change("entityType", e.target.value)} /></Field>
        <Field label="From date (UTC)"><input className="input" type="date" value={filters.dateFrom ?? ""} onChange={(e) => change("dateFrom", e.target.value)} /></Field>
        <Field label="To date (UTC)" error={datesInvalid ? "Choose a date on or after From date." : undefined}><input className="input" type="date" value={filters.dateTo ?? ""} onChange={(e) => change("dateTo", e.target.value)} /></Field>
      </div><Button variant="ghost" onClick={() => setFilters({ ...initial })}>Clear filters</Button>
      {tenants.error && <ErrorState message={tenants.error} onRetry={tenants.reload} />}
    </div>
    <div className="card"><DataTable<AuditEvent> rows={q.data?.items ?? null} loading={q.loading} error={q.error} onRetry={q.reload} onRowClick={setSelected} empty={{ title: "No audit events match these filters" }} columns={[
      { key: "time", header: "Time (UTC)", render: (row) => time(row.time) },
      { key: "actor", header: "Actor", render: (row) => <div>{row.actor}<div className="t-micro">{row.actorRole.replaceAll("_", " ")}</div></div> },
      { key: "action", header: "Action" }, { key: "target", header: "Target" },
      { key: "tenant", header: "Organization", render: (row) => row.tenant ?? "Platform" }, { key: "ip", header: "IP" },
    ]} /></div>
    <Paging page={filters.page ?? 1} size={filters.pageSize ?? 25} total={q.data?.meta.total ?? 0} pages={q.data?.meta.totalPages ?? 1} loading={q.loading}
      onPage={(page) => setFilters((f) => ({ ...f, page }))} onSize={(size) => change("pageSize", size)} />
    {selected && <EventDetail event={selected} onClose={() => setSelected(null)} />}
  </div>;
}
