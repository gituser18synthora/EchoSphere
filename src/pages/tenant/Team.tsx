import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { inviteUser, listRoles, listTeam } from "@/services/api";
import { Avatar, Button, CardSkeleton, Field, MenuButton, Modal, StatusChip } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { useApp } from "@/state/AppContext";

const prettyPermission = (code: string) => {
  const [subject, ...rest] = code.split(".");
  const action = rest.join(" ").replace(/_/g, " ");
  const label = (action ? `${action} ${subject}` : subject).replace(/_/g, " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
};

export default function Team() {
  const q = useAsync(listTeam, []);
  const rolesQ = useAsync(listRoles, []);
  const { user, toast, hasPermission } = useApp();
  // Server-enforced: user create/update APIs require tenant admin.
  const canManageTeam = hasPermission("team.manage");
  const [inviteOpen, setInviteOpen] = useState(false);
  const [mode, setMode] = useState<"invite" | "create">("invite");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [nameErr, setNameErr] = useState("");
  const [err, setErr] = useState("");
  const [pwErr, setPwErr] = useState("");
  const [busy, setBusy] = useState(false);

  const tenantRoles = (rolesQ.data ?? []).filter((r) => r.scope === "tenant");
  // New members are always added as Tenant User — admin and platform roles
  // are never offered here (the backend rejects them too).
  const memberRoles = tenantRoles.filter((r) => r.code === "tenant_user");
  const selectedRole = memberRoles[0]?.code ?? "tenant_user";

  const closeModal = () => {
    setInviteOpen(false);
    setName(""); setEmail(""); setPassword(""); setConfirm("");
    setNameErr(""); setErr(""); setPwErr("");
    setMode("invite");
  };

  const submit = async () => {
    if (!name.trim()) { setNameErr("Enter the member's name"); return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { setErr("Enter a valid work email"); return; }
    if (mode === "create") {
      // Light client checks; the backend enforces the full password policy.
      if (password.length < 8) { setPwErr("At least 8 characters, with upper/lowercase and a digit"); return; }
      if (password !== confirm) { setPwErr("Passwords do not match"); return; }
    }
    setBusy(true);
    try {
      const created = await inviteUser({
        name: name.trim(), email, roleCode: selectedRole,
        ...(mode === "create" ? { password } : {}),
      });
      toast(mode === "create"
        ? `${name.trim()} can sign in now with the password you set`
        : created.temporaryPassword
          ? `Invite created — temporary password: ${created.temporaryPassword}`
          : `Invite sent to ${email}`);
      q.reload();
      closeModal();
    } catch (e) {
      toast(e instanceof Error ? e.message : mode === "create" ? "Could not create the user" : "Invite failed", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Team</h1>
          <p className="page-sub">Who can build, review and publish — roles are enforced on every action</p>
        </div>
        <div className="page-actions">
          {canManageTeam && (
            <Button variant="primary" icon="plus" onClick={() => setInviteOpen(true)}>Invite / Create Member</Button>
          )}
        </div>
      </div>

      <div className="card">
        <DataTable
          loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
          empty={{ icon: "users", title: "No team members", body: "Invite colleagues to build and review bots with you." }}
          columns={[
            { key: "name", header: "Member", sortValue: (m) => m.name, render: (m) => <div className="row gap-12"><Avatar name={m.name} /><div><div className="t-strong">{m.name}</div><div className="t-micro">{m.email}</div></div></div> },
            { key: "role", header: "Role", sortValue: (m) => m.role, render: (m) => <span className="tag">{m.role}</span> },
            { key: "status", header: "Status", render: (m) => <StatusChip status={m.status} /> },
            { key: "bots", header: "Bots owned", align: "right", sortValue: (m) => m.botsOwned, render: (m) => <span className="t-num">{m.botsOwned}</span> },
            { key: "last", header: "Last active", render: (m) => <span className="t-sub">{m.lastActive === "—" ? "—" : new Date(m.lastActive).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span> },
            {
              key: "act", header: "", width: 48,
              render: (m) => canManageTeam ? (
                <MenuButton actions={[
                  { label: "Change role", icon: "shield", onClick: () => toast(`Role editor opened for ${m.name}`, "info") },
                  { label: "Transfer bot ownership", icon: "bot", onClick: () => toast("Ownership transfer requires the receiving member to accept", "info") },
                  "sep",
                  m.status === "invited"
                    ? { label: "Resend invite", icon: "mail", onClick: () => toast(`Invite resent to ${m.email}`) }
                    : { label: "Deactivate", icon: "x-circle", danger: true, onClick: () => toast(`${m.name} deactivated — owned bots need reassignment`, "info") },
                ]} />
              ) : null,
            },
          ]}
        />
      </div>

      <div className="card card-pad mt-16">
        <span className="card-title">Role capabilities</span>
        {rolesQ.loading ? (
          <div className="mt-12"><CardSkeleton rows={4} /></div>
        ) : (
          <div className="table-wrap mt-12">
            <table className="table">
              <thead>
                <tr><th>Role</th><th className="num">Members</th><th>Capabilities</th></tr>
              </thead>
              <tbody>
                {tenantRoles.map((r) => (
                  <tr key={r.code}>
                    <td><div className="t-strong">{r.name}</div><div className="t-micro">{r.description}</div></td>
                    <td className="num t-num">{r.members}</td>
                    <td>
                      <span className="row gap-4 wrap">
                        {r.permissions.map((p) => <span key={p} className="tag">{prettyPermission(p)}</span>)}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <Modal
        open={inviteOpen} onClose={closeModal}
        title="Invite a team member"
        sub={mode === "invite"
          ? `They'll receive an email to join ${user?.tenantName ?? "your organization"} on EchoSphere.`
          : `The account is created immediately in ${user?.tenantName ?? "your organization"} — no email required.`}
        footer={
          <>
            <Button variant="ghost" onClick={closeModal}>Cancel</Button>
            <Button variant="primary" icon={mode === "invite" ? "send" : "plus"} busy={busy} onClick={submit}>
              {mode === "invite" ? "Send invite" : "Create user"}
            </Button>
          </>
        }
      >
        <div className="col gap-16">
          <div className="segmented" role="group" aria-label="How to add the member">
            <button aria-pressed={mode === "invite"} onClick={() => { setMode("invite"); setPwErr(""); }}>Invite user</button>
            <button aria-pressed={mode === "create"} onClick={() => setMode("create")}>Create user</button>
          </div>
          <Field label="Name" required error={nameErr}>
            <input className="input" value={name} autoFocus onChange={(e) => { setName(e.target.value); setNameErr(""); }} placeholder="Full name" aria-invalid={!!nameErr} />
          </Field>
          <Field label="Work email" required error={err}>
            <input className="input" value={email} onChange={(e) => { setEmail(e.target.value); setErr(""); }} placeholder="colleague@company.com" aria-invalid={!!err} />
          </Field>
          {mode === "create" && (
            <>
              <Field label="Password" required error={pwErr}
                     hint="At least 8 characters with an uppercase letter, a lowercase letter and a digit.">
                <input className="input" type="password" value={password} autoComplete="new-password"
                       onChange={(e) => { setPassword(e.target.value); setPwErr(""); }} aria-invalid={!!pwErr} />
              </Field>
              <Field label="Confirm password" required>
                <input className="input" type="password" value={confirm} autoComplete="new-password"
                       onChange={(e) => { setConfirm(e.target.value); setPwErr(""); }} />
              </Field>
            </>
          )}
          <Field label="Role" hint="New members join as Tenant User — they work on the organization's shared bots without admin or billing access.">
            <select className="select" value={selectedRole} aria-label="Role" onChange={() => undefined}>
              {(memberRoles.length ? memberRoles : [{ code: "tenant_user", name: "Tenant User" }]).map((r) => (
                <option key={r.code} value={r.code}>{r.name}</option>
              ))}
            </select>
          </Field>
        </div>
      </Modal>
    </>
  );
}
