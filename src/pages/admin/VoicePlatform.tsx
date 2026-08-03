import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAsync } from "@/hooks/useAsync";
import {
  listBots, listChannelsSummary, listPhoneNumbers, listSipTrunks, listTenants,
  setPhoneNumberActive, updatePhoneNumber,
} from "@/services/api";
import type { PhoneNumber } from "@/types/domain";
import { DataTable } from "@/components/DataTable";
import {
  Button, Health, StatusChip, Tabs, Callout, CardSkeleton, ConfirmModal,
  EmptyState, ErrorState, Field, Modal,
} from "@/components/ui";
import { fmtNum } from "@/components/charts";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const tabs = [
  { id: "bots", label: "VoiceBots" },
  { id: "numbers", label: "Phone Numbers" },
  { id: "sip", label: "SIP & Telephony" },
  { id: "channels", label: "Channels" },
];

export default function VoicePlatform() {
  const [tab, setTab] = useState("bots");
  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Voice Platform</h1>
          <p className="page-sub">Every bot, number and trunk on the platform</p>
        </div>
      </div>
      <Tabs tabs={tabs} active={tab} onChange={setTab} />
      <div className="mt-16">
        {tab === "bots" && <AllBots />}
        {tab === "numbers" && <Numbers />}
        {tab === "sip" && <Sip />}
        {tab === "channels" && <ChannelsSummary />}
      </div>
    </>
  );
}

function AllBots() {
  const botsQ = useAsync(listBots, []);
  const tenantsQ = useAsync(listTenants, []);
  const navigate = useNavigate();
  const tenantName = (id: string) => tenantsQ.data?.find((t) => t.id === id)?.name ?? id;
  return (
    <div className="card">
      <DataTable
        loading={botsQ.loading} error={botsQ.error} onRetry={botsQ.reload} rows={botsQ.data}
        onRowClick={(b) => navigate(`/admin/tenants/${b.tenantId}`)}
        empty={{ icon: "bot", title: "No bots on the platform" }}
        columns={[
          { key: "name", header: "Bot", sortValue: (b) => b.name, render: (b) => <div><div className="t-strong">{b.name}</div><div className="t-micro">{tenantName(b.tenantId)}</div></div> },
          { key: "status", header: "Status", sortValue: (b) => b.status, render: (b) => <StatusChip status={b.status} /> },
          { key: "health", header: "Health", sortValue: (b) => b.health, render: (b) => <Health level={b.health} /> },
          { key: "version", header: "Live", render: (b) => <code>{b.liveVersion ?? "—"}</code> },
          { key: "langs", header: "Languages", render: (b) => <span className="t-sub">{b.languages.join(", ")}</span> },
          { key: "calls", header: "Calls / mo", align: "right", sortValue: (b) => b.callsMonth, render: (b) => <span className="t-num">{fmtNum(b.callsMonth)}</span> },
          { key: "cost", header: "Cost / call", align: "right", sortValue: (b) => b.avgCostPerCall, render: (b) => <span className="t-num">{b.avgCostPerCall ? `$${b.avgCostPerCall.toFixed(3)}` : "—"}</span> },
        ]}
      />
    </div>
  );
}

function Numbers() {
  const q = useAsync(listPhoneNumbers, []);
  const { toast } = useApp();
  const [editing, setEditing] = useState<PhoneNumber | null>(null);
  const [confirmOff, setConfirmOff] = useState<PhoneNumber | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const toggleActive = async (n: PhoneNumber, active: boolean) => {
    setBusyId(n.id);
    try {
      await setPhoneNumberActive(n.id, active);
      toast(`${n.number} ${active ? "activated" : "deactivated"}`);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Updating the number failed", "error");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Phone number inventory</span>
        <Button size="sm" variant="primary" icon="plus" onClick={() => toast("Number purchase flow requires carrier API credentials (TODO_BACKEND)", "info")}>Buy number</Button>
      </div>
      <DataTable
        loading={q.loading} error={q.error} onRetry={q.reload} rows={q.data}
        empty={{ icon: "phone", title: "No numbers provisioned" }}
        columns={[
          { key: "number", header: "Number", sortValue: (n) => n.number, render: (n) => <code className="t-strong" style={{ fontSize: 12.5 }}>{n.number}</code> },
          { key: "country", header: "Country" },
          { key: "tenant", header: "Tenant", render: (n) => n.tenant ?? <span className="t-micro">Unassigned</span> },
          { key: "bot", header: "Bot", render: (n) => n.bot ?? "—" },
          { key: "provider", header: "Carrier" },
          { key: "status", header: "Status", sortValue: (n) => n.status, render: (n) => <StatusChip status={n.status} /> },
          {
            key: "active", header: "Active", sortValue: (n) => (n.isActive ? 0 : 1),
            render: (n) => <StatusChip status={n.isActive ? "active" : "inactive"} />,
          },
          { key: "cost", header: "Monthly", align: "right", sortValue: (n) => n.monthlyCost, render: (n) => <span className="t-num">${n.monthlyCost.toFixed(2)}</span> },
          {
            key: "actions", header: "", align: "right",
            render: (n) => (
              <div className="row gap-6" style={{ justifyContent: "flex-end" }} onClick={(e) => e.stopPropagation()}>
                <Button size="sm" icon="settings" onClick={() => setEditing(n)}>Edit</Button>
                {n.isActive ? (
                  <Button size="sm" variant="ghost" icon="pause" busy={busyId === n.id} onClick={() => setConfirmOff(n)}>Deactivate</Button>
                ) : (
                  <Button size="sm" variant="ghost" icon="play" busy={busyId === n.id} onClick={() => void toggleActive(n, true)}>Activate</Button>
                )}
              </div>
            ),
          },
        ]}
      />

      {editing && (
        <PhoneNumberEditModal
          number={editing}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); q.reload(); }}
        />
      )}

      {confirmOff && (
        <ConfirmModal
          open
          onClose={() => setConfirmOff(null)}
          danger
          title={`Deactivate ${confirmOff.number}?`}
          body={confirmOff.tenant
            ? `This number stays assigned to ${confirmOff.bot ?? confirmOff.tenant} and keeps its current routing, but it cannot be claimed for any NEW bot or channel assignment until reactivated.`
            : "An inactive number cannot be claimed for new bot or channel assignments until it is reactivated."}
          confirmLabel="Deactivate"
          onConfirm={() => {
            const n = confirmOff;
            setConfirmOff(null);
            void toggleActive(n, false);
          }}
        />
      )}
    </div>
  );
}

const E164_INPUT = /^\+[1-9]\d{6,14}$/;
const normPhone = (v: string) => v.replace(/[\s().-]/g, "");

function PhoneNumberEditModal({ number, onClose, onSaved }: {
  number: PhoneNumber; onClose: () => void; onSaved: () => void;
}) {
  const { toast } = useApp();
  const [form, setForm] = useState({
    number: number.number,
    country: number.country,
    provider: number.provider,
    monthlyCost: String(number.monthlyCost),
    status: number.status as string,
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [apiError, setApiError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const assigned = Boolean(number.tenant);
  const set = (key: string) => (v: string) => {
    setForm((f) => ({ ...f, [key]: v }));
    setErrors((e) => ({ ...e, [key]: "" }));
  };

  const save = async () => {
    const errs: Record<string, string> = {};
    if (!E164_INPUT.test(normPhone(form.number))) errs.number = "Enter an E.164 number, e.g. +14155550119.";
    if (form.country.trim().length > 5) errs.country = "Use a short ISO code, e.g. US or IN.";
    const cost = Number(form.monthlyCost);
    if (form.monthlyCost.trim() === "" || Number.isNaN(cost) || cost < 0) errs.monthlyCost = "Enter a non-negative amount.";
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setApiError(null);
    try {
      await updatePhoneNumber(number.id, {
        number: form.number.trim(),
        country: form.country.trim(),
        provider: form.provider.trim(),
        monthlyCost: cost,
        ...(assigned ? {} : { status: form.status }),
      });
      toast(`${form.number.trim()} updated`);
      onSaved();
    } catch (e) {
      setApiError(e instanceof Error ? e.message : "Saving the number failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={`Edit ${number.number}`}
      sub={assigned
        ? `Assigned to ${number.bot ? `${number.bot} · ` : ""}${number.tenant} — release or move it from the bot's Channels tab.`
        : "Unassigned number — bots claim it from their Channels tab."}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="check" busy={busy} onClick={save}>Save changes</Button>
        </>
      }
    >
      <div className="col gap-14">
        {apiError && <Callout tone="critical" title="Save failed">{apiError}</Callout>}
        <div className="grid grid-2">
          <Field label="Number" hint="E.164 — the number bots and carriers route by." error={errors.number || undefined}>
            <input className="input" value={form.number} disabled={assigned}
              placeholder="+14155550119" onChange={(e) => set("number")(e.target.value)} />
          </Field>
          <Field label="Country" error={errors.country || undefined}>
            <input className="input" value={form.country} placeholder="US" onChange={(e) => set("country")(e.target.value)} />
          </Field>
          <Field label="Carrier">
            <input className="input" value={form.provider} placeholder="Twilio" onChange={(e) => set("provider")(e.target.value)} />
          </Field>
          <Field label="Monthly cost (USD)" error={errors.monthlyCost || undefined}>
            <input className="input" value={form.monthlyCost} inputMode="decimal" onChange={(e) => set("monthlyCost")(e.target.value)} />
          </Field>
          {!assigned && (
            <Field label="Status">
              <select className="select" value={form.status} onChange={(e) => set("status")(e.target.value)}>
                {["available", "porting", "error"].map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </Field>
          )}
        </div>
      </div>
    </Modal>
  );
}

function Sip() {
  const q = useAsync(listSipTrunks, []);
  if (q.loading) return <div className="grid grid-3">{Array.from({ length: 3 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}</div>;
  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  const trunks = q.data ?? [];
  const degraded = trunks.filter((t) => t.status !== "healthy" || t.failurePct > 1);
  return (
    <>
      {degraded.length > 0 && (
        <Callout tone="critical" title={`Trunk degradation: ${degraded[0].name}`}>
          {degraded[0].name} shows {degraded[0].failurePct}% call setup failures in {degraded[0].region}. Failover to the backup trunk is armed.
        </Callout>
      )}
      <div className="grid grid-3 mt-16">
        {trunks.length === 0 && <EmptyState icon="phone" title="No SIP trunks configured" />}
        {trunks.map((t) => {
          const usage = t.capacityLines ? Math.round((t.activeCalls / t.capacityLines) * 100) : 0;
          const level = t.status === "healthy" ? "good" : t.failurePct > 5 ? "critical" : "warning";
          return (
            <div key={t.id} className="card card-pad col gap-12">
              <div className="row-between">
                <span className="t-strong" style={{ fontSize: 13.5 }}>{t.name}</span>
                <Health level={level} />
              </div>
              <div className="row gap-16">
                <div><div className="t-micro">Region</div><div className="t-strong">{t.region}</div></div>
                <div><div className="t-micro">Lines</div><div className="t-strong t-num">{t.capacityLines}</div></div>
                <div><div className="t-micro">Utilization</div><div className="t-strong t-num">{usage}%</div></div>
                <div><div className="t-micro">Failures</div><div className="t-strong t-num">{t.failurePct}%</div></div>
              </div>
              <div className="progress"><div className={`progress-fill ${usage > 80 ? "critical" : "good"}`} style={{ width: `${usage}%` }} /></div>
            </div>
          );
        })}
      </div>
    </>
  );
}

const channelMeta: Record<string, { name: string; icon: "phone" | "whatsapp" | "monitor" | "smartphone" | "message" }> = {
  voice: { name: "Voice (PSTN/SIP)", icon: "phone" },
  whatsapp: { name: "WhatsApp Business", icon: "whatsapp" },
  web: { name: "Web widget", icon: "monitor" },
  mobile: { name: "Mobile SDK", icon: "smartphone" },
  sms: { name: "SMS", icon: "message" },
};

function ChannelsSummary() {
  const q = useAsync(listChannelsSummary, []);
  if (q.loading) return <div className="grid grid-4">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={2} />)}</div>;
  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  return (
    <div className="grid grid-4">
      {q.data?.map((r) => {
        const meta = channelMeta[r.type] ?? { name: r.type, icon: "monitor" as const };
        return (
          <div key={r.type} className="card card-pad col gap-12">
            <div className="row gap-12">
              <span className="icon-tile brand"><Icon name={meta.icon} size={16} /></span>
              <span className="t-strong" style={{ fontSize: 13.5 }}>{meta.name}</span>
            </div>
            <div className="row gap-16">
              <div><div className="t-micro">Live</div><div className="t-strong t-num" style={{ color: "var(--status-good)" }}>{r.live}</div></div>
              <div><div className="t-micro">Testing</div><div className="t-strong t-num">{r.testing}</div></div>
              <div><div className="t-micro">Failed</div><div className="t-strong t-num" style={{ color: r.failed ? "var(--status-critical)" : undefined }}>{r.failed}</div></div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
