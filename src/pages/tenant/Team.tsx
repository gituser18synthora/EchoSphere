import { useState } from "react";
import { useAsync } from "@/hooks/useAsync";
import { listTeam, simulateAction } from "@/services/api";
import { Avatar, Button, Field, MenuButton, Modal, StatusChip } from "@/components/ui";
import { DataTable } from "@/components/DataTable";
import { useApp } from "@/state/AppContext";

const roles = ["Tenant Admin", "Bot Manager", "Content Editor", "QA Reviewer", "Analyst (read-only)"];

export default function Team() {
  const q = useAsync(listTeam, []);
  const { toast } = useApp();
  const [inviteOpen, setInviteOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState(roles[1]);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const invite = async () => {
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { setErr("Enter a valid work email"); return; }
    setBusy(true);
    await simulateAction("invite");
    setBusy(false);
    toast(`Invite sent to ${email} as ${role}`);
    setInviteOpen(false);
    setEmail("");
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
        <div className="table-wrap mt-12">
          <table className="table">
            <thead>
              <tr><th>Capability</th>{roles.map((r) => <th key={r}>{r}</th>)}</tr>
            </thead>
            <tbody>
              {[
                ["Edit bots & knowledge", [1, 1, 1, 0, 0]],
                ["Approve prompts & releases", [1, 0, 0, 0, 0]],
                ["Publish / roll back", [1, 1, 0, 0, 0]],
                ["Review conversations", [1, 1, 1, 1, 1]],
                ["QA scorecard overrides", [1, 0, 0, 1, 0]],
                ["Manage team & settings", [1, 0, 0, 0, 0]],
              ].map(([cap, flags]) => (
                <tr key={cap as string}>
                  <td className="t-sub">{cap}</td>
                  {(flags as number[]).map((f, i) => (
                    <td key={i}>{f ? <span style={{ color: "var(--status-good)", fontWeight: 700 }}>✓</span> : <span className="t-micro">—</span>}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <Modal
        open={inviteOpen} onClose={() => setInviteOpen(false)}
        title="Invite a team member" sub="They'll receive an email to join Meridian Health Group on EchoSphere."
        footer={
          <>
            <Button variant="ghost" onClick={() => setInviteOpen(false)}>Cancel</Button>
            <Button variant="primary" icon="send" busy={busy} onClick={invite}>Send invite</Button>
          </>
        }
      >
        <div className="col gap-16">
          <Field label="Work email" required error={err}>
            <input className="input" value={email} autoFocus onChange={(e) => { setEmail(e.target.value); setErr(""); }} placeholder="colleague@meridianhealth.com" aria-invalid={!!err} />
          </Field>
          <Field label="Role" hint="Roles gate publishing, approvals and settings. You can change this later.">
            <select className="select" value={role} onChange={(e) => setRole(e.target.value)}>
              {roles.map((r) => <option key={r}>{r}</option>)}
            </select>
          </Field>
        </div>
      </Modal>
    </>
  );
}
