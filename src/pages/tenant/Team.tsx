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
  const { user, toast } = useApp();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [roleCode, setRoleCode] = useState("");
  const [nameErr, setNameErr] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const tenantRoles = (rolesQ.data ?? []).filter((r) => r.scope === "tenant");
  const selectedRole = roleCode || tenantRoles[0]?.code || "";

  const invite = async () => {
    if (!name.trim()) { setNameErr("Enter the member's name"); return; }
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { setErr("Enter a valid work email"); return; }
    setBusy(true);
    try {
      const created = await inviteUser({ name: name.trim(), email, roleCode: selectedRole });
      toast(created.temporaryPassword
        ? `Invite created — temporary password: ${created.temporaryPassword}`
        : `Invite sent to ${email}`);
      q.reload();
      setInviteOpen(false);
      setName("");
      setEmail("");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Invite failed", "error");
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
          <Button variant="primary" icon="plus" onClick={() => setInviteOpen(true)}>Invite member</Button>
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
              render: (m) => (
                <MenuButton actions={[
                  { label: "Change role", icon: "shield", onClick: () => toast(`Role editor opened for ${m.name}`, "info") },
                  { label: "Transfer bot ownership", icon: "bot", onClick: () => toast("Ownership transfer requires the receiving member to accept", "info") },
                  "sep",
                  m.status === "invited"
                    ? { label: "Resend invite", icon: "mail", onClick: () => toast(`Invite resent to ${m.email}`) }
                    : { label: "Deactivate", icon: "x-circle", danger: true, onClick: () => toast(`${m.name} deactivated — owned bots need reassignment`, "info") },
                ]} />
              ),
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
        open={inviteOpen} onClose={() => setInviteOpen(false)}
        title="Invite a team member" sub={`They'll receive an email to join ${user?.tenantName ?? "your organization"} on EchoSphere.`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setInviteOpen(false)}>Cancel</Button>
            <Button variant="primary" icon="send" busy={busy} onClick={invite}>Send invite</Button>
          </>
        }
      >
        <div className="col gap-16">
          <Field label="Name" required error={nameErr}>
            <input className="input" value={name} autoFocus onChange={(e) => { setName(e.target.value); setNameErr(""); }} placeholder="Full name" aria-invalid={!!nameErr} />
          </Field>
          <Field label="Work email" required error={err}>
            <input className="input" value={email} onChange={(e) => { setEmail(e.target.value); setErr(""); }} placeholder="colleague@company.com" aria-invalid={!!err} />
          </Field>
          <Field label="Role" hint="Roles gate publishing, approvals and settings. You can change this later.">
            <select className="select" value={selectedRole} onChange={(e) => setRoleCode(e.target.value)}>
              {tenantRoles.map((r) => <option key={r.code} value={r.code}>{r.name}</option>)}
            </select>
          </Field>
        </div>
      </Modal>
    </>
  );
}
