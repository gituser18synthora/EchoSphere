import { useState } from "react";
import { Button, Callout, Field, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { simulateAction } from "@/services/api";

export default function Settings() {
  const { toast } = useApp();
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notifPublish, setNotifPublish] = useState(true);
  const [notifEscalation, setNotifEscalation] = useState(true);
  const [notifDigest, setNotifDigest] = useState(false);

  const touch = () => setDirty(true);

  const save = async () => {
    setBusy(true);
    await simulateAction("settings");
    setBusy(false);
    setDirty(false);
    toast("Settings saved — changes to hours and holidays apply to routing immediately");
  };

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Settings</h1>
          <p className="page-sub">Organization-wide defaults that every bot inherits</p>
        </div>
        <div className="page-actions">
          <Button variant="primary" icon="check" busy={busy} disabled={!dirty} onClick={save}>
            {dirty ? "Save changes" : "Saved"}
          </Button>
        </div>
      </div>

      <div className="grid grid-2">
        <div className="col gap-16">
          <section className="card card-pad col gap-14">
            <span className="card-title">Organization</span>
            <Field label="Display name"><input className="input" defaultValue="Meridian Health Group" onChange={touch} /></Field>
            <Field label="Default timezone" hint="Business hours and holiday routing use this timezone.">
              <select className="select" defaultValue="America/New_York" onChange={touch}>
                {["America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "Europe/London", "Europe/Berlin"].map((t) => <option key={t}>{t}</option>)}
              </select>
            </Field>
            <Field label="Default languages">
              <div className="row wrap gap-6">
                {["en-US", "es-US", "vi-VN"].map((l) => (
                  <span key={l} className="chip chip-brand"><Icon name="check" size={11} />{l}</span>
                ))}
                <button className="chip chip-neutral" onClick={() => { touch(); toast("Language added — assign voices per bot in Studio", "info"); }}>
                  <Icon name="plus" size={11} /> Add
                </button>
              </div>
            </Field>
          </section>

          <section className="card card-pad col gap-14">
            <span className="card-title">Branding</span>
            <Field label="Bot display name suffix" hint="Shown in web/mobile widgets, e.g. “Ava — Meridian Health assistant”.">
              <input className="input" defaultValue="Meridian Health assistant" onChange={touch} />
            </Field>
            <Field label="Accent color">
              <div className="row gap-8">
                {["#6d55d9", "#2a78d6", "#1baf7a", "#eb6834"].map((c, i) => (
                  <button key={c} onClick={touch} aria-label={`Accent ${c}`}
                    style={{ width: 28, height: 28, borderRadius: 8, background: c, border: i === 0 ? "2px solid var(--ink)" : "2px solid transparent" }} />
                ))}
              </div>
            </Field>
          </section>
        </div>

        <div className="col gap-16">
          <section className="card card-pad col gap-14">
            <span className="card-title">Business hours</span>
            {["Monday – Friday", "Saturday", "Sunday"].map((d, i) => (
              <div key={d} className="row-between">
                <span className="t-sub t-strong">{d}</span>
                <div className="row gap-6">
                  <select className="select" style={{ width: 100 }} defaultValue={i === 2 ? "closed" : "8:00 AM"} onChange={touch}>
                    <option>7:00 AM</option><option>8:00 AM</option><option>9:00 AM</option><option value="closed">Closed</option>
                  </select>
                  <span className="t-micro">to</span>
                  <select className="select" style={{ width: 100 }} defaultValue={i === 0 ? "6:00 PM" : i === 1 ? "1:00 PM" : "closed"} onChange={touch}>
                    <option>1:00 PM</option><option>5:00 PM</option><option>6:00 PM</option><option value="closed">Closed</option>
                  </select>
                </div>
              </div>
            ))}
            <Callout tone="info">
              Outside these hours, bots use their after-hours flow. Handover nodes route to voicemail or callback when queues are closed.
            </Callout>
          </section>

          <section className="card card-pad col gap-14">
            <span className="card-title">Holidays</span>
            {[["Independence Day", "Jul 4, 2026"], ["Labor Day", "Sep 7, 2026"], ["Thanksgiving", "Nov 26, 2026"]].map(([n, d]) => (
              <div key={n} className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                <span className="row gap-8"><Icon name="calendar" size={14} style={{ color: "var(--ink-3)" }} /><span className="t-strong" style={{ fontSize: 13 }}>{n}</span></span>
                <span className="t-sub t-num">{d}</span>
              </div>
            ))}
            <Button size="sm" icon="plus" onClick={() => { touch(); toast("Holiday added to the routing calendar"); }}>Add holiday</Button>
          </section>

          <section className="card card-pad col gap-12">
            <span className="card-title">Notifications</span>
            {[
              { label: "Publish & rollback events", desc: "Email + Slack on every release action", val: notifPublish, set: setNotifPublish },
              { label: "Escalation spikes", desc: "Alert when escalations exceed 2× baseline", val: notifEscalation, set: setNotifEscalation },
              { label: "Weekly digest", desc: "Monday summary of calls, containment and cost", val: notifDigest, set: setNotifDigest },
            ].map((n) => (
              <div key={n.label} className="row-between">
                <div>
                  <div className="t-strong" style={{ fontSize: 13 }}>{n.label}</div>
                  <div className="t-micro">{n.desc}</div>
                </div>
                <Toggle checked={n.val} onChange={(v) => { n.set(v); touch(); }} label={n.label} />
              </div>
            ))}
          </section>
        </div>
      </div>
    </>
  );
}
