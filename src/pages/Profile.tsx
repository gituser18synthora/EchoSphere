/* My Profile — personal details, preferences and password change.
   Available to every signed-in role (mounted under /admin/profile and /t/profile). */

import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useApp } from "@/state/AppContext";
import { useAsync } from "@/hooks/useAsync";
import { changeMyPassword, listLanguages, me, updateMyProfile } from "@/services/api";
import { setToken } from "@/services/http";
import { Avatar, Button, Callout, Field, StatusChip, Tabs } from "@/components/ui";

const TIMEZONES = [
  "UTC", "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Europe/London",
  "Europe/Berlin", "America/New_York", "America/Chicago", "America/Los_Angeles",
];

function fmtDate(value?: string | null): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

export default function Profile() {
  const { user, updateSessionUser, toast } = useApp();
  const location = useLocation() as { state?: { tab?: string } };
  const [tab, setTab] = useState(location.state?.tab === "security" ? "security" : "profile");

  const meQ = useAsync(me, []);
  const langsQ = useAsync(() => listLanguages(), []);

  /* ---- profile form ---- */
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [phone, setPhone] = useState("");
  const [locale, setLocale] = useState("");
  const [timezone, setTimezone] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);

  useEffect(() => {
    if (meQ.data) {
      setFirstName(meQ.data.firstName || meQ.data.name.split(" ")[0] || "");
      setLastName(meQ.data.lastName || meQ.data.name.split(" ").slice(1).join(" "));
      setPhone(meQ.data.phone || "");
      setLocale(meQ.data.locale || "");
      setTimezone(meQ.data.timezone || "");
      setDirty(false);
    }
  }, [meQ.data]);

  /* warn about unsaved changes on tab close */
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  const mark = <T,>(setter: (v: T) => void) => (v: T) => { setter(v); setDirty(true); };

  const saveProfile = async () => {
    if (!firstName.trim()) { setProfileError("First name is required."); return; }
    setSaving(true);
    setProfileError(null);
    try {
      const updated = await updateMyProfile({
        firstName: firstName.trim(), lastName: lastName.trim(),
        phone, locale, timezone,
      });
      updateSessionUser({ name: updated.name });
      setDirty(false);
      toast("Profile updated");
      meQ.reload();
    } catch (e) {
      setProfileError(e instanceof Error ? e.message : "Could not save profile.");
    } finally {
      setSaving(false);
    }
  };

  /* ---- password form ---- */
  const [currentPw, setCurrentPw] = useState("");
  const [newPw, setNewPw] = useState("");
  const [confirmPw, setConfirmPw] = useState("");
  const [pwBusy, setPwBusy] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);

  const pwLocalError =
    newPw && confirmPw && newPw !== confirmPw ? "New password and confirmation do not match."
    : newPw && newPw === currentPw ? "The new password must be different from the current one."
    : null;

  const changePassword = async () => {
    setPwError(null);
    setPwDone(false);
    if (pwLocalError) { setPwError(pwLocalError); return; }
    setPwBusy(true);
    try {
      const result = await changeMyPassword({
        currentPassword: currentPw, newPassword: newPw, confirmPassword: confirmPw,
      });
      // Adopt the fresh token so THIS session survives the global invalidation.
      setToken(result.token);
      setCurrentPw(""); setNewPw(""); setConfirmPw("");
      setPwDone(true);
      toast("Password changed — other sessions were signed out");
    } catch (e) {
      setPwError(e instanceof Error ? e.message : "Password change failed.");
    } finally {
      setPwBusy(false);
    }
  };

  if (!user) return null;
  const info = meQ.data;

  return (
    <div className="col gap-16">
      <div className="row gap-12" style={{ alignItems: "center" }}>
        <Avatar name={user.name} size="lg" />
        <div className="grow">
          <h1 className="page-title" style={{ marginBottom: 2 }}>{user.name}</h1>
          <div className="t-sub row gap-8" style={{ alignItems: "center" }}>
            {user.email}
            <StatusChip status="active" label={info?.roleName || user.role} />
            {user.tenantName && <span className="t-micro">· {user.tenantName}</span>}
          </div>
        </div>
      </div>

      <Tabs
        tabs={[
          { id: "profile", label: "Profile", icon: "user" },
          { id: "security", label: "Security", icon: "shield" },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "profile" && (
        <div className="card card-pad col gap-14" style={{ maxWidth: 640 }}>
          {profileError && <Callout tone="critical">{profileError}</Callout>}
          <div className="grid grid-2">
            <Field label="First name" required>
              <input className="input" value={firstName} onChange={(e) => mark(setFirstName)(e.target.value)} maxLength={80} />
            </Field>
            <Field label="Last name">
              <input className="input" value={lastName} onChange={(e) => mark(setLastName)(e.target.value)} maxLength={80} />
            </Field>
            <Field label="Email" hint="Contact an administrator to change your sign-in email.">
              <input className="input" value={user.email} disabled />
            </Field>
            <Field label="Phone">
              <input className="input" value={phone} onChange={(e) => mark(setPhone)(e.target.value)} maxLength={30} placeholder="+91 …" />
            </Field>
            <Field label="Preferred language">
              <select className="select" value={locale} onChange={(e) => mark(setLocale)(e.target.value)}>
                <option value="">System default</option>
                {(langsQ.data ?? []).map((l) => (
                  <option key={l.code} value={l.code}>{l.name}{l.nativeName ? ` · ${l.nativeName}` : ""}</option>
                ))}
              </select>
            </Field>
            <Field label="Time zone">
              <select className="select" value={timezone} onChange={(e) => mark(setTimezone)(e.target.value)}>
                <option value="">System default</option>
                {TIMEZONES.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
              </select>
            </Field>
          </div>
          <div className="row gap-8" style={{ justifyContent: "flex-end" }}>
            {dirty && <span className="t-micro" style={{ alignSelf: "center" }}>Unsaved changes</span>}
            <Button variant="primary" busy={saving} disabled={!dirty} onClick={saveProfile}>
              Save profile
            </Button>
          </div>
        </div>
      )}

      {tab === "security" && (
        <div className="col gap-16" style={{ maxWidth: 640 }}>
          <div className="card card-pad col gap-14">
            <div className="t-strong">Change password</div>
            {pwDone && (
              <Callout tone="good" title="Password changed">
                Your password was updated. Other active sessions have been signed out.
              </Callout>
            )}
            {(pwError || pwLocalError) && !pwDone && (
              <Callout tone="critical">{pwError || pwLocalError}</Callout>
            )}
            <Field label="Current password" required>
              <input className="input" type="password" autoComplete="current-password"
                value={currentPw} onChange={(e) => setCurrentPw(e.target.value)} />
            </Field>
            <Field label="New password" required
              hint="At least 10 characters with an uppercase letter, a lowercase letter and a digit.">
              <input className="input" type="password" autoComplete="new-password"
                value={newPw} onChange={(e) => setNewPw(e.target.value)} />
            </Field>
            <Field label="Confirm new password" required>
              <input className="input" type="password" autoComplete="new-password"
                value={confirmPw} onChange={(e) => setConfirmPw(e.target.value)} />
            </Field>
            <div className="row" style={{ justifyContent: "flex-end" }}>
              <Button
                variant="primary"
                busy={pwBusy}
                disabled={!currentPw || !newPw || !confirmPw || Boolean(pwLocalError)}
                onClick={changePassword}
              >
                Change password
              </Button>
            </div>
          </div>

          <div className="card card-pad col gap-8">
            <div className="t-strong">Recent sign-in activity</div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="t-sub">Last sign-in</span>
              <span>{fmtDate(info?.lastLoginAt)}</span>
            </div>
            <div className="row" style={{ justifyContent: "space-between" }}>
              <span className="t-sub">Password last changed</span>
              <span>{fmtDate(info?.passwordChangedAt)}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
