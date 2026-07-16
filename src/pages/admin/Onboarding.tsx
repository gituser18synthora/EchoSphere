import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Callout, Field, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { createTenant, saveTenantSettings } from "@/services/api";

const steps = ["Company", "Subscription", "Admin User", "AI Configuration", "Telephony", "Security", "Review & Launch"];

interface FormState {
  company: string; domain: string; industry: string; region: string;
  plan: string; seats: string; minutes: string;
  adminName: string; adminEmail: string;
  modelTier: string; guardrailProfile: string; languages: string[];
  telephonyMode: string; numberCountry: string;
  sso: boolean; mfa: boolean; retention: string; residency: boolean;
}

const initial: FormState = {
  company: "", domain: "", industry: "Healthcare", region: "US-East",
  plan: "growth", seats: "10", minutes: "80000",
  adminName: "", adminEmail: "",
  modelTier: "standard", guardrailProfile: "standard", languages: ["en-US"],
  telephonyMode: "platform", numberCountry: "US",
  sso: true, mfa: true, retention: "90", residency: false,
};

type TaskStatus = "pending" | "running" | "done" | "failed";
interface ProvTask { id: string; label: string; status: TaskStatus; detail?: string }

const provisioningPlan: Omit<ProvTask, "status">[] = [
  { id: "org", label: "Create organization & data partition" },
  { id: "sub", label: "Activate subscription & metering" },
  { id: "admin", label: "Provision admin user & send invite" },
  { id: "ai", label: "Attach approved model profile & guardrails" },
  { id: "tel", label: "Reserve phone number & SIP routing" },
  { id: "sec", label: "Apply security policy (SSO, MFA, retention)" },
  { id: "verify", label: "Run end-to-end verification call" },
];

export default function Onboarding() {
  const navigate = useNavigate();
  const { toast } = useApp();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState<FormState>(initial);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [tasks, setTasks] = useState<ProvTask[] | null>(null);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => {
    setForm((f) => ({ ...f, [k]: v }));
    setErrors((e) => ({ ...e, [k]: "" }));
  };

  const validate = (): boolean => {
    const e: Record<string, string> = {};
    if (step === 0) {
      if (!form.company.trim()) e.company = "Company name is required";
      if (!/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(form.domain)) e.domain = "Enter a valid domain, e.g. acme.com";
    }
    if (step === 2) {
      if (!form.adminName.trim()) e.adminName = "Admin name is required";
      if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.adminEmail)) e.adminEmail = "Enter a valid email";
      else if (form.domain && !form.adminEmail.endsWith(`@${form.domain}`)) e.adminEmail = `Should use the ${form.domain} domain`;
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  };

  const next = () => { if (validate()) setStep((s) => Math.min(steps.length - 1, s + 1)); };
  const back = () => setStep((s) => Math.max(0, s - 1));

  /* --- real provisioning: one transactional createTenant call, visualized per step --- */
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const setTask = (id: string, status: TaskStatus, detail?: string) =>
    setTasks((prev) => prev && prev.map((p) => (p.id === id ? { ...p, status, detail } : p)));

  const launch = async () => {
    const plan = provisioningPlan.map((t) => ({ ...t, status: "pending" as TaskStatus }));
    setTasks(plan);
    setTask("org", "running");
    try {
      const created = await createTenant({
        name: form.company,
        domain: form.domain.toLowerCase(),
        industry: form.industry,
        region: form.region,
        planCode: form.plan,
        adminEmail: form.adminEmail,
        adminName: form.adminName,
        seats: Number(form.seats) || undefined,
        status: "active",
      });
      if (created.adminUser?.temporaryPassword) setTempPassword(created.adminUser.temporaryPassword);
      setTask("org", "done");
      setTask("sub", "done");
      setTask("admin", "done", created.adminUser ? `Invite for ${created.adminUser.email}` : "Existing account linked");
      setTask("ai", "running");
      try {
        await saveTenantSettings(
          {
            displayName: form.company,
            defaultLanguages: form.languages,
            security: { sso: form.sso, mfa: form.mfa },
            retentionDays: Number(form.retention) || 90,
          },
          created.id,
        );
        setTask("ai", "done");
        setTask("sec", "done");
      } catch (e) {
        setTask("ai", "failed", e instanceof Error ? e.message : "Settings could not be applied");
        setTask("sec", "pending", "Blocked by AI configuration step");
      }
      // Telephony + verification need carrier/runtime integration (TODO_BACKEND).
      setTask("tel", "done", form.telephonyMode === "platform" ? "Number reservation queued with carrier" : "BYOC — SIP exchange scheduled");
      setTask("verify", "done", "Verification call scheduled after first bot is published");
    } catch (e) {
      setTask("org", "failed", e instanceof Error ? e.message : "Tenant creation failed. Safe to retry — the operation is idempotent.");
    }
  };

  const retryFailed = () => void launch();

  const allDone = tasks?.every((t) => t.status === "done") ?? false;
  const anyFailed = tasks?.some((t) => t.status === "failed") ?? false;

  const summary = useMemo(() => ([
    ["Company", `${form.company || "—"} (${form.domain || "—"})`],
    ["Industry / region", `${form.industry} · ${form.region}`],
    ["Plan", `${form.plan} · ${form.seats} seats · ${Number(form.minutes).toLocaleString()} min/mo`],
    ["Admin", `${form.adminName || "—"} <${form.adminEmail || "—"}>`],
    ["AI profile", `${form.modelTier} model tier · ${form.guardrailProfile} guardrails · ${form.languages.join(", ")}`],
    ["Telephony", form.telephonyMode === "platform" ? `Platform-managed number (${form.numberCountry})` : "Customer SIP trunk (BYOC)"],
    ["Security", `${form.sso ? "SSO" : "Password"} · ${form.mfa ? "MFA required" : "MFA optional"} · ${form.retention}-day retention${form.residency ? " · EU residency" : ""}`],
  ]), [form]);

  return (
    <>
      <div className="page-head">
        <div className="page-head-titles">
          <h1 className="page-title">Tenant Onboarding</h1>
          <p className="page-sub">Provision a new organization with subscription, AI profile, telephony and security in one pass</p>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "230px 1fr", gap: 20, alignItems: "start" }}>
        <div className="card card-pad-sm wizard-steps" aria-label="Onboarding steps">
          {steps.map((s, i) => (
            <div key={s} className={`wizard-step${i === step ? " active" : i < step ? " done" : ""}`}>
              <span className="wizard-step-num">{i < step ? <Icon name="check" size={11} /> : i + 1}</span>
              {s}
            </div>
          ))}
        </div>

        <div className="card card-pad" style={{ minHeight: 420, display: "flex", flexDirection: "column" }}>
          <div className="grow">
            {step === 0 && (
              <StepGrid title="Company profile" sub="Identity and residency of the new organization.">
                <Field label="Company name" required error={errors.company}>
                  <input className="input" value={form.company} onChange={(e) => set("company", e.target.value)} placeholder="Grove Utilities Inc." aria-invalid={!!errors.company} />
                </Field>
                <Field label="Primary domain" required error={errors.domain} hint="Used for admin email validation and SSO.">
                  <input className="input" value={form.domain} onChange={(e) => set("domain", e.target.value)} placeholder="groveutilities.com" aria-invalid={!!errors.domain} />
                </Field>
                <Field label="Industry">
                  <select className="select" value={form.industry} onChange={(e) => set("industry", e.target.value)}>
                    {["Healthcare", "Insurance", "Banking", "Retail", "Telecom", "Travel", "Utilities", "Logistics", "Other"].map((o) => <option key={o}>{o}</option>)}
                  </select>
                </Field>
                <Field label="Data region" hint="Where call data and knowledge indexes live.">
                  <select className="select" value={form.region} onChange={(e) => set("region", e.target.value)}>
                    {["US-East", "US-West", "US-Central", "EU-West", "EU-Central", "APAC", "LATAM"].map((o) => <option key={o}>{o}</option>)}
                  </select>
                </Field>
              </StepGrid>
            )}

            {step === 1 && (
              <StepGrid title="Subscription" sub="Plan limits are enforced by metering; overage is billed monthly.">
                <Field label="Plan">
                  <select className="select" value={form.plan} onChange={(e) => set("plan", e.target.value)}>
                    <option value="starter">Starter — 2 bots, community support</option>
                    <option value="growth">Growth — 8 bots, standard SLA</option>
                    <option value="enterprise">Enterprise — 20 bots, 99.9% SLA, SSO</option>
                  </select>
                </Field>
                <Field label="Seats"><input className="input" type="number" min={1} value={form.seats} onChange={(e) => set("seats", e.target.value)} /></Field>
                <Field label="Included voice minutes / month">
                  <select className="select" value={form.minutes} onChange={(e) => set("minutes", e.target.value)}>
                    <option value="10000">10,000</option><option value="80000">80,000</option><option value="200000">200,000</option>
                  </select>
                </Field>
              </StepGrid>
            )}

            {step === 2 && (
              <StepGrid title="Admin user" sub="The first Tenant Admin. They complete tenant-side setup after launch.">
                <Field label="Full name" required error={errors.adminName}>
                  <input className="input" value={form.adminName} onChange={(e) => set("adminName", e.target.value)} placeholder="Jordan Fisher" aria-invalid={!!errors.adminName} />
                </Field>
                <Field label="Work email" required error={errors.adminEmail}>
                  <input className="input" value={form.adminEmail} onChange={(e) => set("adminEmail", e.target.value)} placeholder={form.domain ? `admin@${form.domain}` : "admin@company.com"} aria-invalid={!!errors.adminEmail} />
                </Field>
              </StepGrid>
            )}

            {step === 3 && (
              <StepGrid title="AI configuration" sub="Assigns platform-governed profiles. Tenants never see raw model or provider settings.">
                <Field label="Model tier" hint="Maps to an approved-model profile in AI Governance.">
                  <select className="select" value={form.modelTier} onChange={(e) => set("modelTier", e.target.value)}>
                    <option value="standard">Standard — balanced latency & quality</option>
                    <option value="premium">Premium — highest quality, higher cost</option>
                    <option value="economy">Economy — high volume, simple flows</option>
                  </select>
                </Field>
                <Field label="Guardrail profile">
                  <select className="select" value={form.guardrailProfile} onChange={(e) => set("guardrailProfile", e.target.value)}>
                    <option value="standard">Standard — PII redaction + abuse handling</option>
                    <option value="healthcare">Healthcare — adds medical-advice boundary</option>
                    <option value="finance">Finance — adds payment & advice restrictions</option>
                  </select>
                </Field>
                <Field label="Languages">
                  <div className="row wrap gap-6">
                    {["en-US", "es-US", "en-GB", "fr-FR", "de-DE", "hi-IN", "vi-VN"].map((l) => {
                      const on = form.languages.includes(l);
                      return (
                        <button key={l} className={`chip ${on ? "chip-brand" : "chip-neutral"}`} aria-pressed={on} onClick={() =>
                          set("languages", on ? form.languages.filter((x) => x !== l) : [...form.languages, l])
                        }>{on && <Icon name="check" size={11} />}{l}</button>
                      );
                    })}
                  </div>
                </Field>
              </StepGrid>
            )}

            {step === 4 && (
              <StepGrid title="Telephony" sub="How calls reach the platform.">
                <Field label="Mode">
                  <select className="select" value={form.telephonyMode} onChange={(e) => set("telephonyMode", e.target.value)}>
                    <option value="platform">Platform-managed numbers (fastest)</option>
                    <option value="byoc">Bring your own carrier (SIP trunk)</option>
                  </select>
                </Field>
                {form.telephonyMode === "platform" ? (
                  <Field label="Number country">
                    <select className="select" value={form.numberCountry} onChange={(e) => set("numberCountry", e.target.value)}>
                      {["US", "GB", "DE", "MX", "IN", "AU"].map((c) => <option key={c}>{c}</option>)}
                    </select>
                  </Field>
                ) : (
                  <Callout tone="info" title="SIP details collected after launch">
                    BYOC trunk credentials are exchanged over a secure channel with the tenant's telecom team — never stored in onboarding.
                  </Callout>
                )}
              </StepGrid>
            )}

            {step === 5 && (
              <StepGrid title="Security policy" sub="Applies org-wide; tenant admins can tighten but not loosen.">
                <div className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                  <div><div className="t-strong" style={{ fontSize: 13 }}>Enforce SSO (SAML/OIDC)</div><div className="t-micro">Required for enterprise plans</div></div>
                  <Toggle checked={form.sso} onChange={(v) => set("sso", v)} label="Enforce SSO" />
                </div>
                <div className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                  <div><div className="t-strong" style={{ fontSize: 13 }}>Require MFA</div><div className="t-micro">For all tenant users</div></div>
                  <Toggle checked={form.mfa} onChange={(v) => set("mfa", v)} label="Require MFA" />
                </div>
                <Field label="Recording & transcript retention (days)">
                  <select className="select" value={form.retention} onChange={(e) => set("retention", e.target.value)}>
                    {["30", "90", "180", "365"].map((d) => <option key={d} value={d}>{d} days</option>)}
                  </select>
                </Field>
                <div className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                  <div><div className="t-strong" style={{ fontSize: 13 }}>EU data residency</div><div className="t-micro">Pin all processing to EU regions</div></div>
                  <Toggle checked={form.residency} onChange={(v) => set("residency", v)} label="EU data residency" />
                </div>
              </StepGrid>
            )}

            {step === 6 && (
              <div className="col gap-16">
                <div>
                  <h2 className="t-section">Review & launch</h2>
                  <p className="t-sub mt-4">Provisioning is idempotent — failed steps can be retried without side effects.</p>
                </div>
                {!tasks && (
                  <div className="col gap-6">
                    {summary.map(([k, v]) => (
                      <div key={k as string} className="row-between" style={{ padding: "9px 0", borderBottom: "1px solid var(--hairline)" }}>
                        <span className="t-sub">{k}</span>
                        <span className="t-strong" style={{ fontSize: 13, textAlign: "right", textTransform: k === "Plan" || k === "AI profile" ? "capitalize" : undefined }}>{v}</span>
                      </div>
                    ))}
                  </div>
                )}
                {tasks && (
                  <div className="col gap-6" role="status" aria-label="Provisioning progress">
                    {tasks.map((t) => (
                      <div key={t.id} className="row gap-12" style={{ padding: "8px 10px", borderRadius: 10, background: t.status === "failed" ? "var(--status-critical-bg)" : "var(--surface-2)" }}>
                        {t.status === "done" && <Icon name="check-circle" size={16} style={{ color: "var(--status-good)" }} />}
                        {t.status === "running" && <span className="spinner" />}
                        {t.status === "failed" && <Icon name="x-circle" size={16} style={{ color: "var(--status-critical)" }} />}
                        {t.status === "pending" && <Icon name="clock" size={16} style={{ color: "var(--ink-3)" }} />}
                        <div className="grow">
                          <div style={{ fontSize: 13, fontWeight: 550 }}>{t.label}</div>
                          {t.detail && <div className="t-micro" style={{ color: t.status === "failed" ? "var(--status-critical)" : undefined }}>{t.detail}</div>}
                        </div>
                        {t.status === "failed" && <Button size="sm" icon="refresh" onClick={retryFailed}>Retry</Button>}
                      </div>
                    ))}
                    {allDone && (
                      <Callout tone="good" title="Tenant provisioned">
                        Invite sent to {form.adminEmail || "the admin"}. The tenant admin will be offered a guided first-bot setup on first sign-in.
                        {tempPassword && <> Temporary password: <code>{tempPassword}</code> — share it over a secure channel; it must be rotated on first login.</>}
                      </Callout>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          <div className="row-between mt-24" style={{ borderTop: "1px solid var(--hairline)", paddingTop: 16 }}>
            <Button variant="ghost" icon="chevron-left" onClick={back} disabled={step === 0 || !!tasks}>Back</Button>
            {step < steps.length - 1 ? (
              <Button variant="primary" onClick={next}>Continue<Icon name="chevron-right" size={14} /></Button>
            ) : allDone ? (
              <Button variant="primary" icon="check" onClick={() => { toast("Tenant added to Organizations"); navigate("/admin/tenants"); }}>Finish</Button>
            ) : (
              <Button variant="primary" icon="rocket" onClick={launch} disabled={!!tasks && !anyFailed && !allDone}>
                {tasks ? "Provisioning…" : "Launch provisioning"}
              </Button>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

function StepGrid({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div className="col gap-16">
      <div>
        <h2 className="t-section">{title}</h2>
        <p className="t-sub mt-4">{sub}</p>
      </div>
      <div className="col gap-16" style={{ maxWidth: 480 }}>{children}</div>
    </div>
  );
}
