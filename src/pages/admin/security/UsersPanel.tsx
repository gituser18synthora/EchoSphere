import { useState } from "react";
import { DataTable } from "@/components/DataTable";
import { Avatar, Button, Callout, ConfirmModal, ErrorState, Field, MenuButton, Modal, PasswordInput, StatusChip } from "@/components/ui";
import { useAsync } from "@/hooks/useAsync";
import { useApp } from "@/state/AppContext";
import * as api from "@/services/api";
import { passwordPolicyError, PASSWORD_POLICY_HINT } from "@/services/password";
import type { RoleInfo, SecurityUserFilters, TeamMember, Tenant } from "@/types/domain";
import { Paging } from "./Paging";
const message = (e: unknown) => e instanceof Error ? e.message : "Could not complete the action.";

function UserEditor({ member, roles, tenants, onClose, onSaved, onSecret }: {
  member: TeamMember | null; roles: RoleInfo[]; tenants: Tenant[]; onClose: () => void; onSaved: () => void;
  onSecret: (email: string, password: string) => void;
}) {
  const { user } = useApp();
  const [name, setName] = useState(member?.name ?? "");
  const [email, setEmail] = useState(member?.email ?? "");
  const [tenantId, setTenantId] = useState(member?.tenantId ?? "");
  const [role, setRole] = useState(member?.roleCode ?? "super_admin");
  const [temporary, setTemporary] = useState(true);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(false);
  const options = roles.filter((r) => r.scope === (tenantId ? "tenant" : "platform"));
  const self = member?.id === user?.id;
  const validate = () => {
    if (!name.trim() || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) return "Enter a name and valid email address.";
    if (!options.some((r) => r.code === role)) return "Select an available role.";
    if (!member && !temporary) return passwordPolicyError(password) || (password !== confirm ? "Passwords do not match." : "");
    return "";
  };
  const submit = async () => {
    const problem = validate(); if (problem) { setError(problem); return; }
    if (!review) { setReview(true); setError(""); return; }
    setBusy(true); setError("");
    try {
      if (member) await api.updateUser(member.id, { name: name.trim(), email: email.trim(), roleCode: role });
      else {
        const result = await api.inviteUser({ name: name.trim(), email: email.trim(), roleCode: role,
          ...(tenantId ? { tenantId } : {}), ...(!temporary ? { password } : {}) });
        if (result.temporaryPassword) onSecret(email.trim(), result.temporaryPassword);
      }
      window.dispatchEvent(new Event("echosphere:permissions-changed"));
      onSaved(); onClose();
    } catch (e) { setError(message(e)); }
    finally { setBusy(false); }
  };
  return <Modal open title={member ? "Edit user" : "Create user"} onClose={() => { if (!busy) onClose(); }}
    footer={<><Button variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
      {review && <Button disabled={busy} onClick={() => setReview(false)}>Back</Button>}
      <Button variant="primary" busy={busy} onClick={() => void submit()}>{review ? "Confirm and save" : "Review changes"}</Button></>}>
    <div className="col gap-12">
      {error && <div role="alert"><Callout tone="critical">{error}</Callout></div>}
      {review ? <><p>{member ? "Update" : "Create"} <strong>{name}</strong> ({email})?</p>
        <p>Organization: {tenants.find((t) => t.id === tenantId)?.name ?? "Platform"}</p>
        <p>Role: {roles.find((r) => r.code === role)?.name}. {member?.roleCode !== role && member ? `Previous role: ${member.role}.` : ""}</p>
        <Callout tone="warning">This role determines the account's available actions. {role === "super_admin" ? "Super Admin has platform-wide administration access." : "The role's current permissions apply."}</Callout>
      </> : <>
        <Field label="Name" required><input className="input" value={name} maxLength={150} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Email" required><input className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
        <Field label="Organization" hint={member ? "Organization assignment is preserved when editing an account." : undefined}>
          <select className="select" value={tenantId} disabled={!!member} onChange={(e) => {
            setTenantId(e.target.value); setRole(e.target.value ? "tenant_user" : "super_admin");
          }}><option value="">Platform</option>{tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select>
        </Field>
        <Field label="Role" hint={self ? "Your own role is protected from accidental changes." : undefined}>
          <select className="select" value={role} disabled={self} onChange={(e) => setRole(e.target.value)}>{options.map((r) => <option key={r.id} value={r.code}>{r.name}</option>)}</select>
        </Field>
        {!member && <>
          <label className="row gap-8"><input type="checkbox" checked={temporary} onChange={(e) => setTemporary(e.target.checked)} />Issue a temporary password</label>
          {temporary ? <p className="field-hint">Shown once after creation. Share it securely; the user must choose a new password when signing in. No email is sent.</p> : <>
            <Field label="Password" hint={PASSWORD_POLICY_HINT}><PasswordInput value={password} onChange={setPassword} aria-label="Password" autoComplete="new-password" /></Field>
            <Field label="Confirm password"><PasswordInput value={confirm} onChange={setConfirm} aria-label="Confirm password" autoComplete="new-password" /></Field>
          </>}
        </>}
      </>}
    </div>
  </Modal>;
}

function PasswordReset({ member, onClose, onSaved, onSecret }: {
  member: TeamMember; onClose: () => void; onSaved: () => void; onSecret: (email: string, secret: string) => void;
}) {
  const [password, setPassword] = useState(""); const [confirm, setConfirm] = useState("");
  const [temporary, setTemporary] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const save = async () => {
    const problem = temporary ? "" : passwordPolicyError(password) || (password !== confirm ? "Passwords do not match." : "");
    if (problem) { setError(problem); return; }
    setBusy(true); setError("");
    try {
      const result = await api.resetUserPassword(member.id, temporary ? undefined : { newPassword: password, confirmPassword: confirm });
      if (result.temporaryPassword) onSecret(member.email, result.temporaryPassword);
      onSaved(); onClose();
    } catch (e) { setError(message(e)); } finally { setBusy(false); }
  };
  return <Modal open title="Reset password" onClose={() => { if (!busy) onClose(); }}
    footer={<><Button disabled={busy} onClick={onClose}>Cancel</Button><Button variant="primary" busy={busy} onClick={() => void save()}>Confirm reset</Button></>}>
    <div className="col gap-12"><p>Reset the password for <strong>{member.email}</strong>. Existing sessions will be signed out.</p>
      {error && <div role="alert"><Callout tone="critical">{error}</Callout></div>}
      <label><input type="checkbox" checked={temporary} onChange={(e) => setTemporary(e.target.checked)} /> Issue a temporary password</label>
      {!temporary && <><Field label="New password" hint={PASSWORD_POLICY_HINT}><PasswordInput value={password} onChange={setPassword} aria-label="New password" /></Field>
        <Field label="Confirm password"><PasswordInput value={confirm} onChange={setConfirm} aria-label="Confirm password" /></Field></>}
    </div>
  </Modal>;
}

export default function UsersPanel() {
  const { user, toast } = useApp();
  const [filters, setFilters] = useState<SecurityUserFilters>({ scope: "platform", page: 1, pageSize: 25 });
  const q = useAsync(() => filters.scope === "tenant" && !filters.tenantId ? Promise.reject(new Error("Select an organization to view its members.")) : api.listSecurityUsers(filters), [filters]);
  const catalogs = useAsync(() => Promise.all([api.listRoles(), api.listAllTenants()]), []);
  const [editor, setEditor] = useState<{ member: TeamMember | null } | null>(null);
  const [reset, setReset] = useState<TeamMember | null>(null);
  const [statusTarget, setStatusTarget] = useState<TeamMember | null>(null);
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState<{ email: string; value: string } | null>(null);
  const roles = catalogs.data?.[0] ?? []; const tenants = catalogs.data?.[1] ?? [];
  const change = (key: keyof SecurityUserFilters, value: string | number) => setFilters((f) => ({ ...f, [key]: value || undefined, page: 1 }));
  const saved = () => { q.reload(); toast("User updated."); };
  const showSecret = (email: string, value: string) => setSecret({ email, value });
  const changeStatus = async () => {
    if (!statusTarget) return; setBusy(true);
    try { await api.updateUser(statusTarget.id, { status: statusTarget.status === "deactivated" ? "active" : "deactivated" }); saved(); setStatusTarget(null); }
    catch (e) { toast(message(e), "error"); } finally { setBusy(false); }
  };
  return <div className="col gap-12">
    <div className="card card-pad col gap-12"><div className="row-between gap-12"><p className="t-sub">Manage platform accounts and organization members.</p>
      <div className="row gap-8"><Button onClick={q.reload} disabled={q.loading}>Refresh</Button><Button variant="primary" disabled={!catalogs.data} onClick={() => setEditor({ member: null })}>Create user</Button></div></div>
      <div className="row gap-12 wrap">
        <Field label="Search users"><input className="input" placeholder="Name or email" value={filters.search ?? ""} onChange={(e) => change("search", e.target.value)} /></Field>
        <Field label="Scope"><select className="select" value={filters.scope} onChange={(e) => setFilters((f) => ({ ...f, scope: e.target.value as SecurityUserFilters["scope"], tenantId: undefined, roleCode: undefined, page: 1 }))}>
          <option value="platform">Platform users</option><option value="all">All users</option><option value="tenant">Organization members</option></select></Field>
        <Field label="Organization"><select className="select" disabled={filters.scope === "platform"} value={filters.tenantId ?? ""} onChange={(e) => change("tenantId", e.target.value)}><option value="">{filters.scope === "tenant" ? "Select organization" : "All organizations"}</option>{tenants.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</select></Field>
        <Field label="Role"><select className="select" value={filters.roleCode ?? ""} onChange={(e) => change("roleCode", e.target.value)}><option value="">All roles</option>{roles.map((r) => <option key={r.id} value={r.code}>{r.name}</option>)}</select></Field>
        <Field label="Status"><select className="select" value={filters.status ?? ""} onChange={(e) => change("status", e.target.value)}><option value="">All statuses</option>{["active", "invited", "deactivated"].map((s) => <option key={s}>{s}</option>)}</select></Field>
      </div>
      {catalogs.error && <ErrorState message={catalogs.error} onRetry={catalogs.reload} />}
    </div>
    <div className="card"><DataTable<TeamMember> rows={q.data?.items ?? null} loading={q.loading} error={q.error} onRetry={q.reload} empty={{ title: "No users match these filters" }} columns={[
      { key: "name", header: "User", render: (m) => <div className="row gap-8"><Avatar name={m.name} /><div>{m.name}<div className="t-micro">{m.email}</div></div></div> },
      { key: "organization", header: "Organization", render: (m) => m.tenantName ?? (m.tenantId ? "Organization" : "Platform") },
      { key: "role", header: "Role" }, { key: "status", header: "Status", render: (m) => <StatusChip status={m.status} /> },
      { key: "mfa", header: "MFA setting", render: (m) => m.mfa ? "Enabled" : "Pending" },
      { key: "lastLoginAt", header: "Last login", render: (m) => m.lastLoginAt ? new Date(m.lastLoginAt).toLocaleString() : "Never" },
      { key: "actions", header: "Actions", render: (m) => <MenuButton label={`Actions for ${m.name}`} actions={[
        { label: "Edit user / role", onClick: () => setEditor({ member: m }), disabled: !catalogs.data },
        { label: "Reset password", onClick: () => setReset(m), disabled: m.id === user?.id },
        { label: m.status === "deactivated" ? "Reactivate" : "Deactivate", onClick: () => setStatusTarget(m), danger: m.status !== "deactivated", disabled: m.id === user?.id },
      ]} /> },
    ]} /></div>
    <Paging page={filters.page ?? 1} size={filters.pageSize ?? 25} total={q.data?.meta.total ?? 0} pages={q.data?.meta.totalPages ?? 1} loading={q.loading} onPage={(page) => setFilters((f) => ({ ...f, page }))} onSize={(size) => change("pageSize", size)} />
    {editor && <UserEditor member={editor.member} roles={roles} tenants={tenants} onClose={() => setEditor(null)} onSaved={saved} onSecret={showSecret} />}
    {reset && <PasswordReset member={reset} onClose={() => setReset(null)} onSaved={saved} onSecret={showSecret} />}
    <ConfirmModal open={!!statusTarget} title={statusTarget?.status === "deactivated" ? "Reactivate account" : "Deactivate account"} onClose={() => { if (!busy) setStatusTarget(null); }} onConfirm={() => void changeStatus()} busy={busy} danger={statusTarget?.status !== "deactivated"} body={`Change account access for ${statusTarget?.name ?? "this user"}?`} />
    <Modal open={!!secret} title="Temporary password" onClose={() => setSecret(null)} footer={<Button onClick={() => setSecret(null)}>I have saved it securely</Button>}>
      <div className="col gap-12"><p>{secret?.email}: share this password securely. It is shown only until this dialog closes. No email is sent.</p><PasswordInput value={secret?.value ?? ""} onChange={() => undefined} aria-label="Temporary password" readOnly autoComplete="off" /></div>
    </Modal>
  </div>;
}
