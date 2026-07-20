import { useEffect, useState, type ReactNode } from "react";
import { useAsync } from "@/hooks/useAsync";
import { getTenantProfile, getTenantSettings, listLanguages, saveTenantProfile, saveTenantSettings } from "@/services/api";
import type { TenantProfile, TenantSettings } from "@/types/domain";
import { Button, Callout, CardSkeleton, ErrorState, Field, Tabs, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

const TIMEZONES = ["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London", "Europe/Berlin"];
const ACCENTS = ["#6d55d9", "#2a78d6", "#1baf7a", "#eb6834"];
const OPEN_TIMES = ["7:00 AM", "8:00 AM", "9:00 AM", "10:00 AM"];
const CLOSE_TIMES = ["1:00 PM", "5:00 PM", "6:00 PM", "8:00 PM"];

const withOption = (opts: string[], value: string) => (value && !opts.includes(value) ? [value, ...opts] : opts);

const fmtHolidayDate = (d: string) => {
  const t = new Date(d);
  return Number.isNaN(t.getTime()) ? d : t.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
};

export default function Settings() {
  const { toast } = useApp();
  const [tab, setTab] = useState("profile");
  const q = useAsync(getTenantSettings, []);
  const langsQ = useAsync(listLanguages, []);
  const [form, setForm] = useState<TenantSettings | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [newHoliday, setNewHoliday] = useState<{ name: string; date: string } | null>(null);

  useEffect(() => {
    if (q.data) {
      setForm(q.data);
      setDirty(false);
    }
  }, [q.data]);

  const patch = (p: Partial<TenantSettings>) => {
    setForm((f) => (f ? { ...f, ...p } : f));
    setDirty(true);
  };

  const save = async () => {
    if (!form) return;
    setBusy(true);
    try {
      const saved = await saveTenantSettings(form);
      setForm(saved);
      setDirty(false);
      toast("Settings saved — changes to hours and holidays apply to routing immediately");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Failed to save settings", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Settings</h1>
          <p className="page-sub">Organization-wide defaults that every bot inherits</p>
        </div>
        {tab === "workspace" && (
          <div className="page-actions">
            <Button variant="primary" icon="check" busy={busy} disabled={!dirty} onClick={save}>
              {dirty ? "Save changes" : "Saved"}
            </Button>
          </div>
        )}
      </div>

      <Tabs
        tabs={[{ id: "profile", label: "Organization profile" }, { id: "workspace", label: "Workspace settings" }]}
        active={tab}
        onChange={setTab}
      />

      <div className="mt-16">
        {tab === "profile" && <OrganizationProfileTab />}
        {tab === "workspace" && (q.error ? <ErrorState message={q.error} onRetry={q.reload} /> : q.loading || !form ? (
        <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={5} />)}</div>
      ) : (
        <div className="grid grid-2">
          <div className="col gap-16">
            <section className="card card-pad col gap-14">
              <span className="card-title">Organization</span>
              <Field label="Display name">
                <input className="input" value={form.displayName ?? ""} onChange={(e) => patch({ displayName: e.target.value })} />
              </Field>
              <Field label="Default timezone" hint="Business hours and holiday routing use this timezone.">
                <select className="select" value={form.timezone} onChange={(e) => patch({ timezone: e.target.value })}>
                  {withOption(TIMEZONES, form.timezone).map((t) => <option key={t}>{t}</option>)}
                </select>
              </Field>
              <Field label="Default languages">
                <div className="row wrap gap-6">
                  {form.defaultLanguages.map((l) => (
                    <button key={l} className="chip chip-brand" title="Remove language"
                      onClick={() => patch({ defaultLanguages: form.defaultLanguages.filter((x) => x !== l) })}>
                      <Icon name="check" size={11} />{l}
                    </button>
                  ))}
                  {(langsQ.data ?? []).filter((l) => l.enabled && !form.defaultLanguages.includes(l.code)).map((l) => (
                    <button key={l.code} className="chip chip-neutral" title={l.name}
                      onClick={() => patch({ defaultLanguages: [...form.defaultLanguages, l.code] })}>
                      <Icon name="plus" size={11} />{l.code}
                    </button>
                  ))}
                </div>
              </Field>
            </section>

            <section className="card card-pad col gap-14">
              <span className="card-title">Branding</span>
              <Field label="Bot display name suffix" hint="Shown in web/mobile widgets, e.g. “Ava — your organization's assistant”.">
                <input className="input" value={form.branding.assistantName ?? ""} onChange={(e) => patch({ branding: { ...form.branding, assistantName: e.target.value } })} />
              </Field>
              <Field label="Accent color">
                <div className="row gap-8">
                  {withOption(ACCENTS, form.branding.accent ?? "").map((c) => (
                    <button key={c} onClick={() => patch({ branding: { ...form.branding, accent: c } })} aria-label={`Accent ${c}`}
                      style={{ width: 28, height: 28, borderRadius: 8, background: c, border: form.branding.accent === c ? "2px solid var(--ink)" : "2px solid transparent" }} />
                  ))}
                </div>
              </Field>
            </section>
          </div>

          <div className="col gap-16">
            <section className="card card-pad col gap-14">
              <span className="card-title">Business hours</span>
              {Object.entries(form.businessHours).map(([day, h]) => (
                <div key={day} className="row-between">
                  <span className="t-sub t-strong" style={{ textTransform: "capitalize" }}>{day.replace(/[_-]/g, " ")}</span>
                  <div className="row gap-6">
                    <select className="select" style={{ width: 100 }} value={h.closed ? "closed" : h.open}
                      onChange={(e) => patch({ businessHours: { ...form.businessHours, [day]: e.target.value === "closed" ? { ...h, closed: true } : { ...h, open: e.target.value, closed: false } } })}>
                      {withOption(OPEN_TIMES, h.open).map((t) => <option key={t} value={t}>{t}</option>)}
                      <option value="closed">Closed</option>
                    </select>
                    <span className="t-micro">to</span>
                    <select className="select" style={{ width: 100 }} value={h.closed ? "closed" : h.close}
                      onChange={(e) => patch({ businessHours: { ...form.businessHours, [day]: e.target.value === "closed" ? { ...h, closed: true } : { ...h, close: e.target.value, closed: false } } })}>
                      {withOption(CLOSE_TIMES, h.close).map((t) => <option key={t} value={t}>{t}</option>)}
                      <option value="closed">Closed</option>
                    </select>
                  </div>
                </div>
              ))}
              {Object.keys(form.businessHours).length === 0 && <p className="t-micro">No business hours configured yet.</p>}
              <Callout tone="info">
                Outside these hours, bots use their after-hours flow. Handover nodes route to voicemail or callback when queues are closed.
              </Callout>
            </section>

            <section className="card card-pad col gap-14">
              <span className="card-title">Holidays</span>
              {form.holidays.map((h) => (
                <div key={`${h.name}-${h.date}`} className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                  <span className="row gap-8"><Icon name="calendar" size={14} style={{ color: "var(--ink-3)" }} /><span className="t-strong" style={{ fontSize: 13 }}>{h.name}</span></span>
                  <span className="row gap-8">
                    <span className="t-sub t-num">{fmtHolidayDate(h.date)}</span>
                    <button className="btn-icon" aria-label={`Remove ${h.name}`} onClick={() => patch({ holidays: form.holidays.filter((x) => x !== h) })}>
                      <Icon name="x" size={12} />
                    </button>
                  </span>
                </div>
              ))}
              {form.holidays.length === 0 && !newHoliday && <p className="t-micro">No holidays on the routing calendar yet.</p>}
              {newHoliday ? (
                <div className="row gap-6">
                  <input className="input" placeholder="Holiday name" value={newHoliday.name} autoFocus onChange={(e) => setNewHoliday({ ...newHoliday, name: e.target.value })} />
                  <input className="input" type="date" style={{ width: 150 }} value={newHoliday.date} onChange={(e) => setNewHoliday({ ...newHoliday, date: e.target.value })} />
                  <Button size="sm" icon="check" disabled={!newHoliday.name.trim() || !newHoliday.date}
                    onClick={() => { patch({ holidays: [...form.holidays, { name: newHoliday.name.trim(), date: newHoliday.date }] }); setNewHoliday(null); }}>
                    Add
                  </Button>
                </div>
              ) : (
                <Button size="sm" icon="plus" onClick={() => setNewHoliday({ name: "", date: "" })}>Add holiday</Button>
              )}
            </section>

            <section className="card card-pad col gap-12">
              <span className="card-title">Notifications</span>
              {form.notifications.map((n) => (
                <div key={n.id} className="row-between">
                  <div className="t-strong" style={{ fontSize: 13 }}>{n.label}</div>
                  <Toggle checked={n.enabled} label={n.label}
                    onChange={(v) => patch({ notifications: form.notifications.map((x) => (x.id === n.id ? { ...x, enabled: v } : x)) })} />
                </div>
              ))}
              {form.notifications.length === 0 && <p className="t-micro">No notification rules configured yet.</p>}
            </section>
          </div>
        </div>
      ))}
      </div>
    </>
  );
}

/* ---------- Organization profile (tenant-facing subset of the platform tenant record) ---------- */

const PROFILE_FIELDS = [
  "displayName", "website", "contactName", "contactEmail", "contactPhone",
  "address", "country", "timezone", "supportEmail", "supportPhone",
] as const;
type ProfileForm = Pick<TenantProfile, (typeof PROFILE_FIELDS)[number]>;

const pickProfileForm = (p: TenantProfile): ProfileForm =>
  PROFILE_FIELDS.reduce((acc, k) => ({ ...acc, [k]: p[k] ?? "" }), {} as ProfileForm);

function OrganizationProfileTab() {
  const { toast, hasPermission } = useApp();
  const canEdit = hasPermission("edit_tenant_profile");
  const q = useAsync(() => getTenantProfile(), []);
  const [form, setForm] = useState<ProfileForm | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (q.data) {
      setForm(pickProfileForm(q.data));
      setDirty(false);
    }
  }, [q.data]);

  /* Warn on unsaved changes when leaving the page. */
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const patch = (p: Partial<ProfileForm>) => {
    if (!canEdit) return;
    setForm((f) => (f ? { ...f, ...p } : f));
    setDirty(true);
  };

  const save = async () => {
    if (!form || !canEdit) return;
    setBusy(true);
    setErr(null);
    try {
      const saved = await saveTenantProfile(form);
      setForm(pickProfileForm(saved));
      setDirty(false);
      toast("Organization profile saved");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Failed to save organization profile");
    } finally {
      setBusy(false);
    }
  };

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading || !form || !q.data) {
    return <div className="grid grid-2"><CardSkeleton rows={7} /><CardSkeleton rows={5} /></div>;
  }
  const p = q.data;

  const managed: [string, ReactNode][] = [
    ["Plan", p.planName],
    ["Subscription status", p.subscriptionStatus],
    ["Data region", (
      <span>
        {p.dataRegionName}
        {!p.dataRegionInfrastructureReady && (
          <span className="t-micro" style={{ display: "block" }}>Configured operational region — not an infrastructure guarantee</span>
        )}
      </span>
    )],
    ["Tenant code", p.code],
    ["Tenant status", p.status],
  ];

  return (
    <div className="grid grid-2">
      <section className="card card-pad col gap-14">
        <span className="card-title">Organization profile</span>
        {!canEdit && <Callout tone="info">You need the edit permission to change the organization profile.</Callout>}
        {err && <Callout tone="critical" title="Save failed">{err}</Callout>}
        <Field label="Display name">
          <input className="input" value={form.displayName} disabled={!canEdit} onChange={(e) => patch({ displayName: e.target.value })} />
        </Field>
        <Field label="Website">
          <input className="input" value={form.website} disabled={!canEdit} placeholder="https://example.com" onChange={(e) => patch({ website: e.target.value })} />
        </Field>
        <Field label="Primary contact name">
          <input className="input" value={form.contactName} disabled={!canEdit} onChange={(e) => patch({ contactName: e.target.value })} />
        </Field>
        <Field label="Primary contact email">
          <input className="input" type="email" value={form.contactEmail} disabled={!canEdit} onChange={(e) => patch({ contactEmail: e.target.value })} />
        </Field>
        <Field label="Primary contact phone">
          <input className="input" value={form.contactPhone} disabled={!canEdit} onChange={(e) => patch({ contactPhone: e.target.value })} />
        </Field>
        <Field label="Business address">
          <textarea className="textarea" rows={3} value={form.address} disabled={!canEdit} onChange={(e) => patch({ address: e.target.value })} />
        </Field>
        <Field label="Country">
          <input className="input" value={form.country} disabled={!canEdit} onChange={(e) => patch({ country: e.target.value })} />
        </Field>
        <Field label="Time zone">
          <select className="select" value={form.timezone} disabled={!canEdit} onChange={(e) => patch({ timezone: e.target.value })}>
            {withOption(TIMEZONES, form.timezone).map((t) => <option key={t}>{t}</option>)}
          </select>
        </Field>
        <Field label="Support email">
          <input className="input" type="email" value={form.supportEmail} disabled={!canEdit} onChange={(e) => patch({ supportEmail: e.target.value })} />
        </Field>
        <Field label="Support phone">
          <input className="input" value={form.supportPhone} disabled={!canEdit} onChange={(e) => patch({ supportPhone: e.target.value })} />
        </Field>
        {canEdit && (
          <div className="row" style={{ justifyContent: "flex-end" }}>
            <Button variant="primary" icon="check" busy={busy} disabled={!dirty} onClick={save}>
              {dirty ? "Save profile" : "Saved"}
            </Button>
          </div>
        )}
      </section>

      <section className="card card-pad col gap-12" style={{ alignSelf: "start" }}>
        <span className="card-title">Managed by platform administrator</span>
        <p className="t-micro">These values are controlled by the platform team and are read-only here.</p>
        {managed.map(([k, v], i) => (
          <div className="row-between" key={k} style={{ alignItems: "flex-start", borderBottom: i < managed.length - 1 ? "1px solid var(--hairline)" : "none", paddingBottom: i < managed.length - 1 ? 10 : 0 }}>
            <span className="t-sub">{k}</span>
            <span className="t-strong" style={{ fontSize: 13, textAlign: "right", maxWidth: "60%" }}>{v}</span>
          </div>
        ))}
      </section>
    </div>
  );
}
