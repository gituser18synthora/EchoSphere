import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Icon } from "@/components/Icon";
import { PasswordInput } from "@/components/ui";
import { useApp } from "@/state/AppContext";
import * as api from "@/services/api";
import aurexionLogo from "@/assets/brand/Aurexion-logo.svg";
import robotMascot from "@/assets/brand/synthora-ai-front-view.png";

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
      <div className="auth-mascot" aria-hidden>
        <div className="auth-mascot-glow">
          <img
            src={robotMascot}
            alt=""
            className="auth-mascot-img"
          />
        </div>
      </div>

      <div className="auth-panel">
        <div className="auth-card">
          <div className="auth-card-inner">
            <div className="auth-brand">
              <img src={aurexionLogo} alt="Aurexion" className="auth-brand-logo" />
              <div className="auth-brand-divider" aria-hidden />
              <div className="auth-brand-product">
                <span className="auth-brand-name">EchoSphere</span>
                <span className="auth-brand-tagline">Enterprise VoiceBot Platform</span>
              </div>
            </div>

            <h1 className="auth-title">Welcome back</h1>
            <p className="auth-subtitle">
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
        </div>
      </div>
    </div>
  );
}
