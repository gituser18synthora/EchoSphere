import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import { listTenants, simulateAction } from "@/services/api";
import { DataTable, type Column } from "@/components/DataTable";
import { Button, ConfirmModal, Health, MenuButton, StatusChip } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import type { Tenant } from "@/types/domain";
import { fmtNum } from "@/components/charts";

export default function Organizations() {
  const navigate = useNavigate();
  const { toast } = useApp();
  const tenantsQ = useAsync(listTenants, []);
  const [query, setQuery] = useState("");
  const [plan, setPlan] = useState("all");
  const [status, setStatus] = useState("all");
  const [suspendTarget, setSuspendTarget] = useState<Tenant | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    let r = tenantsQ.data ?? [];
    if (query) {
      const q = query.toLowerCase();
      r = r.filter((t) => t.name.toLowerCase().includes(q) || t.domain.toLowerCase().includes(q) || t.industry.toLowerCase().includes(q));
    }
    if (plan !== "all") r = r.filter((t) => t.plan === plan);
    if (status !== "all") r = r.filter((t) => t.status === status);
    return r;
  }, [tenantsQ.data, query, plan, status]);

  const suspend = async () => {
    if (!suspendTarget) return;
    setBusy(true);
    await simulateAction("suspend");
    setBusy(false);
    toast(`${suspendTarget.name} suspended. Calls stop routing within 60s; data retained 90 days.`);
    setSuspendTarget(null);
  };

  const columns: Column<Tenant>[] = [
    {
      key: "name", header: "Organization", sortValue: (t) => t.name,
      render: (t) => (
        <div className="row gap-12">
          <div className="icon-tile brand" style={{ width: 30, height: 30 }}><Icon name="building" size={14} /></div>
          <div>
            <div className="t-strong">{t.name}</div>
            <div className="t-micro">{t.domain} · {t.industry}</div>
          </div>
        </div>
      ),
    },
    { key: "plan", header: "Plan", sortValue: (t) => t.plan, render: (t) => <span className="tag" style={{ textTransform: "capitalize" }}>{t.plan}</span> },
    { key: "status", header: "Status", sortValue: (t) => t.status, render: (t) => <StatusChip status={t.status} /> },
    { key: "health", header: "Health", sortValue: (t) => t.health, render: (t) => <Health level={t.health} /> },
    { key: "bots", header: "Bots", align: "right", sortValue: (t) => t.bots, render: (t) => <span className="t-num">{t.bots}</span> },
    { key: "calls", header: "Calls / mo", align: "right", sortValue: (t) => t.callsMonth, render: (t) => <span className="t-num">{fmtNum(t.callsMonth)}</span> },
    { key: "mrr", header: "MRR", align: "right", sortValue: (t) => t.mrr, render: (t) => <span className="t-num">${fmtNum(t.mrr)}</span> },
    { key: "aiCost", header: "AI cost", align: "right", sortValue: (t) => t.aiCostMonth, render: (t) => <span className="t-num">${fmtNum(t.aiCostMonth)}</span> },
    { key: "region", header: "Region", sortValue: (t) => t.region },
    {
      key: "actions", header: "", width: 48,
      render: (t) => (
        <MenuButton
          actions={[
            { label: "Open tenant", icon: "external", onClick: () => navigate(`/admin/tenants/${t.id}`) },
            { label: "View usage", icon: "chart", onClick: () => navigate("/admin/usage") },
            "sep",
            t.status === "suspended"
              ? { label: "Reactivate", icon: "check-circle", onClick: () => toast(`${t.name} reactivation requires a settled invoice — see Billing.`, "info") }
              : { label: "Suspend tenant", icon: "x-circle", danger: true, onClick: () => setSuspendTarget(t) },
          ]}
        />
      ),
    },
  ];

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Organizations</h1>
          <p className="page-sub">{tenantsQ.data ? `${tenantsQ.data.length} tenants` : "Loading tenants…"} · multi-tenant isolation enforced per row</p>
        </div>
        <div className="page-actions">
          <Button variant="primary" icon="plus" onClick={() => navigate("/admin/onboarding")}>New tenant</Button>
        </div>
      </div>

      <div className="filter-bar">
        <div className="search-box">
          <Icon name="search" size={14} />
          <input className="input" placeholder="Search name, domain, industry…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search organizations" />
        </div>
        <select className="select" value={plan} onChange={(e) => setPlan(e.target.value)} aria-label="Filter by plan">
          <option value="all">All plans</option>
          <option value="enterprise">Enterprise</option>
          <option value="growth">Growth</option>
          <option value="starter">Starter</option>
        </select>
        <select className="select" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Filter by status">
          <option value="all">All statuses</option>
          <option value="active">Active</option>
          <option value="trial">Trial</option>
          <option value="suspended">Suspended</option>
          <option value="provisioning">Provisioning</option>
        </select>
      </div>

      <div className="card">
        <DataTable
          columns={columns}
          rows={rows}
          loading={tenantsQ.loading}
          error={tenantsQ.error}
          onRetry={tenantsQ.reload}
          onRowClick={(t) => navigate(`/admin/tenants/${t.id}`)}
          empty={{
            icon: "building",
            title: query || plan !== "all" || status !== "all" ? "No organizations match these filters" : "No tenants yet",
            body: query ? "Try a different search term or clear the filters." : "Onboard your first tenant to get started.",
            action: <Button variant="primary" icon="rocket" onClick={() => navigate("/admin/onboarding")}>Onboard tenant</Button>,
          }}
        />
      </div>

      <ConfirmModal
        open={suspendTarget !== null}
        onClose={() => setSuspendTarget(null)}
        onConfirm={suspend}
        busy={busy}
        danger
        title={`Suspend ${suspendTarget?.name}?`}
        confirmLabel="Suspend tenant"
        body={
          <>
            <p>All published bots stop receiving calls within 60 seconds, users lose access, and phone numbers are parked. Data is retained for 90 days.</p>
            <p className="mt-8">This action is recorded in the audit log and is reversible from this screen.</p>
          </>
        }
      />
    </>
  );
}
