import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listAudit } from "@/services/api";
import { DataTable } from "@/components/DataTable";
import { Avatar, StatusChip, Tabs } from "@/components/ui";
import { Icon } from "@/components/Icon";

const tabs = [
  { id: "users", label: "Users" },
  { id: "roles", label: "Roles" },
  { id: "audit", label: "Audit Logs" },
];

const platformUsers = [
  { id: "pu-1", name: "Alex Rivera", email: "alex.rivera@aurexion.com", role: "Super Admin", status: "active", mfa: true },
  { id: "pu-2", name: "Sofia Chen", email: "sofia.chen@aurexion.com", role: "Platform Engineer", status: "active", mfa: true },
  { id: "pu-3", name: "Ravi Patel", email: "ravi.patel@aurexion.com", role: "Support (read-only)", status: "active", mfa: true },
  { id: "pu-4", name: "Emma Ford", email: "emma.ford@aurexion.com", role: "Billing Ops", status: "invited", mfa: false },
];

const roles = [
  { id: "r-1", name: "Super Admin", scope: "Platform", perms: "Full control: tenants, governance, security, billing", members: 2 },
  { id: "r-2", name: "Platform Engineer", scope: "Platform", perms: "Monitoring, telephony, knowledge pipeline; no billing", members: 4 },
  { id: "r-3", name: "Billing Ops", scope: "Platform", perms: "Subscriptions, invoices, dunning; read-only elsewhere", members: 3 },
  { id: "r-4", name: "Support (read-only)", scope: "Platform", perms: "Read-only tenant views, no PII transcript access", members: 6 },
  { id: "r-5", name: "Tenant Admin", scope: "Tenant", perms: "Bots, knowledge, workflows, publish, team — within own tenant", members: 47 },
  { id: "r-6", name: "Bot Manager", scope: "Tenant", perms: "Edit + test assigned bots; publish requires approval", members: 82 },
  { id: "r-7", name: "QA Reviewer", scope: "Tenant", perms: "Conversation review, scorecards, flags; no editing", members: 31 },
];

export default function Security() {
  const [tab, setTab] = useState("users");
  const audit = useAsync(listAudit, []);
  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Security</h1>
          <p className="page-sub">Platform users, role definitions and the immutable audit trail</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <div className="mt-16">
        {tab === "users" && (
          <div className="card">
            <DataTable
              rows={platformUsers}
              empty={{ icon: "users", title: "No platform users" }}
              columns={[
                { key: "name", header: "User", sortValue: (u) => u.name, render: (u) => <div className="row gap-12"><Avatar name={u.name} /><div><div className="t-strong">{u.name}</div><div className="t-micro">{u.email}</div></div></div> },
                { key: "role", header: "Role", render: (u) => <span className="tag">{u.role}</span> },
                { key: "status", header: "Status", render: (u) => <StatusChip status={u.status} /> },
                { key: "mfa", header: "MFA", render: (u) => u.mfa ? <span className="row gap-4 t-sub"><Icon name="check-circle" size={14} style={{ color: "var(--status-good)" }} /> Enabled</span> : <StatusChip status="warning" label="Pending" /> },
              ]}
            />
          </div>
        )}
        {tab === "roles" && (
          <div className="card">
            <DataTable
              rows={roles}
              empty={{ icon: "shield", title: "No roles" }}
              columns={[
                { key: "name", header: "Role", sortValue: (r) => r.name, render: (r) => <span className="t-strong">{r.name}</span> },
                { key: "scope", header: "Scope", sortValue: (r) => r.scope, render: (r) => <StatusChip status={r.scope === "Platform" ? "info" : "neutral"} label={r.scope} /> },
                { key: "perms", header: "Permissions", render: (r) => <span className="t-sub" style={{ fontSize: 12.5 }}>{r.perms}</span> },
                { key: "members", header: "Members", align: "right", sortValue: (r) => r.members, render: (r) => <span className="t-num">{r.members}</span> },
              ]}
            />
          </div>
        )}
        {tab === "audit" && (
          <div className="card">
            <DataTable
              loading={audit.loading} error={audit.error} onRetry={audit.reload} rows={audit.data}
              empty={{ icon: "shield", title: "No audit events" }}
              columns={[
                { key: "time", header: "Time", sortValue: (a) => a.time, render: (a) => <span className="t-sub t-num">{new Date(a.time).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span> },
                { key: "actor", header: "Actor", sortValue: (a) => a.actor, render: (a) => <div className="row gap-6"><Avatar name={a.actor} /><div><div style={{ fontSize: 13 }}>{a.actor}</div><div className="t-micro">{a.actorRole === "super_admin" ? "Super Admin" : "Tenant Admin"}</div></div></div> },
                { key: "action", header: "Action", render: (a) => <span className="t-sub">{a.action}</span> },
                { key: "target", header: "Target", render: (a) => <code style={{ fontSize: 12 }}>{a.target}</code> },
                { key: "tenant", header: "Tenant", render: (a) => a.tenant ?? <span className="t-micro">Platform</span> },
                { key: "ip", header: "IP", render: (a) => <span className="t-micro t-num">{a.ip}</span> },
              ]}
            />
          </div>
        )}
      </div>
    </>
  );
}
