import { useState, type FormEvent } from "react";
import { Button, Callout, PasswordInput } from "@/components/ui";
import * as api from "@/services/api";
import { PASSWORD_POLICY_HINT, passwordPolicyError } from "@/services/password";
import type { SessionUserInfo } from "@/types/domain";

export function InitialPasswordForm({ token, currentPassword, onComplete, onCancel }: {
  token: string; currentPassword: string;
  onComplete: (result: { token: string; user: SessionUserInfo }) => void; onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const problem = passwordPolicyError(password) || (password !== confirm ? "The passwords do not match." : "")
      || (password === currentPassword ? "Choose a password different from the temporary password." : "");
    if (problem) { setError(problem); return; }
    setBusy(true); setError("");
    try {
      const result = await api.completeInitialPasswordChange(token, {
        currentPassword, newPassword: password, confirmPassword: confirm,
      });
      setPassword(""); setConfirm(""); onComplete(result);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not set your password."); }
    finally { setBusy(false); }
  };
  return <form className="col gap-12" onSubmit={submit}>
    <p className="t-sub">Choose your own password before opening your workspace.</p>
    <label className="col gap-6">New password<PasswordInput value={password} onChange={setPassword} aria-label="New password" autoComplete="new-password" required /></label>
    <p className="field-hint">{PASSWORD_POLICY_HINT}</p>
    <label className="col gap-6">Confirm new password<PasswordInput value={confirm} onChange={setConfirm} aria-label="Confirm new password" autoComplete="new-password" required /></label>
    {error && <div role="alert"><Callout tone="critical">{error}</Callout></div>}
    <Button type="submit" variant="primary" busy={busy}>Set password and continue</Button>
    <Button type="button" variant="ghost" disabled={busy} onClick={onCancel}>Back to sign in</Button>
  </form>;
}
