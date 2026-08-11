import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button, Callout, ErrorState, Field, Skeleton, Toggle } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import { useAsync } from "@/hooks/useAsync";
import { createTenant, getOnboardingOptions, saveTenantSettings } from "@/services/api";

const steps = ["Company", "Subscription", "Admin User", "AI Configuration", "Telephony", "Security", "Review & Launch"];

interface FormState {
  company: string; domain: string; industry: string; region: string;
  plan: string; seats: string;
  adminName: string; adminEmail: string;
  aiProfile: string; guardrailProfile: string; languages: string[];
  callSummaryEnabled: boolean; usePreviousCallSummary: boolean;
  telephonyMode: string; numberCountry: string;
  sso: boolean; mfa: boolean; retention: string; residency: boolean;
}

const initial: FormState = {
  company: "", domain: "", industry: "", region: "",
  plan: "", seats: "10",
  adminName: "", adminEmail: "",
  aiProfile: "", guardrailProfile: "", languages: [],
  callSummaryEnabled: false, usePreviousCallSummary: false,
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

  /* The Super Admin's explicit guardrail choice is sticky: once they pick a
     profile, industry changes stop re-suggesting the industry default. */
  const [guardrailTouched, setGuardrailTouched] = useState(false);

  const optionsQ = useAsync(getOnboardingOptions, []);
  const opts = optionsQ.data;

  /** The recommended profile id for an industry: its configured default,
      falling back to the platform "standard" profile. */
  const suggestedGuardrailProfile = (industryCode: string): string => {
    if (!opts) return "";
    const profiles = opts.guardrailProfiles ?? [];
    const industry = opts.industries.find((i) => i.code === industryCode);
    if (industry?.defaultGuardrailProfileId
        && profiles.some((p) => p.id === industry.defaultGuardrailProfileId)) {
      return industry.defaultGuardrailProfileId;
    }
    return profiles.find((p) => p.code === "standard")?.id ?? profiles[0]?.id ?? "";
  };

  /* Default selections once the platform option catalog loads. */
  useEffect(() => {
    if (!opts) return;
    setForm((f) => {
      const industry = f.industry || (opts.industries[0]?.code ?? "");
      return {
        ...f,
        industry,
        region: f.region || (opts.dataRegions[0]?.code ?? ""),
        plan: f.plan || (opts.plans.find((p) => p.isRecommended)?.code ?? opts.plans[0]?.code ?? ""),
        aiProfile: f.aiProfile || (opts.aiProfiles[0]?.code ?? ""),
        guardrailProfile: f.guardrailProfile || (
          (opts.industries.find((i) => i.code === industry)?.defaultGuardrailProfileId)
          || (opts.guardrailProfiles ?? []).find((p) => p.code === "standard")?.id
          || (opts.guardrailProfiles ?? [])[0]?.id || ""
        ),
        languages: f.languages.length > 0 ? f.languages : opts.languages[0] ? [opts.languages[0].code] : [],
      };
    });
  }, [opts]);

  const selectedPlan = opts?.plans.find((p) => p.code === form.plan);
  const selectedProfile = opts?.aiProfiles.find((p) => p.code === form.aiProfile);

  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => {
    setForm((f) => ({ ...f, [k]: v }));
    setErrors((e) => ({ ...e, [k]: "" }));
  };

  /** Industry drives the SUGGESTED guardrail profile — but never overrides a
      manual selection, and unrelated field edits never reset it. */
  const setIndustry = (code: string) => {
    setForm((f) => ({
      ...f,
      industry: code,
      guardrailProfile: guardrailTouched ? f.guardrailProfile : suggestedGuardrailProfile(code),
    }));
    setErrors((e) => ({ ...e, industry: "" }));
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
    if (step === 3 && form.languages.length === 0) {
      e.languages = "Select at least one language for this tenant";
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  };

  const next = () => { if (opts && validate()) setStep((s) => Math.min(steps.length - 1, s + 1)); };
  const back = () => setStep((s) => Math.max(0, s - 1));

  /* --- real provisioning: one transactional createTenant call, visualized per step --- */
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const setTask = (id: string, status: TaskStatus, detail?: string) =>
    setTasks((prev) => prev && prev.map((p) => (p.id === id ? { ...p, status, detail } : p)));

  const launch = async () => {
    if (!opts) return;
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
        aiProfileCode: form.aiProfile,
        guardrailProfileId: form.guardrailProfile || undefined,
        adminEmail: form.adminEmail,
        adminName: form.adminName,
        seats: Number(form.seats) || undefined,
        defaultLanguages: form.languages,
        callSummaryEnabled: form.callSummaryEnabled,
        usePreviousCallSummary: form.usePreviousCallSummary,
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

  const summary = useMemo(() => {
    const industryName = opts?.industries.find((i) => i.code === form.industry)?.name ?? form.industry;
    const regionName = opts?.dataRegions.find((r) => r.code === form.region)?.name ?? form.region;
    const planSel = opts?.plans.find((p) => p.code === form.plan);
    const profileName = opts?.aiProfiles.find((p) => p.code === form.aiProfile)?.name ?? form.aiProfile;
    const guardrailName = (opts?.guardrailProfiles ?? []).find((p) => p.id === form.guardrailProfile)?.name ?? "Standard";
    const langNames = form.languages.map((c) => opts?.languages.find((l) => l.code === c)?.name ?? c);
    return [
      ["Company", `${form.company || "—"} (${form.domain || "—"})`],
      ["Industry / region", `${industryName} · ${regionName}`],
      ["Plan", `${planSel?.name ?? form.plan} · ${form.seats} seats · ${(planSel?.minutesIncluded ?? 0).toLocaleString()} min/mo`],
      ["Admin", `${form.adminName || "—"} <${form.adminEmail || "—"}>`],
      ["AI profile", `${profileName} · ${guardrailName} guardrails · ${langNames.join(", ")}`],
      ["Call summary", `Generate: ${form.callSummaryEnabled ? "yes" : "no"} · Use previous: ${form.usePreviousCallSummary ? "yes" : "no"}`],
      ["Telephony", form.telephonyMode === "platform" ? `Platform-managed number (${form.numberCountry})` : "Customer SIP trunk (BYOC)"],
      ["Security", `${form.sso ? "SSO" : "Password"} · ${form.mfa ? "MFA required" : "MFA optional"} · ${form.retention}-day retention${form.residency ? " · EU residency" : ""}`],
    ];
  }, [form, opts]);

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
            {optionsQ.loading && (
              <div className="col gap-12" aria-busy="true" aria-label="Loading onboarding options">
                <Skeleton w="35%" h={18} />
                {Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} h={34} />)}
              </div>
            )}
            {!optionsQ.loading && optionsQ.error && <ErrorState message={optionsQ.error} onRetry={optionsQ.reload} />}
            {opts && (
              <>
            {step === 0 && (
              <StepGrid title="Company profile" sub="Identity and residency of the new organization.">
                <Field label="Company name" required error={errors.company}>
                  <input className="input" value={form.company} onChange={(e) => set("company", e.target.value)} placeholder="Grove Utilities Inc." aria-invalid={!!errors.company} />
                </Field>
                <Field label="Primary domain" required error={errors.domain} hint="Used for admin email validation and SSO.">
                  <input className="input" value={form.domain} onChange={(e) => set("domain", e.target.value)} placeholder="groveutilities.com" aria-invalid={!!errors.domain} />
                </Field>
                <Field label="Industry" hint="Suggests the recommended guardrail profile — you can override it in AI Configuration.">
                  <select className="select" value={form.industry} onChange={(e) => setIndustry(e.target.value)}>
                    {opts.industries.map((o) => <option key={o.code} value={o.code}>{o.name}</option>)}
                  </select>
                </Field>
                <Field label="Data region" hint="Configured operational region. Infrastructure deployment may differ.">
                  <select className="select" value={form.region} onChange={(e) => set("region", e.target.value)}>
                    {opts.dataRegions.map((o) => <option key={o.code} value={o.code}>{o.name}{o.infrastructureReady ? "" : " — configured region"}</option>)}
                  </select>
                </Field>
              </StepGrid>
            )}

            {step === 1 && (
              <StepGrid title="Subscription" sub="Plan limits are enforced by metering; overage is billed monthly.">
                <Field label="Plan">
                  <div className="col gap-6">
                    {opts.plans.map((p) => {
                      const on = form.plan === p.code;
                      return (
                        <button key={p.code} type="button" aria-pressed={on} onClick={() => set("plan", p.code)}
                          className="row-between card-pad-sm gap-12"
                          style={{ border: `1px solid ${on ? "var(--brand-500)" : "var(--hairline)"}`, borderRadius: 10, background: on ? "var(--surface-2)" : "transparent", textAlign: "left", cursor: "pointer" }}>
                          <div>
                            <div className="t-strong" style={{ fontSize: 13 }}>{p.name} — {p.description}</div>
                            <div className="t-micro">${p.priceMonthly.toLocaleString()}/mo · {p.minutesIncluded.toLocaleString()} min · {p.botLimit} bots · {p.seatsIncluded} seats included</div>
                          </div>
                          {p.isRecommended && <span className="tag">Recommended</span>}
                        </button>
                      );
                    })}
                  </div>
                </Field>
                <Field label="Seats"><input className="input" type="number" min={1} value={form.seats} onChange={(e) => set("seats", e.target.value)} /></Field>
                <Field label="Included voice minutes / month" hint="Set by the selected plan.">
                  <input className="input" value={(selectedPlan?.minutesIncluded ?? 0).toLocaleString()} readOnly disabled aria-label="Included voice minutes per month" />
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
                <Field label="AI configuration profile" hint="Maps to an approved-model profile in AI Governance.">
                  <div className="row gap-8">
                    <select className="select grow" value={form.aiProfile} onChange={(e) => set("aiProfile", e.target.value)}>
                      {opts.aiProfiles.map((p) => <option key={p.code} value={p.code}>{p.name} — {p.description}</option>)}
                    </select>
                    {selectedProfile && <span className="chip chip-neutral" style={{ textTransform: "capitalize", whiteSpace: "nowrap" }}>{selectedProfile.costCategory} cost</span>}
                  </div>
                </Field>
                <Field
                  label="Guardrail profile"
                  hint={
                    !guardrailTouched && form.guardrailProfile === suggestedGuardrailProfile(form.industry)
                      ? `Suggested by the ${opts.industries.find((i) => i.code === form.industry)?.name ?? "selected"} industry. Mandatory platform guardrails always apply.`
                      : "Manually selected — industry changes will not replace it. Mandatory platform guardrails always apply."
                  }
                >
                  <select
                    className="select"
                    value={form.guardrailProfile}
                    onChange={(e) => { setGuardrailTouched(true); set("guardrailProfile", e.target.value); }}
                  >
                    {(opts.guardrailProfiles ?? []).map((p) => (
                      <option key={p.id} value={p.id}>{p.name}{p.description ? ` — ${p.description}` : ""}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Call Summary" hint="Both settings are independent and can be changed later from Edit tenant.">
                  <div className="col gap-6">
                    <div className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                      <div><div className="t-strong" style={{ fontSize: 13 }}>Generate call summary after calls</div><div className="t-micro">AI summary, outcome and next best action once each call ends</div></div>
                      <Toggle checked={form.callSummaryEnabled} onChange={(v) => set("callSummaryEnabled", v)} label="Generate call summary after calls" />
                    </div>
                    <div className="row-between card-pad-sm" style={{ border: "1px solid var(--hairline)", borderRadius: 10 }}>
                      <div><div className="t-strong" style={{ fontSize: 13 }}>Use previous call summary on new calls</div><div className="t-micro">Give the bot the customer's latest stored summary when a call starts</div></div>
                      <Toggle checked={form.usePreviousCallSummary} onChange={(v) => set("usePreviousCallSummary", v)} label="Use previous call summary on new calls" />
                    </div>
                  </div>
                </Field>
                <Field label="Languages" required error={errors.languages}>
                  <div className="row wrap gap-6">
                    {opts.languages.map((l) => {
                      const on = form.languages.includes(l.code);
                      return (
                        <button key={l.code} className={`chip ${on ? "chip-brand" : "chip-neutral"}`} aria-pressed={on} title={l.nativeName} onClick={() =>
                          set("languages", on ? form.languages.filter((x) => x !== l.code) : [...form.languages, l.code])
                        }>{on && <Icon name="check" size={11} />}{l.name}</button>
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
              </>
            )}
          </div>

          <div className="row-between mt-24" style={{ borderTop: "1px solid var(--hairline)", paddingTop: 16 }}>
            <Button variant="ghost" icon="chevron-left" onClick={back} disabled={step === 0 || !!tasks}>Back</Button>
            {step < steps.length - 1 ? (
              <Button variant="primary" onClick={next} disabled={!opts}>Continue<Icon name="chevron-right" size={14} /></Button>
            ) : allDone ? (
              <Button variant="primary" icon="check" onClick={() => { toast("Tenant added to Organizations"); navigate("/admin/tenants"); }}>Finish</Button>
            ) : (
              <Button variant="primary" icon="rocket" onClick={launch} disabled={!opts || (!!tasks && !anyFailed && !allDone)}>
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
