import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@/components/Icon";
import { PasswordInput } from "@/components/ui";
import { useApp } from "@/state/AppContext";
import * as api from "@/services/api";

export default function Login() {
  const { signIn } = useApp();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!email || !password || busy) return;
    setBusy(true);
    setError(null);
    try {
      const { token, user } = await api.login(email.trim(), password);
      signIn(
        {
          id: user.id,
          name: user.name,
          email: user.email,
          role: user.role,
          tenantName: user.tenantName ?? undefined,
          tenantId: user.tenantId,
          permissions: user.permissions,
        },
        token,
      );
      navigate(user.role === "super_admin" ? "/admin" : "/t");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
      setBusy(false);
    }
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
          Sign in with your work email. Your role determines your workspace.
        </p>
        <form className="col gap-12" onSubmit={submit}>
          <label className="col gap-6">
            <span className="t-section">Email</span>
            <input
              className="input"
              type="email"
              autoComplete="email"
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoFocus
              required
            />
          </label>
          <label className="col gap-6">
            <span className="t-section">Password</span>
            <PasswordInput
              autoComplete="current-password"
              placeholder="••••••••"
              value={password}
              onChange={setPassword}
              aria-label="Password"
              required
            />
          </label>
          {error && (
            <div className="callout callout-error" role="alert" style={{ padding: "10px 12px" }}>
              {error}
            </div>
          )}
          <button className="btn btn-primary" type="submit" disabled={busy || !email || !password}>
            {busy ? <span className="spinner" /> : <Icon name="arrow-right" size={15} />}
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </div>
      <div className="auth-hero">
        <div style={{ position: "relative", maxWidth: 460 }}>
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
