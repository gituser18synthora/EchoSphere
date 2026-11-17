import { useState } from "react";
import { DataTable } from "@/components/DataTable";
import { Button, Callout, Field, Modal, StatusChip } from "@/components/ui";
import { useAsync } from "@/hooks/useAsync";
import { useApp } from "@/state/AppContext";
import * as api from "@/services/api";
import type { PermissionInfo, RoleInfo } from "@/types/domain";

function RoleEditor({ role, catalog, onClose, onSaved }: {
  role: RoleInfo; catalog: PermissionInfo[]; onClose: () => void; onSaved: () => void;
}) {
  const [original, setOriginal] = useState(role.permissions);
  const [draft, setDraft] = useState(new Set(role.permissions));
  const [search, setSearch] = useState(""); const [review, setReview] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const readOnly = !["tenant_admin", "tenant_user"].includes(role.code);
  const added = [...draft].filter((p) => !original.includes(p));
  const removed = original.filter((p) => !draft.has(p));
  const label = (code: string) => catalog.find((p) => p.code === code)?.name ?? code;
  const choices = catalog.filter((p) => (role.scope === "platform" || p.category !== "platform")
    && `${p.name} ${p.description} ${p.code}`.toLowerCase().includes(search.toLowerCase()));
  const categories = [...new Set(choices.map((p) => p.category))].sort();
  const save = async () => {
    setBusy(true); setError("");
    try { await api.saveRolePermissions(role.id, [...draft].sort(), [...original].sort());
      window.dispatchEvent(new Event("echosphere:permissions-changed")); onSaved(); onClose();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not update permissions."); setConflict((e as { status?: number }).status === 409); }
    finally { setBusy(false); }
  };
  const reload = async () => {
    setBusy(true);
    try { const latest = (await api.listRoles()).find((r) => r.id === role.id); if (!latest) throw new Error("Role not found.");
      setOriginal(latest.permissions); setDraft(new Set(latest.permissions)); setConflict(false); setError(""); setReview(false);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not reload role."); } finally { setBusy(false); }
  };
  return <Modal open title={`${readOnly ? "View" : "Manage"} permissions: ${role.name}`} wide onClose={() => { if (!busy) onClose(); }}
    footer={<><Button disabled={busy} onClick={onClose}>{readOnly ? "Close" : "Cancel"}</Button>
      {!readOnly && review && <Button disabled={busy} onClick={() => setReview(false)}>Back</Button>}
      {!readOnly && <Button variant="primary" disabled={!added.length && !removed.length} busy={busy} onClick={() => review ? void save() : setReview(true)}>{review ? "Confirm changes" : "Review changes"}</Button>}</>}>
    <div className="col gap-12"><p>{role.description}</p>
      <Callout tone={readOnly ? "info" : "warning"}>{readOnly ? "This system role is protected." : `Changes affect all ${role.members} members assigned this role across all organizations, and future members. Some administration actions also require the Tenant Admin role.`}</Callout>
      {error && <div role="alert"><Callout tone="critical">{error}</Callout>{conflict && <Button onClick={() => void reload()} disabled={busy}>Reload latest permissions</Button>}</div>}
      {review ? <><p>Review the changes before applying them:</p><strong>Added ({added.length})</strong><ul>{added.map((p) => <li key={p}>{label(p)}</li>)}</ul>
        <strong>Removed ({removed.length})</strong><ul>{removed.map((p) => <li key={p}>{label(p)}</li>)}</ul></> : <>
        <Field label="Search permissions"><input className="input" placeholder="Permission name or description" value={search} onChange={(e) => setSearch(e.target.value)} /></Field>
        {categories.map((category) => <fieldset key={category} disabled={busy || readOnly} className="card card-pad col gap-12">
          <legend className="t-strong">{category === "tenant" ? "Organization capabilities" : category === "platform" ? "Platform capabilities" : category}</legend>
          {choices.filter((p) => p.category === category).map((p) => <label key={p.code} className="row gap-12" style={{ alignItems: "flex-start" }}>
            <input type="checkbox" checked={draft.has(p.code)} onChange={() => setDraft((current) => { const next = new Set(current); if (next.has(p.code)) next.delete(p.code); else next.add(p.code); return next; })} />
            <span><span className="t-strong">{p.name}</span><span className="field-hint" style={{ display: "block" }}>{p.description || p.code.replaceAll("_", " ").replaceAll(".", " ")}</span></span>
          </label>)}
        </fieldset>)}
        {!choices.length && <p>No matching permissions.</p>}
      </>}
    </div>
  </Modal>;
}

export default function RolesPanel() {
  const { toast } = useApp();
  const q = useAsync(() => Promise.all([api.listRoles(), api.listPermissions()]), []);
  const [selected, setSelected] = useState<RoleInfo | null>(null);
  return <div className="col gap-12"><div className="row-between gap-12"><p className="t-sub">Inspect role capabilities and control access. Existing role definitions apply across organizations.</p><Button onClick={q.reload} disabled={q.loading}>Refresh</Button></div>
    <div className="card"><DataTable<RoleInfo> rows={q.data?.[0] ?? null} loading={q.loading} error={q.error} onRetry={q.reload} empty={{ title: "No roles" }} columns={[
      { key: "name", header: "Role", render: (r) => <div className="t-strong">{r.name}</div> },
      { key: "scope", header: "Scope", render: (r) => <StatusChip status="info" label={r.scope === "platform" ? "Platform" : "Organization"} /> },
      { key: "description", header: "Description" }, { key: "permissionCount", header: "Permissions" }, { key: "members", header: "Members" },
      { key: "actions", header: "Actions", render: (r) => <Button onClick={() => setSelected(r)}>{r.code === "super_admin" ? "View permissions" : "Manage permissions"}</Button> },
    ]} /></div>
    {selected && q.data && <RoleEditor role={selected} catalog={q.data[1]} onClose={() => setSelected(null)} onSaved={() => { q.reload(); toast("Role permissions updated."); }} />}
  </div>;
}
