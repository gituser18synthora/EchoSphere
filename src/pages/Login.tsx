import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@/components/Icon";
import { useApp } from "@/state/AppContext";
import type { Role } from "@/types/domain";

const personas: { role: Role; name: string; email: string; tenantName?: string; title: string; desc: string }[] = [
  {
    role: "tenant_admin", name: "Priya Sharma", email: "priya.sharma@meridianhealth.com",
    tenantName: "Meridian Health Group", title: "Tenant Admin",
    desc: "Build, test and publish your organization’s VoiceBots. Manage knowledge, workflows, channels, team and analytics.",
  },
  {
    role: "super_admin", name: "Alex Rivera", email: "alex.rivera@aurexion.com",
    title: "Super Admin",
    desc: "Platform governance: tenants, subscriptions, AI governance, telephony, global knowledge, monitoring and security.",
  },
];

export default function Login() {
  const { signIn } = useApp();
  const navigate = useNavigate();
  const [busy, setBusy] = useState<Role | null>(null);

  const enter = (p: (typeof personas)[number]) => {
    setBusy(p.role);
    setTimeout(() => {
      signIn({ name: p.name, email: p.email, role: p.role, tenantName: p.tenantName });
      navigate(p.role === "super_admin" ? "/admin" : "/t");
    }, 450);
  };

  return (
    <div className="auth-screen">
      <div className="auth-panel">
        <div className="row gap-12" style={{ marginBottom: 28 }}>
          <div className="sidebar-brand-logo" style={{ width: 40, height: 40 }}><Icon name="mic" size={21} /></div>
          <div>
            <div style={{ fontSize: 18, fontWeight: 750, letterSpacing: "-0.01em" }}>AUREXION EchoSphere</div>
            <div className="t-micro" style={{ letterSpacing: "0.08em", textTransform: "uppercase" }}>Enterprise VoiceBot Platform</div>
          </div>
        </div>
        <h1 style={{ fontSize: 26, fontWeight: 750, letterSpacing: "-0.02em" }}>Welcome back</h1>
        <p className="t-sub" style={{ marginBottom: 22 }}>
          Choose a workspace to continue. Single sign-on is enforced for production tenants.
        </p>
        <div className="col gap-12">
          {personas.map((p) => (
            <button
              key={p.role}
              className="card card-pad card-clickable"
              style={{ textAlign: "left", display: "flex", gap: 14, alignItems: "flex-start" }}
              onClick={() => enter(p)}
              disabled={busy !== null}
            >
              <div className={`icon-tile ${p.role === "super_admin" ? "brand" : "info"}`}>
                <Icon name={p.role === "super_admin" ? "shield" : "building"} size={17} />
              </div>
              <div className="grow">
                <div className="row-between">
                  <span className="t-section">{p.title}</span>
                  {busy === p.role ? <span className="spinner" /> : <Icon name="arrow-right" size={15} />}
                </div>
                <div className="t-micro mt-4">{p.name} · {p.tenantName ?? "Aurexion Platform"}</div>
                <p className="t-sub mt-8" style={{ fontSize: 12.5 }}>{p.desc}</p>
              </div>
            </button>
          ))}
        </div>
        <p className="t-micro mt-24">
          Demo build — authentication is mocked. RBAC boundaries between the two roles are enforced in routing and the service layer.
        </p>
      </div>
      <div className="auth-hero">
        <div style={{ position: "relative", maxWidth: 460 }}>
          <div className="chip chip-brand" style={{ background: "rgba(138,114,230,0.2)", color: "#cfc4f8", marginBottom: 18 }}>
            <span className="chip-dot live" /> 47 tenants · 128 bots live
          </div>
          <h2 style={{ fontSize: 30, fontWeight: 750, letterSpacing: "-0.02em", lineHeight: 1.25 }}>
            Every customer call, answered with a voice you govern.
          </h2>
          <p style={{ color: "#b9b3cf", marginTop: 14, fontSize: 14.5, lineHeight: 1.65 }}>
            Create a bot, ground it in your knowledge, give it a voice, test it like software,
            and publish with approvals, versioning and one-click rollback — while the platform
            team governs models, guardrails and telephony centrally.
          </p>
          <div className="row gap-16 wrap" style={{ marginTop: 26 }}>
            {["Versioned releases", "Approval workflow", "Full call tracing", "PII guardrails"].map((f) => (
              <span key={f} className="row gap-6" style={{ fontSize: 12.5, color: "#cfc4f8", fontWeight: 600 }}>
                <Icon name="check-circle" size={14} /> {f}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
