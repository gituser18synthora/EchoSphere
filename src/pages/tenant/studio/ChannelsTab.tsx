import { useState } from "react";
import type { ChannelConfig, ChannelType, VoiceBot } from "@/types/domain";
import { useAsync } from "@/hooks/useAsync";
import {
  activateChannel, archiveChannel, deactivateChannel, listChannels, saveChannel, testChannel,
} from "@/services/api";
import {
  Button, Callout, CardSkeleton, ConfirmModal, ErrorState, Field, Modal, StatusChip,
} from "@/components/ui";
import { Icon, type IconName } from "@/components/Icon";
import { useApp } from "@/state/AppContext";

export const channelMeta: Record<ChannelType, { icon: IconName; name: string; desc: string }> = {
  voice: { icon: "phone", name: "Voice", desc: "Inbound PSTN / SIP calls" },
  whatsapp: { icon: "whatsapp", name: "WhatsApp", desc: "Business messaging" },
  web: { icon: "monitor", name: "Web widget", desc: "Chat + voice on your site" },
  mobile: { icon: "smartphone", name: "Mobile SDK", desc: "In-app voice assistant" },
  sms: { icon: "message", name: "SMS", desc: "Text-message fallback" },
};

const ALL_TYPES: ChannelType[] = ["voice", "whatsapp", "web", "mobile", "sms"];

/* ---------- client-side validation (mirrors backend/routers/channels.py) ---------- */

const E164 = /^\+[1-9]\d{6,14}$/;
const ENV_REF = /^env:[A-Za-z_][A-Za-z0-9_]*$/;
const ORIGIN = /^https?:\/\/[A-Za-z0-9.-]+(:\d{1,5})?$/;
const BUNDLE = /^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$/;
const SENDER = /^([A-Za-z0-9]{3,15}|\+[1-9]\d{6,14})$/;

const normPhone = (v: string) => v.replace(/[\s().-]/g, "");

interface FieldDef {
  key: string;
  label: string;
  kind: "text" | "select" | "lines";
  options?: string[];
  placeholder?: string;
  hint?: string;
  /** returns an error message or null; runs only when visible */
  validate: (value: string, form: Record<string, string>) => string | null;
  showIf?: (form: Record<string, string>) => boolean;
  secretRef?: boolean;
}

const req = (label: string) => (v: string) => (v.trim() ? null : `${label} is required.`);
const refField = (required: (f: Record<string, string>) => boolean) =>
  (v: string, f: Record<string, string>) => {
    if (!v.trim()) return required(f) ? "Required for this provider." : null;
    return ENV_REF.test(v.trim()) ? null : "Must be an environment reference like env:VAR_NAME — raw secrets are never stored.";
  };

const FIELDS: Record<ChannelType, FieldDef[]> = {
  voice: [
    { key: "phoneNumber", label: "Phone number", kind: "text", placeholder: "+14155550119",
      hint: "E.164 — this number is claimed for inbound routing to this bot.",
      validate: (v) => (E164.test(normPhone(v)) ? null : "Enter an E.164 number, e.g. +14155550119.") },
    { key: "telephonyProvider", label: "Telephony provider", kind: "select",
      options: ["freeswitch", "twilio", "telnyx", "plivo", "exotel"],
      validate: req("Provider") },
    { key: "publicWsBase", label: "Public WebSocket base", kind: "text", placeholder: "wss://voice.example.com",
      hint: "Where the carrier streams call media (your voice runtime's public address).",
      showIf: (f) => f.telephonyProvider !== "freeswitch",
      validate: (v) => (!v.trim() ? "Required for cloud telephony providers."
        : v.startsWith("ws://") || v.startsWith("wss://") ? null : "Must start with ws:// or wss://.") },
    { key: "authTokenReference", label: "Auth token reference", kind: "text", placeholder: "env:TWILIO_AUTH_TOKEN",
      hint: "Environment reference only — the raw token never leaves the server env.", secretRef: true,
      showIf: (f) => f.telephonyProvider === "twilio",
      validate: refField((f) => f.telephonyProvider === "twilio") },
    { key: "language", label: "Language override", kind: "text", placeholder: "en (optional)",
      validate: () => null },
    { key: "voiceId", label: "Voice profile id", kind: "text", placeholder: "optional",
      validate: () => null },
  ],
  whatsapp: [
    { key: "whatsappNumber", label: "WhatsApp number", kind: "text", placeholder: "+14155550119",
      validate: (v) => (E164.test(normPhone(v)) ? null : "Enter an E.164 number.") },
    { key: "provider", label: "Provider", kind: "select", options: ["meta", "twilio", "pinbot"],
      validate: req("Provider") },
    { key: "phoneNumberId", label: "Phone number ID", kind: "text", placeholder: "Meta Cloud API phone number id",
      showIf: (f) => f.provider === "meta",
      validate: (v, f) => (f.provider === "meta" && !v.trim() ? "Required for the Meta Cloud API." : null) },
    { key: "businessAccountId", label: "Business account ID", kind: "text", placeholder: "optional",
      validate: () => null },
    { key: "apiKeyReference", label: "API key reference", kind: "text", placeholder: "env:WHATSAPP_API_KEY",
      secretRef: true, validate: refField(() => true) },
    { key: "webhookSecretReference", label: "Webhook secret reference", kind: "text", placeholder: "env:WHATSAPP_WEBHOOK_SECRET",
      hint: "Used to verify inbound webhook signatures and the Meta subscribe handshake.", secretRef: true,
      validate: refField((f) => f.provider === "meta") },
  ],
  web: [
    { key: "allowedOrigins", label: "Allowed origins", kind: "lines", placeholder: "https://www.example.com\nhttps://app.example.com",
      hint: "One origin per line — the widget only loads on these sites.",
      validate: (v) => {
        const lines = v.split("\n").map((s) => s.trim()).filter(Boolean);
        if (lines.length === 0) return "Add at least one origin.";
        const bad = lines.find((l) => !ORIGIN.test(l.replace(/\/$/, "")));
        return bad ? `Invalid origin: ${bad}` : null;
      } },
    { key: "widgetColor", label: "Widget color", kind: "text", placeholder: "#1A73E8 (optional)",
      validate: (v) => (!v.trim() || /^#[0-9A-Fa-f]{6}$/.test(v.trim()) ? null : "Use a hex color like #1A73E8.") },
    { key: "language", label: "Language override", kind: "text", placeholder: "en (optional)",
      validate: () => null },
  ],
  mobile: [
    { key: "platform", label: "Platform", kind: "select", options: ["both", "ios", "android"],
      validate: req("Platform") },
    { key: "bundleIds", label: "App bundle ids", kind: "lines", placeholder: "com.example.app",
      hint: "One per line.",
      validate: (v) => {
        const lines = v.split("\n").map((s) => s.trim()).filter(Boolean);
        if (lines.length === 0) return "Add at least one bundle id.";
        const bad = lines.find((l) => !BUNDLE.test(l));
        return bad ? `Invalid bundle id: ${bad}` : null;
      } },
    { key: "apiKeyReference", label: "SDK API key reference", kind: "text", placeholder: "env:MOBILE_SDK_KEY (optional)",
      secretRef: true, validate: refField(() => false) },
  ],
  sms: [
    { key: "provider", label: "Provider", kind: "select", options: ["twilio", "plivo", "telnyx", "exotel"],
      validate: req("Provider") },
    { key: "senderId", label: "Sender ID", kind: "text", placeholder: "AUREXION or +14155550119",
      validate: (v) => (SENDER.test(v.startsWith("+") ? normPhone(v) : v.trim()) ? null : "3–15 alphanumeric characters or an E.164 number.") },
    { key: "accountId", label: "Account ID", kind: "text", placeholder: "Twilio Account SID",
      showIf: (f) => f.provider === "twilio",
      validate: (v, f) => (f.provider === "twilio" && !v.trim() ? "Required for Twilio." : null) },
    { key: "apiKeyReference", label: "API key reference", kind: "text", placeholder: "env:SMS_API_KEY",
      secretRef: true, validate: refField(() => true) },
  ],
};

const LIST_KEYS = new Set(["allowedOrigins", "bundleIds"]);

function toForm(type: ChannelType, config: ChannelConfig["config"]): Record<string, string> {
  const form: Record<string, string> = {};
  for (const def of FIELDS[type]) {
    const value = config?.[def.key];
    form[def.key] = Array.isArray(value) ? value.join("\n")
      : typeof value === "string" ? value
      : def.kind === "select" ? def.options![0] : "";
  }
  return form;
}

function toConfig(type: ChannelType, form: Record<string, string>) {
  const config: Record<string, string | string[]> = {};
  for (const def of FIELDS[type]) {
    if (def.showIf && !def.showIf(form)) continue;
    const raw = (form[def.key] ?? "").trim();
    if (LIST_KEYS.has(def.key)) {
      config[def.key] = raw.split("\n").map((s) => s.trim()).filter(Boolean);
    } else if (raw) {
      config[def.key] = raw;
    }
  }
  return config;
}

/* ---------- tab ---------- */

export default function ChannelsTab({ bot }: { bot: VoiceBot }) {
  const q = useAsync(() => listChannels(bot.id), [bot.id]);
  const { toast, hasPermission } = useApp();
  const canManage = hasPermission("manage_channels");
  const [editing, setEditing] = useState<ChannelConfig | null>(null);
  const [details, setDetails] = useState<ChannelConfig | null>(null);
  const [confirm, setConfirm] = useState<{ kind: "deactivate" | "archive"; channel: ChannelConfig } | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null); // `${type}:${action}`

  if (q.error) return <ErrorState message={q.error} onRetry={q.reload} />;
  if (q.loading) return <div className="grid grid-2">{Array.from({ length: 4 }).map((_, i) => <CardSkeleton key={i} rows={3} />)}</div>;

  const configs = ALL_TYPES.map((t) => q.data?.find((c) => c.type === t) ?? ({
    id: null, type: t, botId: bot.id, status: "not_configured", enabled: false,
    detail: "", workflow: "—", config: null,
  } as ChannelConfig));

  const run = async (channel: ChannelConfig, action: string, fn: () => Promise<unknown>, success: string) => {
    setBusyAction(`${channel.type}:${action}`);
    try {
      await fn();
      toast(success);
      q.reload();
    } catch (e) {
      toast(e instanceof Error ? e.message : `${action} failed`, "error");
    } finally {
      setBusyAction(null);
    }
  };

  const test = (c: ChannelConfig) =>
    run(c, "test", async () => {
      const updated = await testChannel(bot.id, c.type);
      if (!updated.lastTest?.ok) {
        throw new Error(updated.lastTest?.message || "Connection test failed");
      }
    }, `${channelMeta[c.type].name} connection test passed`);

  return (
    <>
      <div className="grid grid-2">
        {configs.map((c) => {
          const meta = channelMeta[c.type];
          const configured = c.status !== "not_configured";
          const busy = (a: string) => busyAction === `${c.type}:${a}`;
          return (
            <div key={c.type} className="card card-pad col gap-12">
              <div className="row-between">
                <div className="row gap-12">
                  <span className={`icon-tile ${c.status === "live" ? "good" : c.status === "failed" ? "critical" : c.status === "testing" ? "warning" : "neutral"}`}>
                    <Icon name={meta.icon} size={17} />
                  </span>
                  <div>
                    <span className="t-strong" style={{ fontSize: 14 }}>{meta.name}</span>
                    <div className="t-micro">{meta.desc}</div>
                  </div>
                </div>
                <div className="row gap-6">
                  {configured && !c.enabled && <span className="tag">paused</span>}
                  <StatusChip status={c.status} />
                </div>
              </div>

              <div className="col gap-6" style={{ fontSize: 12.5 }}>
                <div className="row-between" style={{ padding: "6px 0", borderBottom: "1px solid var(--hairline)" }}>
                  <span className="t-sub">Endpoint</span>
                  <code style={{ fontSize: 12 }}>{c.detail || "—"}</code>
                </div>
                <div className="row-between" style={{ padding: "6px 0", borderBottom: "1px solid var(--hairline)" }}>
                  <span className="t-sub">Routes to</span>
                  <span className="t-strong">
                    {c.binding ? `${c.binding.botName} ${c.binding.publishedVersion ? `v${c.binding.publishedVersion}` : "(unpublished)"}` : c.workflow}
                  </span>
                </div>
                <div className="row-between" style={{ padding: "6px 0" }}>
                  <span className="t-sub">Last test</span>
                  {c.lastTest ? (
                    <span className={`row gap-4 ${c.lastTest.ok ? "t-good" : "t-bad"}`} style={{ fontWeight: 600, fontSize: 12 }}>
                      <Icon name={c.lastTest.ok ? "check-circle" : "x-circle"} size={13} />
                      {new Date(c.lastTest.at).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
                    </span>
                  ) : <span className="t-micro">never</span>}
                </div>
              </div>

              {c.lastTest && !c.lastTest.ok && (
                <div className="callout callout-critical" style={{ padding: "9px 11px", fontSize: 12 }}>
                  <Icon name="x-circle" size={13} />
                  <div className="callout-body">{c.lastTest.message}</div>
                </div>
              )}

              <div className="row gap-6" style={{ flexWrap: "wrap" }}>
                {!configured ? (
                  canManage
                    ? <Button variant="primary" size="sm" icon="plus" onClick={() => setEditing(c)}>Configure</Button>
                    : <span className="t-micro">Channel management requires the channel-management permission.</span>
                ) : (
                  <>
                    <Button size="sm" icon="eye" onClick={() => setDetails(c)}>Details</Button>
                    {canManage && (
                      <>
                        <Button size="sm" icon="play" busy={busy("test")} onClick={() => test(c)}>Run test</Button>
                        <Button size="sm" icon="settings" onClick={() => setEditing(c)}>Edit</Button>
                        {c.enabled ? (
                          <Button size="sm" variant="ghost" icon="pause" onClick={() => setConfirm({ kind: "deactivate", channel: c })}>Deactivate</Button>
                        ) : (
                          <Button size="sm" variant="ghost" icon="play" busy={busy("activate")}
                            onClick={() => run(c, "activate", () => activateChannel(bot.id, c.type), `${meta.name} channel activated`)}>
                            Activate
                          </Button>
                        )}
                        <Button size="sm" variant="danger-ghost" icon="trash" onClick={() => setConfirm({ kind: "archive", channel: c })}>Remove</Button>
                      </>
                    )}
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {editing && (
        <ChannelFormModal
          bot={bot}
          channel={editing}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); q.reload(); }}
        />
      )}

      {details && (
        <ChannelDetailsModal channel={details} onClose={() => setDetails(null)} />
      )}

      {confirm && (
        <ConfirmModal
          open
          onClose={() => setConfirm(null)}
          busy={busyAction === `${confirm.channel.type}:${confirm.kind}`}
          danger
          title={confirm.kind === "deactivate"
            ? `Deactivate the ${channelMeta[confirm.channel.type].name} channel?`
            : `Remove the ${channelMeta[confirm.channel.type].name} channel?`}
          body={confirm.kind === "deactivate"
            ? "Live traffic on this channel stops immediately — inbound calls and messages will be rejected until it is reactivated."
            : "The channel is archived and its phone number (if any) is released. Its configuration is kept for audit but the channel stops receiving traffic."}
          confirmLabel={confirm.kind === "deactivate" ? "Deactivate" : "Remove channel"}
          onConfirm={() => {
            const { kind, channel } = confirm;
            setConfirm(null);
            void run(
              channel, kind,
              () => (kind === "deactivate" ? deactivateChannel(bot.id, channel.type) : archiveChannel(bot.id, channel.type)),
              kind === "deactivate"
                ? `${channelMeta[channel.type].name} channel deactivated`
                : `${channelMeta[channel.type].name} channel removed`,
            );
          }}
        />
      )}
    </>
  );
}

/* ---------- configure / edit modal ---------- */

function ChannelFormModal({ bot, channel, onClose, onSaved }: {
  bot: VoiceBot; channel: ChannelConfig; onClose: () => void; onSaved: () => void;
}) {
  const { toast } = useApp();
  const meta = channelMeta[channel.type];
  const defs = FIELDS[channel.type];
  const [form, setForm] = useState(() => toForm(channel.type, channel.config));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [apiError, setApiError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const isNew = channel.status === "not_configured";

  const save = async () => {
    const errs: Record<string, string> = {};
    for (const def of defs) {
      if (def.showIf && !def.showIf(form)) continue;
      const problem = def.validate(form[def.key] ?? "", form);
      if (problem) errs[def.key] = problem;
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setApiError(null);
    try {
      await saveChannel(bot.id, channel.type, { config: toConfig(channel.type, form) });
      toast(`${meta.name} channel ${isNew ? "created" : "updated"}`);
      onSaved();
    } catch (e) {
      setApiError(e instanceof Error ? e.message : "Saving the channel failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={`${isNew ? "Configure" : "Edit"} ${meta.name} channel`}
      sub={`${bot.name} — saved to the platform database; secrets stay as environment references.`}
      wide
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" icon="check" busy={busy} onClick={save}>
            {isNew ? "Create channel" : "Save changes"}
          </Button>
        </>
      }
    >
      <div className="col gap-14">
        {apiError && <Callout tone="critical" title="Save failed">{apiError}</Callout>}
        <div className="grid grid-2">
          {defs.map((def) => {
            if (def.showIf && !def.showIf(form)) return null;
            const set = (v: string) => { setForm((f) => ({ ...f, [def.key]: v })); setErrors((e) => ({ ...e, [def.key]: "" })); };
            return (
              <Field key={def.key} label={def.label} hint={def.hint} error={errors[def.key] || undefined}>
                {def.kind === "select" ? (
                  <select className="select" value={form[def.key]} onChange={(e) => set(e.target.value)}>
                    {def.options!.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : def.kind === "lines" ? (
                  <textarea className="input" rows={3} value={form[def.key]} placeholder={def.placeholder}
                    onChange={(e) => set(e.target.value)} style={{ resize: "vertical", fontFamily: "inherit" }} />
                ) : (
                  <input className="input" value={form[def.key]} placeholder={def.placeholder}
                    onChange={(e) => set(e.target.value)} />
                )}
              </Field>
            );
          })}
        </div>
      </div>
    </Modal>
  );
}

/* ---------- details modal ---------- */

function ChannelDetailsModal({ channel, onClose }: { channel: ChannelConfig; onClose: () => void }) {
  const meta = channelMeta[channel.type];
  const binding = channel.binding;
  const entries = Object.entries(channel.config ?? {});
  return (
    <Modal
      open
      onClose={onClose}
      title={`${meta.name} channel details`}
      sub="Secret values are never returned — credential fields show environment references."
      wide
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <div className="col gap-14">
        <div>
          <div className="t-strong mb-8" style={{ fontSize: 13 }}>Configuration</div>
          <div className="col gap-4" style={{ fontSize: 12.5 }}>
            {entries.length === 0 && <span className="t-sub">Not configured yet.</span>}
            {entries.map(([key, value]) => (
              <div className="row-between" key={key} style={{ padding: "5px 0", borderBottom: "1px solid var(--hairline)" }}>
                <span className="t-sub">{key}</span>
                <code style={{ fontSize: 12, textAlign: "right" }}>
                  {Array.isArray(value) ? value.join(", ") : String(value ?? "—")}
                </code>
              </div>
            ))}
            {channel.type === "whatsapp" && channel.id && (
              <div className="row-between" style={{ padding: "5px 0" }}>
                <span className="t-sub">Inbound webhook URL</span>
                <code style={{ fontSize: 12 }}>{`/api/v1/channels/whatsapp/webhook/${channel.id}`}</code>
              </div>
            )}
          </div>
        </div>

        {binding && (
          <div>
            <div className="t-strong mb-8" style={{ fontSize: 13 }}>Routing &amp; bindings</div>
            <div className="col gap-4" style={{ fontSize: 12.5 }}>
              {[
                ["Bot", `${binding.botName} (${binding.botStatus})`],
                ["Published version", binding.publishedVersion ? `v${binding.publishedVersion}` : "— not published"],
                ["System prompt", binding.systemPromptPublished ? "published" : "not published"],
                ["Knowledge bases", String(binding.knowledgeBases)],
                ["Language", binding.language],
                ["Voice profile", binding.voiceId ?? "platform default"],
                ["Providers", `${binding.sttProvider} / ${binding.llmProvider} / ${binding.ttsProvider}`],
              ].map(([k, v]) => (
                <div className="row-between" key={k} style={{ padding: "5px 0", borderBottom: "1px solid var(--hairline)" }}>
                  <span className="t-sub">{k}</span>
                  <span className="t-strong">{v}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {channel.lastTest && (
          <div>
            <div className="t-strong mb-8" style={{ fontSize: 13 }}>
              Last connection test — {channel.lastTest.ok ? "passed" : "failed"} · {new Date(channel.lastTest.at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
            </div>
            <div className="col gap-4" style={{ fontSize: 12.5 }}>
              {(channel.lastTest.checks ?? []).map((check, i) => (
                <div className="row gap-8" key={i} style={{ padding: "4px 0" }}>
                  <Icon name={check.ok ? "check-circle" : "x-circle"} size={13} />
                  <span className="t-strong">{check.name}</span>
                  <span className="t-sub">{check.message}</span>
                </div>
              ))}
              {(channel.lastTest.checks ?? []).length === 0 && (
                <span className="t-sub">{channel.lastTest.message}</span>
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
