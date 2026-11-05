import { useEffect, useState } from "react";
import type { RealCallDefaults, RealCallJob, VoiceBot } from "@/types/domain";
import { getRealCallDefaults, hangupRealCall, getRealCallStatus, startRealCall } from "@/services/api";
import { Button, Callout, Field } from "@/components/ui";
import { Icon } from "@/components/Icon";
import { formatCallDuration } from "./PhoneCallView";

/* Real call: dial a tester's own mobile through the dialer's FreeSWITCH and
   let the bot answer it like a campaign call. The backend owns everything
   that is not the tester's decision — the dialer's channel variables, the
   bot's saved customer test data (sent as vb_* variables), the ESL socket —
   so this view is just: number → confirm → Start Call → status → Hang up.
   Per-call choices never change the bot's saved configuration. */

const PHONE_RE = /^\+?[0-9]{8,15}$/;
const POLL_MS = 2500;
const POLL_MAX_MS = 15 * 60 * 1000;
const RING_MIN = 10;
const RING_MAX = 120;

export function normalizePhoneInput(raw: string): string {
  return raw.replace(/[\s().-]/g, "");
}

export function phoneInputError(raw: string): string | undefined {
  if (!raw.trim()) return "Enter the mobile number to call.";
  if (!PHONE_RE.test(normalizePhoneInput(raw))) return "8 to 15 digits, optional leading + (spaces and dashes are fine).";
  return undefined;
}

const STATUS_LABEL: Record<string, string> = {
  dialing: "Dialing", answered: "Connected", failed: "Call failed", timeout: "No answer",
  error: "Could not place the call", hungup: "Ended", ended: "Ended",
};

export function statusTone(status: string): string {
  if (status === "answered") return "chip-good";
  if (status === "dialing") return "chip-brand";
  if (status === "failed" || status === "error") return "chip-critical";
  if (status === "timeout") return "chip-warning";
  return "chip-neutral";
}

const isLive = (job: RealCallJob | null): boolean =>
  job?.status === "dialing" || job?.status === "answered";

export default function RealCallTab({ bot, active }: { bot: VoiceBot; active: boolean }) {
  const [defaults, setDefaults] = useState<RealCallDefaults | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [phone, setPhone] = useState("");
  const [phoneTouched, setPhoneTouched] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [ringSeconds, setRingSeconds] = useState("");
  const [job, setJob] = useState<RealCallJob | null>(null);
  const [starting, setStarting] = useState(false);
  const [hangingUp, setHangingUp] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [seconds, setSeconds] = useState(0);

  /* Readiness (ESL configured, bot publishable, caller id) is fetched the
     first time the view is shown — never on page open, and it dials nothing. */
  useEffect(() => {
    if (!active || defaults || loadErr) return;
    let cancelled = false;
    getRealCallDefaults(bot.id)
      .then((d) => { if (!cancelled) setDefaults(d); })
      .catch((e: Error) => { if (!cancelled) setLoadErr(e.message); });
    return () => { cancelled = true; };
  }, [active, bot.id, defaults, loadErr]);

  /* Poll the tracked call while it is live, with a hard stop. */
  useEffect(() => {
    if (!job || !isLive(job)) return;
    const { jobId } = job;
    const startedAt = Date.now();
    const id = window.setInterval(() => {
      if (Date.now() - startedAt > POLL_MAX_MS) { window.clearInterval(id); return; }
      getRealCallStatus(bot.id, jobId)
        .then((next) => setJob((cur) => (cur?.jobId === jobId ? next : cur)))
        .catch(() => { /* transient — keep the last status */ });
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [bot.id, job?.jobId, job?.status]); // eslint-disable-line react-hooks/exhaustive-deps

  /* Connected-time counter, frozen when the call ends. */
  useEffect(() => {
    if (job?.status !== "answered") return;
    setSeconds(0);
    const startedAt = Date.now();
    const id = window.setInterval(() => setSeconds(Math.floor((Date.now() - startedAt) / 1000)), 500);
    return () => window.clearInterval(id);
  }, [job?.jobId, job?.status]);

  const phoneErr = phoneInputError(phone);
  const ringValue = ringSeconds.trim() === "" ? null : Number(ringSeconds);
  const ringErr = ringValue !== null && (!Number.isInteger(ringValue) || ringValue < RING_MIN || ringValue > RING_MAX)
    ? `Whole seconds between ${RING_MIN} and ${RING_MAX}.` : undefined;
  const blockers = defaults?.blockers ?? [];
  const live = isLive(job);
  const canStart = Boolean(defaults) && blockers.length === 0 && !phoneErr && !ringErr && confirmed && !starting && !live;

  const start = async () => {
    if (!canStart) return;
    setStarting(true); setActionErr(null);
    try {
      const started = await startRealCall(bot.id, {
        phoneNumber: normalizePhoneInput(phone),
        ...(ringValue !== null ? { params: { dialTimeout: String(ringValue) } } : {}),
      });
      setJob(started);
      setConfirmed(false);
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setStarting(false);
    }
  };

  const hangup = async () => {
    if (!job) return;
    setHangingUp(true); setActionErr(null);
    try {
      setJob(await hangupRealCall(bot.id, job.jobId));
    } catch (e) {
      setActionErr((e as Error).message);
    } finally {
      setHangingUp(false);
    }
  };

  return (
    <div className="card col gap-16" style={{ maxWidth: 560, margin: "0 auto", padding: 24 }} data-testid="real-call-tab">
      <div>
        <div className="row gap-8">
          <Icon name="smartphone" size={18} />
          <span className="card-title">Real call</span>
        </div>
        <p className="t-sub mt-4" style={{ margin: 0 }}>
          Rings a real phone through the dialer and connects it to <strong>{bot.name}</strong> exactly
          like a live campaign call, including the bot's saved customer test data as call context.
          Use your own test phone.
        </p>
      </div>

      {loadErr && <Callout tone="critical" title="Real calls are unavailable">{loadErr}</Callout>}
      {defaults && blockers.length > 0 && (
        <Callout tone="critical" title="Calls cannot be placed right now">
          <ul style={{ margin: "4px 0 0 16px", padding: 0 }}>
            {blockers.map((b) => <li key={b}>{b}</li>)}
          </ul>
        </Callout>
      )}

      <Field
        label="Mobile number to call"
        required
        error={phoneTouched ? phoneErr : undefined}
        hint={defaults?.callerIdNumber ? `The phone will see a call from ${defaults.callerIdNumber}.` : undefined}
      >
        <input
          className="input"
          inputMode="tel"
          placeholder="+91 98765 43210"
          value={phone}
          aria-invalid={phoneTouched && Boolean(phoneErr)}
          disabled={live}
          onChange={(e) => setPhone(e.target.value)}
          onBlur={() => setPhoneTouched(true)}
          data-testid="real-call-phone"
        />
      </Field>

      <div className="col gap-8">
        <button
          type="button"
          className="row gap-6 t-sub"
          style={{ background: "none", border: 0, padding: 0, cursor: "pointer" }}
          onClick={() => setShowAdvanced((v) => !v)}
          aria-expanded={showAdvanced}
          data-testid="real-call-advanced"
        >
          <Icon name={showAdvanced ? "chevron-up" : "chevron-down"} size={14} /> Advanced settings
        </button>
        {showAdvanced && (
          <Field label="Ring timeout (seconds)" hint="How long to let the phone ring before giving up. Leave empty for the default." error={ringErr}>
            <input
              className="input"
              inputMode="numeric"
              placeholder="default"
              value={ringSeconds}
              aria-invalid={Boolean(ringErr)}
              disabled={live}
              onChange={(e) => setRingSeconds(e.target.value)}
              style={{ maxWidth: 160 }}
              data-testid="real-call-ring"
            />
          </Field>
        )}
      </div>

      <label className="row gap-8" style={{ alignItems: "flex-start", cursor: live ? "default" : "pointer" }}>
        <input
          type="checkbox"
          checked={confirmed}
          disabled={live}
          onChange={(e) => setConfirmed(e.target.checked)}
          data-testid="real-call-confirm"
        />
        <span className="t-sub">I understand this places a real telephone call to the number above.</span>
      </label>

      {actionErr && <Callout tone="critical" title="Something went wrong">{actionErr}</Callout>}

      <div className="row gap-8">
        {live ? (
          <Button variant="danger" icon="phone" busy={hangingUp} onClick={() => void hangup()} data-testid="real-call-hangup">
            Hang up
          </Button>
        ) : (
          <Button variant="primary" icon="phone" busy={starting} disabled={!canStart} onClick={() => void start()} data-testid="real-call-start">
            Start Call
          </Button>
        )}
        {!defaults && !loadErr && <span className="row gap-6 t-micro"><span className="spinner" /> Checking readiness…</span>}
      </div>

      {job && (
        <div className="col gap-6" style={{ padding: 12, background: "var(--surface-2)", borderRadius: 10 }} role="status" data-testid="real-call-status">
          <div className="row gap-8" style={{ flexWrap: "wrap" }}>
            <span className={`chip ${statusTone(job.status)}`}>
              {job.status === "dialing" && <span className="chip-dot live" />}
              {STATUS_LABEL[job.status] ?? job.status}
            </span>
            <span className="t-sub">{job.phoneMasked}</span>
            {job.status === "answered" && <span className="t-num t-sub">{formatCallDuration(seconds)}</span>}
            {job.cause && job.status !== "answered" && <span className="tag">{job.cause}</span>}
          </div>
          <div className="t-sub">{job.detail}</div>
          {job.conversation ? (
            <div className="t-micro">
              Conversation {job.conversation.status}
              {job.conversation.durationSec ? ` · ${job.conversation.durationSec}s` : ""}
              {job.conversation.disposition ? ` · ${job.conversation.disposition}` : ""}
              {" · "}<a href="/t/conversations">open Conversations</a>
            </div>
          ) : job.status === "answered" ? (
            <div className="t-micro">Connecting the bot… the conversation will appear in Conversations shortly.</div>
          ) : null}
        </div>
      )}
    </div>
  );
}
